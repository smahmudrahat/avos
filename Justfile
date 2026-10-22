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
    echo "== 1-4: authz.decision events (tool and network; the VMs' own heartbeat model calls left out)"
    for v in "$vm_id" "$vm2_id"; do sudo python3 collect/bin/avos trace --run "$v"; done | python3 -c 'import json, sys
    for r in map(json.loads, sys.stdin):
        if r["kind"] == "authz.decision" and r["action"] != "llm.call" and r["args"].get("amount_cents") != 100:
            print(r["decision"].ljust(5), r["principal"].split("/vm/")[1].ljust(52), r["action"], r["resource"], r["args"].get("amount_cents", ""), r["policy_ids"])'
    echo "== gateway round trip (forwarder + mTLS + Cedar + log), 20 allowed calls"
    vm 1 "journalctl -o cat -u avos-agent | grep -oP 'refund 100: allowed \S+ \(\K\d+'" | sort -n | awk '{a[NR]=$1} END {print "median", a[int((NR+1)/2)], "ms, worst", a[NR], "ms"}'
    echo "guest agent (shim + forwarder) RSS: $(vm 1 'ps -o rss=,args= -C python3 | grep /agent.py' | awk '{print int($1/1024)}') MB"
    echo "== 2: M5"
    sudo gateway/gateway.py stats "$vm_id" "$vm2_id"

# Host: the control table (`just ctl ls`, `just ctl halt ag_1234`, ...)
ctl *args:
    sudo supervisor/ctl.py {{args}}

# Host: POC 5 done tests 1-8 on vm-01 (vm-02 holds a sibling in another VM), then M6 over N halts and M7
poc5 n="10":
    #!/usr/bin/env bash
    set -euo pipefail
    vm() { local n=$1; shift; sudo ssh -q -i /var/lib/avos/images/vm_key -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@172.16.$n.2 "$@"; }
    spawn() { curl -sf -X POST http://172.16.$1.2/spawn -d "{\"name\":\"$2\",\"argv\":[\"/usr/bin/python3\",\"/opt/avos/demo_agent.py\",$3]}" | python3 -c 'import json, sys; print(json.load(sys.stdin)["spiffe_id"])'; }
    last() { vm $1 "journalctl -o cat -u avos-agent | grep '\"tick\"' | grep '$2' | tail -1" | python3 -c 'import json, sys; t = json.loads(sys.stdin.readline()); print(t["tick"], t.get("gw", ""), t.get("policy", ""))'; }
    frozen() { vm 1 "cat /sys/fs/cgroup/system.slice/avos-agent.service/$1/cgroup.events" | tr '\n' ' '; echo; }
    ctl() { sudo supervisor/ctl.py "$@"; }
    sudo gateway/gateway.py check
    sudo rm -f /var/lib/avos/cut.json
    vm1=$(sudo infra/vm create vm-01 | tee /dev/stderr | grep -oP 'vm_id=\K\S+')
    vm2=$(sudo infra/vm create vm-02 | grep -oP 'vm_id=\K\S+')
    trap 'sudo infra/vm destroy vm-01; sudo infra/vm destroy vm-02' EXIT
    tick='"--tick","0.05","--sleep","400"'
    echo "== tree: vm-01: A -> B -> B1, support, D, G, H; vm-02: E, J (all ticking every 50 ms; G, H, J are never named)"
    A=$(spawn 1 A "$tick,\"--child\",\"B=--tick 0.05 --sleep 400 --child 'B1=--tick 0.05 --sleep 400'\"")
    C=$(spawn 1 support "$tick"); D=$(spawn 1 D "$tick"); E=$(spawn 2 E "$tick")
    spawn 1 G "$tick" > /dev/null; spawn 1 H "$tick" > /dev/null; spawn 2 J "$tick" > /dev/null
    B="$A/sub/B"
    timeout 20 bash -c "until sudo identity/registrar.py who | grep -q sub/B1; do sleep 0.2; done"; sleep 3
    ctl ls
    b_dir=$(vm 1 "cd /sys/fs/cgroup/system.slice/avos-agent.service && ls -d */*/ | head -1 | sed 's:/$::'")   # A's id/B's id
    b1_dir=$(vm 1 "cd /sys/fs/cgroup/system.slice/avos-agent.service && ls -d */*/*/ | head -1 | sed 's:/$::'")
    echo "== 1: halt B by name; B and B1 freeze, the rest keep ticking"
    ctl halt "$B"
    echo "B: $(frozen $b_dir) B1: $(frozen $b1_dir)"
    b_seq=$(last 1 'sub/B"'); sleep 2; echo "B tick while halted: $b_seq -> $(last 1 'sub/B"') (same = frozen)"
    for who in 'agent/A"' agent/support agent/D; do echo "vm-01 $who: $(last 1 "$who")"; done; echo "vm-02 E: $(last 2 agent/E)"
    echo "== 2: resume B; no seq gap"
    ctl resume "$B"; sleep 1; echo "B: $(frozen $b_dir) now at tick $(last 1 'sub/B"')"
    echo "== 3: $(( {{n}} )) halt/resume repetitions on B (M6)"
    for i in $(seq {{n}}); do ctl halt "$B" | head -1; sleep 0.5; ctl resume "$B" > /dev/null; sleep 0.5; done
    echo "== 6: in-flight: F starts a long model call; halted mid-call, its answer is dropped"
    model=$(sudo grep -oP '^MODEL=\K.*' /var/lib/avos/agent.env)
    F=$(spawn 1 F "\"--slow-llm\",\"$model\",\"--tick\",\"0.05\",\"--sleep\",\"30\""); sleep 1.5
    ctl halt "$F"
    echo "waiting for the upstream answer to reach the gateway while F is frozen..."
    llm_of() { sudo python3 collect/bin/avos trace --principal "$1" | grep '"kind": "llm.call"' | tail -1; }
    timeout 150 bash -c "until sudo python3 collect/bin/avos trace --principal '$F' | grep -q '\"kind\": \"llm.call\"'; do sleep 0.5; done"
    llm_of "$F" | grep -oE '"status": [0-9]+|"ms": [0-9]+|"completion_tokens": [0-9]+|"dropped": \w+|"receipt": "hr_\w+"' | tr '\n' ' '; echo
    ctl resume "$F"; sleep 1.5
    vm 1 "journalctl -o cat -u avos-agent | grep agent/F | grep llm:" | cut -c1-200
    echo "== 6: network cut of support: its refunds flip to 403 naming the receipt; it keeps ticking; others unchanged"
    ctl cut "$C"; sleep 1.5; echo "support: $(last 1 agent/support)"; echo "D: $(last 1 agent/D)"
    ctl uncut "$C"; sleep 1.5; echo "support after uncut: $(last 1 agent/support)"
    echo "== 5: kill D; it exits with signal 9, entry gone, A still ticking"
    ctl kill "$D"; sleep 1; just who | grep -E 'agent/D|agent/A'; echo "A: $(last 1 'agent/A"')"
    echo "== 7: pause vm-02 by name; E stops, vm-01 unaffected; resume"
    e_seq=$(last 2 agent/E); ctl halt "$vm2"; sleep 2; echo "A during vm-02 pause: $(last 1 'agent/A"')"
    ctl resume "$vm2"; sleep 0.2; echo "E: $e_seq before the pause, $(last 2 agent/E) 2 s later (about 4 ticks = it was paused)"
    echo "== 8: an agent cannot use the controls"
    a_uid=$(sudo python3 collect/bin/avos trace --principal "$A" | grep '"agent.spawn"' | python3 -c 'import json, sys; print(json.loads(sys.stdin.read())["uid"])')
    echo "agent -> POST /halt: $(vm 1 "setpriv --reuid=$a_uid --regid=$a_uid --clear-groups curl -s -m 5 http://172.16.1.2/halt -d '{\"spiffe_id\":\"$B\"}'")"
    echo "agent -> own cgroup.freeze: $(vm 1 "setpriv --reuid=$a_uid --regid=$a_uid --clear-groups sh -c 'echo 1 > /sys/fs/cgroup/system.slice/avos-agent.service/$b_dir/cgroup.freeze'" 2>&1 | tail -1)"
    echo "== errors in the forwarder (guest) and the gateway (host) during the run"
    for n in 1 2; do vm $n "journalctl -o cat -u avos-agent | grep -B2 -A12 Traceback | tail -40"; done
    sudo journalctl -u avos-gateway --since "-15 min" -o cat | grep -v -E '^(Started|Stopping|Stopped)' | tail -20 || true  # no output is the good case
    echo "== M6 / M7"
    { vm 1 "journalctl -o cat -u avos-agent | grep '\"tick\"'"; vm 2 "journalctl -o cat -u avos-agent | grep '\"tick\"'"; } > /tmp/poc5-ticks.jsonl
    ctl stats /tmp/poc5-ticks.jsonl --tick 0.05
    echo "== receipts"
    for v in "$vm1" "$vm2"; do sudo python3 collect/bin/avos trace --run "$v"; done | python3 -c 'import json, sys
    for r in map(json.loads, sys.stdin):
        if r["kind"] in ("halt", "resume"):
            print(r["receipt"], r["kind"].ljust(6), r["action"].ljust(10), r["target"].split("/vm/")[1].ljust(50), len(r["nodes"]), "nodes", r["ms"], "ms")'
    sudo gateway/gateway.py stats "$vm1" "$vm2"
    echo "cgroups in vm-01: $(vm 1 'ls -d /sys/fs/cgroup/system.slice/avos-agent.service/*/ 2>/dev/null | wc -l') (one per live top-level agent: A, support, G, H, F)"

# Anywhere (no VM): the POC 6 acceptance suite without the 1M-event scale checks
trace-check:
    python3 trace/tests/run.py --quick

# Anywhere: POC 6 done tests on the real ledger window and the real run, then the full suite (≈ 3 min)
poc6:
    #!/usr/bin/env bash
    set -euo pipefail
    t=/tmp/poc6 f=trace/tests/fixtures run=run_retail-0_9 bin=trace/bin
    mkdir -p $t
    summary() { python3 -c 'import json, sys; j = json.load(sys.stdin); print({k: j[k] for k in sys.argv[1:]})' "$@"; }
    echo "== ingest: real ledger window (content off) and the real run (content on, priced)"
    python3 $bin/ingest --ledger $f/real_events.jsonl -o $t/ledger.jsonl
    AVOS_TRACE_CONTENT=1 python3 $bin/ingest --run-dir data/poc6/$run --prices $f/prices.json -o $t/run.jsonl
    AVOS_TRACE_CONTENT=0 python3 $bin/ingest --run-dir data/poc6/$run -o $t/run-nocontent.jsonl
    wc -l $t/ledger.jsonl $t/run.jsonl $t/run-nocontent.jsonl
    for x in ledger run run-nocontent; do
      echo "== $x: validate, verify, completeness"
      python3 $bin/validate $t/$x.jsonl | summary ok events errors
      python3 $bin/verify $t/$x.jsonl | summary ok events errors
      python3 $bin/completeness $t/$x.jsonl | summary avos_trace_events_stored_total avos_trace_events_expected_total avos_trace_completeness_ratio gaps
    done
    echo "== broken files fail (expected code@event_id from bad_cases.json)"
    for b in $f/golden/bad_*.jsonl; do
      printf '%-24s ' "$(basename $b .jsonl)"
      if [[ $b == *bad_chain* ]]; then python3 $bin/verify $b | python3 -c 'import json, sys; print(*sorted({e["code"] + "@" + e["event_id"] for e in json.load(sys.stdin)["errors"]}))' || true
      else python3 $bin/validate $b | python3 -c 'import json, sys; print(*sorted({e["code"] + "@" + str(e["event_id"]) for e in json.load(sys.stdin)["errors"]}))' || true; fi
    done
    echo "== audit: the first agent halted in the ledger window, with the authority of each event"
    p=$(python3 -c 'import json, sys; print(next(r["principal"] for r in map(json.loads, open(sys.argv[1])) if r["kind"] == "halt" and "/agent/" in r["principal"]))' $t/ledger.jsonl)
    python3 $bin/query audit --principal "$p" $t/ledger.jsonl | python3 -c 'import json, sys
    for e in map(json.loads, sys.stdin): print(e["ts"], e["kind"].ljust(14), json.dumps(e["authority"]))' | head -20
    echo "== lineage of a sub-agent"
    sub=$(python3 -c 'import json, sys; print(next(r["principal"] for r in map(json.loads, open(sys.argv[1])) if r["kind"] == "agent.spawn" and "/sub/" in r["principal"]))' $t/ledger.jsonl)
    python3 $bin/query lineage --principal "$sub" $t/ledger.jsonl
    echo "== cost of $run (gateway tokens x price)"
    python3 $bin/query cost --run $run $t/run.jsonl
    echo "== chat projection of $run"
    python3 $bin/project chat --run $run $t/run.jsonl | python3 -c 'import json, sys; j = json.load(sys.stdin)
    print("messages", len(j["messages"]), "tool calls", sum(len(m.get("tool_calls", [])) for m in j["messages"]), "reward", j["reward"], j["termination_reason"], "usage", j["usage"])'
    echo "== acceptance suite (full)"
    python3 trace/tests/run.py

# Host: claim 1 acceptance suite, written independently of the implementation (`just claim1 --quick` to iterate)
claim1 *args:
    sudo python3 supervisor/tests/claim1.py {{args}}

# Host: POC 7 acceptance suite (collect/tests, written independently): the tree, a τ² job, kills and cuts, M8, M9 at
# 1M imported events, overhead, then claim 1, poc4 and the POC 6 suite (≈ 30 min; `just poc7 --quick` to iterate)
poc7 *args:
    sudo python3 collect/tests/run.py {{args}}

# Host: POC 8 on the host's runs: evidence per run, `export --all` twice (byte-compared), the latest run in the
# viewer, statistics. Acceptance suite: from the workstation, `python3 retrain/tests/run.py [--quick] [--host]`.
poc8:
    #!/usr/bin/env bash
    set -uo pipefail
    t=/tmp/poc8; sudo rm -rf $t; mkdir -p $t
    rt() { sudo python3 retrain/bin/retrain "$@"; }
    echo "== runs: evidence per run"
    rt runs | python3 -c 'import json, sys, collections; r = json.load(sys.stdin); print(len(r), "runs", dict(collections.Counter(x["evidence"] for x in r)))'
    echo "== export --all, twice (exit 1 is expected: witness-only runs are skipped)"
    rt export --out $t/a --all; rt export --out $t/b --all > /dev/null
    for f in trajectories.jsonl sft.jsonl manifest.json stats.json; do sudo cmp $t/a/$f $t/b/$f && echo "$f identical"; done
    sudo python3 -c 'import json, sys, collections
    m = json.load(open(sys.argv[1]))
    print("skipped", dict(collections.Counter(s["reason"] for s in m["skipped"])), "counts", m["counts"], "M14", m["m14"])
    items = [json.loads(l) for l in open(sys.argv[2])]
    print("witness join", dict(collections.Counter(i["witness"]["join"] for i in items)),
          "checked", dict(collections.Counter(",".join(i["witness"]["checked"]) for i in items)))' $t/a/manifest.json $t/a/trajectories.jsonl
    echo "== statistics"
    sudo python3 -c 'import json, sys; s = json.load(open(sys.argv[1])); print({k: s[k] for k in ("items", "runs", "policy_turns_hist", "tool_call_share", "shortcut")}, "duplicates", s["exact_duplicates"]["items"])' $t/a/stats.json
    r=$(ls -t /var/lib/avos/results | head -1)
    echo "== viewer: $r (header, outcome, first messages)"
    rt view --run $r | head -24

# Host: one principal's audit timeline, or one run's (`just audit --principal SPIFFE_ID`, `just audit --run vm-01-abc123`)
audit *args:
    sudo python3 collect/bin/avos trace {{args}}

# Host: M7 baseline: the poc5 tree ticking SECS with no control at all, RUNS times (what happens anyway)
poc5-baseline runs="10" secs="120":
    #!/usr/bin/env bash
    set -euo pipefail
    vm() { local n=$1; shift; sudo ssh -q -i /var/lib/avos/images/vm_key -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@172.16.$n.2 "$@"; }
    spawn() { curl -sf -X POST http://172.16.$1.2/spawn -d "{\"name\":\"$2\",\"argv\":[\"/usr/bin/python3\",\"/opt/avos/demo_agent.py\",$3]}" > /dev/null; }
    out=/tmp/poc5-baseline; rm -rf $out; mkdir -p $out
    tick="\"--tick\",\"0.05\",\"--sleep\",\"$(( {{secs}} + 60 ))\""
    for r in $(seq {{runs}}); do
      trap 'sudo infra/vm destroy vm-01 >/dev/null; sudo infra/vm destroy vm-02 >/dev/null' EXIT
      sudo infra/vm create vm-01 > /dev/null; sudo infra/vm create vm-02 > /dev/null
      spawn 1 A "$tick,\"--child\",\"B=--tick 0.05 --sleep $(( {{secs}} + 60 )) --child 'B1=--tick 0.05 --sleep $(( {{secs}} + 60 ))'\""
      for a in support D G H; do spawn 1 $a "$tick"; done
      for a in E J; do spawn 2 $a "$tick"; done
      sleep {{secs}}
      { vm 1 "journalctl -o cat -u avos-agent | grep '\"tick\"'"; vm 2 "journalctl -o cat -u avos-agent | grep '\"tick\"'"; } > $out/ticks-$r.jsonl
      for n in 1 2; do vm $n "journalctl -o cat -u avos-agent | grep -B2 -A12 Traceback"; done > $out/errors-$r.txt || true
      sudo infra/vm destroy vm-01 > /dev/null; sudo infra/vm destroy vm-02 > /dev/null; trap - EXIT
      echo "run $r: $(sudo supervisor/ctl.py stats $out/ticks-$r.jsonl --tick 0.05 | grep '^M7 counts' | cut -c11-)"
    done
