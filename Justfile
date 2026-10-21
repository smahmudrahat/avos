# POC targets. `push` runs on your Mac; the rest run on the host (ssh avos-dev, cd ~/poc).
host := "avos-dev"

base_url := "https://api.tokenfactory.nebius.com/v1"
model := "Qwen/Qwen3-30B-A3B-Instruct-2507"
judge := "deepseek-ai/DeepSeek-V4-Flash-0731"

# Mac: copy the repo to the host; the model key and upstream URL go to the gateway only, model names to the VMs
push:
    rsync -az --delete --exclude .git --exclude .env --exclude .venv --exclude .serena --exclude notes ./ {{host}}:poc/
    { grep '^nebulus_API_KEY=' .env | sed 's/^nebulus_API_KEY=/OPENAI_API_KEY=/'; echo "OPENAI_BASE_URL={{base_url}}"; } \
      | ssh {{host}} 'sudo install -D -m 600 /dev/stdin /var/lib/avos/gateway.env'
    { echo "MODEL={{model}}"; echo "JUDGE_MODEL={{judge}}"; } | ssh {{host}} 'sudo install -D -m 600 /dev/stdin /var/lib/avos/agent.env'

# Host: build kernel + rootfs with the agent baked in
image:
    sudo infra/image/build.sh

create name="vm-01":
    sudo infra/vm create {{name}}

destroy name="vm-01":
    sudo infra/vm destroy {{name}}

# Host: M1 median and worst over N boots
bench n="20":
    sudo infra/vm bench {{n}}

# Host: POC 1 done tests 1-5 (create, /health, model calls on the tap (to the gateway since POC 4), destroy, twice)
poc1:
    #!/usr/bin/env bash
    set -euo pipefail
    for round in 1 2; do
      echo "== round $round"
      sudo infra/vm create vm-01
      curl -sf http://172.16.1.2/health; echo
      sudo timeout 25 tcpdump -ni tap-vm-01 -c 6 'tcp port 8443' 2>/dev/null
      sudo infra/vm destroy vm-01
      if curl -s -m 2 http://172.16.1.2/health; then echo "FAIL: still answering"; exit 1; else echo "curl fails after destroy: ok"; fi
    done

# Host: one ephemeral job, e.g. `just job tau2 retail/0`
job harness task:
    sudo infra/job run {{harness}} {{task}}

# Host: POC 2 done tests 3-4: N τ² jobs on the first N train tasks (never held-out), M2 and M13
poc2 n="10":
    #!/usr/bin/env bash
    set -uo pipefail
    tasks=$(python3 -c "import json; print(*json.load(open('data/splits/retail_user_disjoint_v1.json'))['train'][:{{n}}])")
    for t in $tasks; do
      sudo infra/job run tau2 retail/$t 2>&1 | tail -1
      echo "left behind: vms=$(sudo ls /var/lib/avos/jail/firecracker | wc -l) taps=$(ip -br link | grep -c tap-)"
    done | tee /tmp/poc2.log
    python3 - <<'PY'
    import json, statistics
    jobs = [json.loads(l) for l in open("/tmp/poc2.log") if l.startswith("{")]
    done = [j for j in jobs if j["status"] == "done"]
    walls = [j["wall_ms"] / 1000 for j in done]
    print(f"M2 harness_e2e_pass: {len(done)}/{len(jobs)} = {100 * len(done) / len(jobs):.0f}%")
    print(f"M13 latency_per_task: median {statistics.median(walls):.1f} s, worst {max(walls):.1f} s")
    print(f"reward (info only): {sum(j['reward'] for j in done):.0f}/{len(done)} passed")
    for j in jobs:
        if j["status"] != "done": print("infra error:", j["run"], "http", j["http"], j.get("error", ""))
    PY

# Host, once (before `just image`): SPIRE server + identity registrar as services
spire:
    sudo identity/install.sh

# Host: principals as a tree (live VMs; `just who --all` includes destroyed ones)
who *args:
    sudo identity/registrar.py who {{args}}

# Host: POC 3 done tests 1-6 on vm-01, then M3/M4 over N extra sub-agents
poc3 n="20":
    #!/usr/bin/env bash
    set -euo pipefail
    vm() { sudo ssh -q -i /var/lib/avos/images/vm_key -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@172.16.1.2 "$@"; }
    spawn() { curl -sf -X POST http://172.16.1.2/spawn -d "{\"name\":\"$1\",\"argv\":[\"/usr/bin/python3\",\"/opt/avos/demo_agent.py\",$2]}"; echo; }
    vm_id=$(sudo infra/vm create vm-01 | tee /dev/stderr | grep -oP 'vm_id=\K\S+')
    trap 'sudo infra/vm destroy vm-01' EXIT
    echo "== 1-2: A spawns B (after its own SVID, ~1 s); B is in the ledger before its process starts"
    spawn A '"--sleep","120","--child","B=15"'
    timeout 15 bash -c 'until sudo identity/registrar.py who | grep -q /sub/B; do sleep 0.1; done'
    just who
    vm 'timeout 15 bash -c "until [ \$(ls /run/avos/ag_*/svid.0.pem | wc -l) = 2 ]; do sleep 0.1; done" 2>/dev/null'
    echo "== 3: log lines carry each principal's own SPIFFE ID"
    vm "journalctl -o cat -u avos-agent | grep spiffe_id"
    echo "== 5: a process started outside the shim asks the Workload API"
    vm '/opt/spire/bin/spire-agent api fetch x509 -socketPath /run/spire/agent.sock' && echo "FAIL: got an SVID" || echo "no SVID: ok"
    echo "== 6: copy B's SVID while B is alive"
    vm 'd=$(ls -td /run/avos/ag_* | head -1); cp $d/svid.0.pem /root/stolen.pem; cp $d/bundle.0.pem /root/bundle.pem; openssl x509 -in /root/stolen.pem -noout -startdate -enddate -ext subjectAltName; openssl verify -CAfile /root/bundle.pem /root/stolen.pem'
    echo "== 4: B exits, A stays live"
    sleep 16; just who
    echo "== 6 (cont.): the copy after its TTL"
    sleep 50; vm 'openssl verify -CAfile /root/bundle.pem /root/stolen.pem' && echo "FAIL: copy still valid" || echo "copy rejected: ok"
    echo "== VM principal: the VM holds an SVID for its own ID (uid 20000 only)"
    curl -s http://172.16.1.2/health | python3 -c 'import json, sys; print(json.load(sys.stdin)["vm_svid"])'
    vm 'openssl x509 -in /run/avos/vm/svid.0.pem -noout -ext subjectAltName | tail -1; stat -c "%U:%a %n" /run/avos/vm'
    echo "== M3/M4 over {{n}} sub-agents"
    spawn bench "\"--sleep\",\"30\"$(for i in $(seq {{n}}); do printf ',"--child","s%d=0"' "$i"; done)"
    sleep 30
    sudo identity/registrar.py stats "$vm_id"

# Host, once (after `just spire` and `just push`): the authorization gateway as a service
gateway:
    sudo gateway/install.sh

# Host: POC 4 done tests 1-4 on vm-01 (vm-02 plays a second VM), then M5. Test 5 = run it again 30 min later.
poc4:
    #!/usr/bin/env bash
    set -euo pipefail
    vm() { local n=$1; shift; sudo ssh -q -i /var/lib/avos/images/vm_key -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@172.16.$n.2 "$@"; }
    spawn() { curl -sf -X POST http://172.16.1.2/spawn -d "{\"name\":\"$1\",\"argv\":[\"/usr/bin/python3\",\"/opt/avos/demo_agent.py\",$2]}"; }
    field() { python3 -c "import json, sys; print(json.load(sys.stdin)['$1'])"; }
    stolen='--cert /root/s/svid.0.pem --key /root/s/svid.0.key -k -s -m 5 --data-binary @/root/s/req.json'
    sudo gateway/gateway.py check
    vm_id=$(sudo infra/vm create vm-01 | tee /dev/stderr | grep -oP 'vm_id=\K\S+')
    trap 'sudo infra/vm destroy vm-01; sudo infra/vm destroy vm-02' EXIT
    echo "== 0: no model key in the VM; the VM itself has no route out"
    vm 1 'T=$(curl -s -X PUT -H "X-metadata-token-ttl-seconds: 60" http://169.254.169.254/latest/api/token); curl -s -H "X-metadata-token: $T" http://169.254.169.254/avos/' | grep _KEY && echo "FAIL: key in MMDS" || echo "no key in MMDS: ok"
    vm 1 'curl -s -m 3 https://1.1.1.1 > /dev/null' && echo "FAIL: root reached the internet" || echo "root has no route out: ok"
    echo "== 1, 3: support and refund-manager call issue_refund; the manager's sub-agent is also named refund-manager"
    sup=$(spawn support "\"--egress\",\"1.1.1.1\",\"--refund\",\"15000\",\"--refund\",\"48000\"$(printf ',"--refund","100"%.0s' $(seq 20)),\"--sleep\",\"120\"")
    mgr=$(spawn refund-manager '"--refund","48000","--refund","150000","--then","70","--child","refund-manager=--refund 48000 --sleep 5","--sleep","10"')
    sup_id=$(field id <<< "$sup") mgr_uid=$(field uid <<< "$mgr")
    vm 1 "timeout 15 bash -c 'until [ -f /run/avos/$sup_id/svid.0.key ]; do sleep 0.1; done'"
    echo "== 3: an agent cannot mint itself a better identity"
    as_mgr="setpriv --reuid=$mgr_uid --regid=$mgr_uid --clear-groups"
    echo "agent -> registrar: $(vm 1 "$as_mgr curl -sk -m 5 https://172.16.1.1:8082/spawn -d '{}'" && echo FAIL || echo "refused at the TLS handshake (no VM SVID): ok")"
    echo "agent -> top-level spawn: $(vm 1 "$as_mgr curl -s -m 5 http://172.16.1.2/spawn -d '{}'")"
    echo "== 4: support's SVID, copied by root while support is alive"
    echo "another principal reading it: $(vm 1 "setpriv --reuid=$mgr_uid --regid=$mgr_uid --clear-groups cat /run/avos/$sup_id/svid.0.key" 2>&1 | tail -1)"
    vm 1 "mkdir -p /root/s && cp /run/avos/$sup_id/svid.0.* /root/s/ && echo '{\"order_id\": \"#W1\", \"amount_cents\": 101}' > /root/s/req.json"
    vm2_id=$(sudo infra/vm create vm-02 | grep -oP 'vm_id=\K\S+')
    vm 1 'tar -C /root -c s' | vm 2 'tar -C /root -x'
    echo "another VM using it: $(vm 2 "curl $stolen https://172.16.2.1:8443/tools/issue_refund" || true)"
    echo "== waiting 80 s: the manager repeats its calls after its SVID rotates; the copy expires"
    sleep 80
    vm 2 "curl $stolen https://172.16.2.1:8443/tools/issue_refund" && echo "FAIL: expired copy accepted" || echo "expired copy refused at the TLS handshake: ok"
    echo "== agents' own logs"
    vm 1 "journalctl -o cat -u avos-agent | grep -E 'refund 1[05]|refund 4|egress|after'"
    echo "== 1-4: authz.decision rows (tool and network; the VMs' own heartbeat model calls left out)"
    sudo python3 -c 'import json, sys
    for r in map(json.loads, open("/var/lib/avos/events.jsonl")):
        if r["kind"] == "authz.decision" and r["vm_id"] in sys.argv[1:] and r["action"] != "llm.call" and r["args"].get("amount_cents") != 100:
            print(r["decision"].ljust(5), r["principal"].split("/vm/")[1].ljust(52), r["action"], r["resource"], r["args"].get("amount_cents", ""), r["policy_ids"])' "$vm_id" "$vm2_id"
    echo "== gateway round trip (forwarder + mTLS + Cedar + log), 20 allowed calls"
    vm 1 "journalctl -o cat -u avos-agent | grep -oP 'refund 100: allowed \S+ \(\K\d+'" | sort -n | awk '{a[NR]=$1} END {print "median", a[int((NR+1)/2)], "ms, worst", a[NR], "ms"}'
    echo "guest agent (shim + forwarder) RSS: $(vm 1 'ps -o rss=,args= -C python3 | grep /agent.py' | awk '{print int($1/1024)}') MB"
    echo "== 2: M5"
    sudo gateway/gateway.py stats "$vm_id" "$vm2_id"
