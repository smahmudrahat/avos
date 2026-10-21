# POC targets. `push` runs on your Mac; the rest run on the host (ssh avos-dev, cd ~/poc).
host := "avos-dev"

base_url := "https://api.tokenfactory.nebius.com/v1"
model := "Qwen/Qwen3-30B-A3B-Instruct-2507"
judge := "deepseek-ai/DeepSeek-V4-Flash-0731"

# Mac: copy the repo and the model config (Nebius key, base URL, model) to the host
push:
    rsync -az --delete --exclude .git --exclude .env --exclude .venv --exclude .serena ./ {{host}}:poc/
    { grep '^nebulus_API_KEY=' .env | sed 's/^nebulus_API_KEY=/OPENAI_API_KEY=/'; echo "OPENAI_BASE_URL={{base_url}}"; echo "MODEL={{model}}"; echo "JUDGE_MODEL={{judge}}"; } \
      | ssh {{host}} 'sudo install -D -m 600 /dev/stdin /var/lib/avos/agent.env'

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

# Host: POC 1 done tests 1-5 (create, /health, model calls on the tap, destroy, twice)
poc1:
    #!/usr/bin/env bash
    set -euo pipefail
    for round in 1 2; do
      echo "== round $round"
      sudo infra/vm create vm-01
      curl -sf http://172.16.1.2/health; echo
      sudo timeout 25 tcpdump -ni tap-vm-01 -c 6 'tcp port 443' 2>/dev/null
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
