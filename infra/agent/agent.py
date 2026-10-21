"""Guest agent. Serves GET /health, runs the VM's SPIRE agent and the spawner shim. Two modes, chosen by the MMDS config:

- heartbeat (POC 1): calls the model in a loop.
- job (POC 2, HARNESS set): runs one harness trial once and serves its result at GET /result
  (202 while running, 200 + result file when done, 500 + log tail when failed).

Config comes from Firecracker MMDS under /avos (set by `vm create`, never written to disk):
VM_NAME, OPENAI_API_KEY, OPENAI_BASE_URL, MODEL, INTERVAL_S; job mode adds HARNESS
(tau2 | mini-swe-agent), TASK (retail/0 | coding/slugify), JUDGE_MODEL, and optional USER_MODEL, SEED.
Identity (POC 3, from identity/registrar.py vm-up): VM_ID, JOIN_TOKEN, SPIRE_BUNDLE, SPIRE_SERVER, REGISTRAR.

Spawner shim (POC 3): the only way to start a principal, the job's harness included. Each one gets a fresh uid (never
reused in this VM) and a registration entry selecting that uid; the shim fetches its SVID into /run/avos/<id> as that
uid (M4) and waits on it to record its death. Top-level agents: POST /spawn on :80 (from the host) or the job;
sub-agents: one JSON line on /run/avos/spawn.sock, where the caller is known by its uid.
The VM itself holds an SVID for its own ID in /run/avos/vm, fetched as uid 20000 (no other process runs as it).
"""
import itertools
import json
import os
import shutil
import socket
import socketserver
import struct
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MMDS = "http://169.254.169.254"


def mmds_config():
    # MMDS V2: get a session token, then read the /avos object. Retry while the network comes up.
    subprocess.run(["ip", "route", "replace", "169.254.169.254", "dev", "eth0"], check=False)
    for _ in range(100):
        try:
            put = urllib.request.Request(f"{MMDS}/latest/api/token", method="PUT", headers={"X-metadata-token-ttl-seconds": "60"})
            token = urllib.request.urlopen(put, timeout=1).read().decode()
            get = urllib.request.Request(f"{MMDS}/avos", headers={"X-metadata-token": token, "Accept": "application/json"})
            return json.load(urllib.request.urlopen(get, timeout=1))
        except OSError:
            time.sleep(0.1)
    raise SystemExit("no config from MMDS")


ENV = mmds_config()
VM = ENV.get("VM_NAME", "unknown")
BASE = ENV.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
MODEL = ENV.get("MODEL", "gpt-5.4-nano")
INTERVAL = float(ENV.get("INTERVAL_S", "10"))
START = time.time()
HARNESS_DIR = "/opt/harness"  # read-only disk shared by all VMs
WORK = "/var/avos"  # writable, on this VM's own disk
state = {"job": "running", "exit_code": None} if "HARNESS" in ENV else {"beats": 0, "llm_calls": 0, "llm_errors": 0, "last_llm": None}
SPIRE_CONF = """agent {
  data_dir = "/run/spire/data"
  log_level = "WARN"
  server_address = "%s"
  server_port = "8081"
  socket_path = "/run/spire/agent.sock"
  trust_bundle_path = "/run/spire/bundle.pem"
  trust_domain = "avoslab.com"
  experimental { sync_interval = "1s" }
}
plugins {
  NodeAttestor "join_token" { plugin_data {} }
  KeyManager "memory" { plugin_data {} }
  WorkloadAttestor "unix" { plugin_data {} }
}
"""
SPAWN_SOCK = "/run/avos/spawn.sock"
VM_UID = 20000  # holds the VM's own SVID
principals = {}  # uid -> registrar's agent.spawn row
uids = itertools.count(VM_UID + 1)
lock = threading.Lock()


def start_spire_agent():
    os.makedirs("/run/spire/data", exist_ok=True)
    open("/run/spire/bundle.pem", "w").write(ENV["SPIRE_BUNDLE"])
    open("/run/spire/agent.conf", "w").write(SPIRE_CONF % ENV["SPIRE_SERVER"])
    subprocess.Popen(["/opt/spire/bin/spire-agent", "run", "-config", "/run/spire/agent.conf", "-joinToken", ENV["JOIN_TOKEN"]])


def registrar(path, body):
    req = urllib.request.Request(ENV["REGISTRAR"] + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=10))
    except urllib.error.HTTPError as e:
        raise ValueError(json.load(e)["error"]) from None


def private_dir(path, uid):
    os.makedirs(path, 0o700)
    os.chown(path, uid, uid)
    return path


def fetch_svid(uid, path, t0):
    # Asks the Workload API as `uid`, so SPIRE attests that uid. New entries reach this VM's SPIRE agent in ~1 s.
    while time.time() - t0 < 30:
        r = subprocess.run(["/opt/spire/bin/spire-agent", "api", "fetch", "x509", "-socketPath", "/run/spire/agent.sock",
                            "-write", path], user=uid, group=uid, extra_groups=[], capture_output=True)
        if r.returncode == 0:
            return round((time.time() - t0) * 1000)
        time.sleep(0.05)


def register(name, parent):
    # Entry first, process second: the principal can use its SVID as soon as it runs.
    with lock:
        uid = next(uids)
        p = registrar("/spawn", {"name": name, "parent": parent, "uid": uid})
        principals[uid] = p
    return uid, p


def spawn(name, parent, argv):
    uid, p = register(name, parent)
    threading.Thread(target=run_principal, args=(uid, p, argv), daemon=True).start()
    return p


def run_principal(uid, p, argv, env=None, cwd=None, out=None):
    # Its own uid and a private dir for its SVID. Its stdout joins this service's journal (or `out`); each line names its writer.
    t0 = time.time()
    home = private_dir(f"/run/avos/{p['id']}", uid)
    svid = {}
    fetch = threading.Thread(target=lambda: svid.update(ms=fetch_svid(uid, home, t0)), daemon=True)
    fetch.start()  # in parallel: harnesses never ask for their SVID, and they need not wait for it
    base = {"PATH": "/usr/bin:/bin", "HOME": home, "AVOS_DIR": home, "AVOS_SPAWN": SPAWN_SOCK, "AVOS_T0": str(t0)}
    rc = subprocess.Popen(argv, user=uid, group=uid, extra_groups=[], env={**base, **(env or {})}, cwd=cwd or home,
                          stdin=subprocess.DEVNULL, stdout=out, stderr=out and subprocess.STDOUT).wait()
    fetch.join()  # before /exit deletes the entry
    reason = f"signal {-rc}" if rc < 0 else f"exit {rc}"
    registrar("/exit", {"spiffe_id": p["spiffe_id"], "reason": reason, "svid_ms": svid.get("ms")})
    shutil.rmtree(home, ignore_errors=True)
    with lock:
        del principals[uid]
    return rc


def vm_svid():
    # Limit: fetched once (TTL 1 h, longer than any job); re-fetch before expiry if VMs start living longer.
    t0 = time.time()
    ms = fetch_svid(VM_UID, private_dir("/run/avos/vm", VM_UID), t0)
    state["vm_svid"] = {"spiffe_id": ENV["VM_SPIFFE_ID"], "ms": ms} if ms is not None else "none within 30 s"


class Shim(socketserver.StreamRequestHandler):
    def handle(self):
        cred = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        caller = principals.get(struct.unpack("3i", cred)[1])
        try:
            req = json.loads(self.rfile.readline())
            if not caller:
                out = {"error": "caller is not a principal; start it through the shim"}
            elif req["op"] == "spawn":
                out = spawn(req["name"], caller["spiffe_id"], req["argv"])
            else:
                out = {"error": f"unknown op {req['op']!r}"}
        except Exception as e:
            out = {"error": repr(e)}
        self.wfile.write(json.dumps(out).encode() + b"\n")


def serve_shim():
    server = socketserver.ThreadingUnixStreamServer(SPAWN_SOCK, Shim)
    os.chmod(SPAWN_SOCK, 0o666)
    server.serve_forever()


def call_model():
    req = urllib.request.Request(
        f"{BASE}/chat/completions",
        data=json.dumps({"model": MODEL, "messages": [{"role": "user", "content": "Reply with one word: alive"}]}).encode(),
        headers={"Authorization": f"Bearer {ENV['OPENAI_API_KEY']}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["choices"][0]["message"]["content"]


def loop():
    while True:
        try:
            state["last_llm"] = call_model()
            state["llm_calls"] += 1
        except Exception as e:
            state["llm_errors"] += 1
            state["last_llm"] = f"error: {e}"
        state["beats"] += 1
        print(json.dumps({"vm": VM, **state}), flush=True)
        time.sleep(INTERVAL)


def tau2_job():
    # One τ²-bench trial. Its data dir = read-only domains on the harness disk + writable simulations/.
    domain, task = ENV["TASK"].split("/", 1)
    data = f"{WORK}/tau2"
    os.makedirs(data, exist_ok=True)
    if not os.path.lexists(f"{data}/tau2"):
        os.symlink(f"{HARNESS_DIR}/tau2-bench/data/tau2", f"{data}/tau2")
    llm_args = json.dumps({"api_base": BASE, "temperature": 0})
    cmd = [f"{HARNESS_DIR}/tau2-bench/.venv/bin/tau2", "run", "--domain", domain, "--task-ids", task,
           "--num-trials", "1", "--max-concurrency", "1", "--seed", ENV.get("SEED", "300"), "--save-to", "job",
           "--agent-llm", f"openai/{MODEL}", "--agent-llm-args", llm_args,
           "--user-llm", f"openai/{ENV.get('USER_MODEL', MODEL)}", "--user-llm-args", llm_args]
    # The base URL env vars are LiteLLM's default endpoint: the NL-assertion judge (TAU2_NL_JUDGE, patched in at
    # image build) takes no llm_args, and anything else that skips them fails at our provider instead of
    # sending the key to OpenAI.
    env = {"PATH": "/usr/bin:/bin", "HOME": WORK, "TAU2_DATA_DIR": data, "OPENAI_API_KEY": ENV["OPENAI_API_KEY"],
           "OPENAI_BASE_URL": BASE, "OPENAI_API_BASE": BASE, "TAU2_NL_JUDGE": f"openai/{ENV['JUDGE_MODEL']}"}
    return cmd, env, lambda: f"{data}/simulations/job/results.json"


def mini_swe_agent_job():
    # One coding task: fix a copy of tasks/<name> (TASK.md, test.sh) until test.sh passes.
    # Result = diff + test outcome + trajectory.
    src = f"{HARNESS_DIR}/tasks/{ENV['TASK'].split('/', 1)[1]}"
    work = f"{WORK}/work"
    shutil.copytree(src, work)
    cmd = [f"{HARNESS_DIR}/mini-swe-agent/.venv/bin/mini", "-y", "--exit-immediately", "-l", "0",
           "-m", f"openai/{MODEL}", "-t", open(f"{src}/TASK.md").read(), "-o", f"{WORK}/trajectory.json",
           "-c", "mini.yaml", "-c", f"environment.cwd={work}", "-c", "model.model_kwargs.temperature=0",
           "-c", "agent.step_limit=30"]
    env = {"PATH": "/usr/bin:/bin", "HOME": WORK, "OPENAI_API_KEY": ENV["OPENAI_API_KEY"],
           "OPENAI_BASE_URL": BASE, "OPENAI_API_BASE": BASE, "MSWEA_COST_TRACKING": "ignore_errors",
           "MSWEA_SILENT_STARTUP": "1", "MSWEA_CONFIGURED": "true", "MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT": "3"}

    def finish():
        for f in os.listdir(src):  # grade with the original tests, whatever the agent did to its copies
            if f.startswith("test"):
                shutil.copy(f"{src}/{f}", work)
        test = subprocess.run(["bash", "test.sh"], cwd=work, capture_output=True, text=True, timeout=60)
        diff = subprocess.run(["diff", "-ruN", "--exclude=__pycache__", src, work], capture_output=True, text=True)
        traj = json.load(open(f"{WORK}/trajectory.json"))
        result = {"task": ENV["TASK"], "tests_passed": test.returncode == 0,
                  "test_output": (test.stdout + test.stderr)[-2000:], "diff": diff.stdout,
                  "exit_status": traj["info"].get("exit_status"), "model_stats": traj["info"].get("model_stats"),
                  "trajectory": traj}
        json.dump(result, open(f"{WORK}/result.json", "w"))
        return f"{WORK}/result.json"

    return cmd, env, finish


HARNESSES = {"tau2": tau2_job, "mini-swe-agent": mini_swe_agent_job}


def run_job():
    # The harness is a principal like any other: spiffe://.../vm/<vm id>/agent/<harness>, its own uid, owning WORK.
    os.makedirs(WORK, exist_ok=True)
    with open(f"{WORK}/job.log", "w") as log:
        try:
            cmd, env, finish = HARNESSES[ENV["HARNESS"]]()
            uid, p = register(ENV["HARNESS"], None)
            state["spiffe_id"] = p["spiffe_id"]
            subprocess.run(["chown", "-R", f"{uid}:{uid}", WORK], check=True)
            state["exit_code"] = run_principal(uid, p, cmd, env, WORK, log)
            state["result_path"] = finish()
        except Exception as e:
            print(f"job error: {e!r}", file=log)
    ok = state["exit_code"] == 0 and os.path.exists(state.get("result_path", ""))
    state["job"] = "done" if ok else "failed"


class Health(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/spawn":
            return self.send_error(404)
        try:
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.reply(200, json.dumps(spawn(req["name"], None, req["argv"])).encode())
        except Exception as e:
            self.reply(400, json.dumps({"error": repr(e)}).encode())

    def do_GET(self):
        if self.path == "/health":
            body = {"vm": VM, "status": "alive", "uptime_s": round(time.time() - START, 1), "model": MODEL, **state}
            self.reply(200, json.dumps(body).encode())
        elif self.path == "/result" and "job" in state:
            if state["job"] == "running":
                self.reply(202, b"running")
            elif state["job"] == "done":
                self.reply(200, open(state["result_path"], "rb").read())
            else:
                self.reply(500, open(f"{WORK}/job.log", "rb").read()[-4000:])
        else:
            self.send_error(404)

    def reply(self, code, body):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


os.makedirs("/run/avos", exist_ok=True)
start_spire_agent()
threading.Thread(target=vm_svid, daemon=True).start()
threading.Thread(target=serve_shim, daemon=True).start()
threading.Thread(target=run_job if "job" in state else loop, daemon=True).start()
ThreadingHTTPServer(("0.0.0.0", 80), Health).serve_forever()
