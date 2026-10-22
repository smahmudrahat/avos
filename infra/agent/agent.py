"""Guest agent. Serves GET /health, runs the VM's SPIRE agent and the spawner shim. Two modes, chosen by the MMDS config:

- heartbeat (POC 1): calls the model in a loop.
- job (POC 2, HARNESS set): runs one harness trial once and serves its result at GET /result
  (202 while running, 200 + result file when done, 500 + log tail when failed).

Config comes from Firecracker MMDS under /avos (set by `vm create`, never written to disk):
VM_NAME, MODEL, INTERVAL_S; job mode adds HARNESS
(tau2 | mini-swe-agent), TASK (retail/0 | coding/slugify), JUDGE_MODEL, and optional USER_MODEL, SEED.
Identity (POC 3, from identity/registrar.py vm-up): VM_ID, JOIN_TOKEN, SPIRE_BUNDLE, SPIRE_SERVER, REGISTRAR.

Spawner shim (POC 3): the only way to start a principal, the job's harness included. Each one gets a fresh uid (never
reused in this VM) and a registration entry selecting that uid; the shim fetches its SVID into /run/avos/<id> as that
uid (M4) and waits on it to record its death. Top-level agents: POST /spawn on :80 (from the host) or the job;
sub-agents: one JSON line on /run/avos/spawn.sock, where the caller is known by its uid.
The VM itself holds an SVID for its own ID in /run/avos/vm, fetched as uid 20000 (no other process runs as it).

Egress forwarder (POC 4): the only way out of the VM. Every principal talks plain HTTP to 127.0.0.1:8090 (model calls
are just OPENAI_BASE_URL; anything else can use it as an HTTPS proxy). The forwarder names the caller by the uid that
owns the connection, and relays it to the host gateway (gateway/gateway.py) over mTLS with that principal's own SVID;
root's connections carry the VM's SVID. No model key is in this VM: the gateway attaches it to allowed calls.
The shim's calls to the registrar also carry the VM's SVID, which only root can read, so agents cannot register.
The forwarder reads the HTTP/1.1 requests it relays (keep-alive, Content-Length or chunked bodies; after CONNECT the
bytes are a tunnel) and stamps each with `X-Avos-Attempt: n`, n counted per principal here, before the gateway is even
reached. The gateway records n on its decision, so a missing n is a request lost between this VM and the host; the
final counts are on GET /health (`attempts`), read by the host before the VM is destroyed, and each principal's count
goes with its /exit to the registrar (POC 7, docs/architecture.md §7). Nothing in the VM writes to the trace.

Supervisor (POC 5): every principal runs in its own cgroup, nested like the identity tree (a sub-agent's cgroup is
inside its parent's), so freezing or killing one cgroup stops that whole branch and nothing else. The host's
control table (supervisor/ctl.py) drives it through POST /halt, /resume and /kill on :80, host-only like /spawn.
"""
import http.client
import itertools
import json
import os
import selectors
import shutil
import socket
import socketserver
import ssl
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
FWD_PORT = 8090
BASE = f"http://127.0.0.1:{FWD_PORT}/v1"  # the gateway forwards model calls upstream with the key
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
GATEWAY_ID = "spiffe://avoslab.com/gateway"
REGISTRAR_ID = "spiffe://avoslab.com/registrar"
SVID_REFRESH_S = 25  # principals' SVIDs live 60 s; the SPIRE agent rotates them at half-life
principals = {}  # uid -> registrar's agent.spawn row (+ cgroup: its branch's cgroup directory)
CG_BASE = "/sys/fs/cgroup" + open("/proc/self/cgroup").read().split("::", 1)[1].strip()  # this service's cgroup
uids = itertools.count(VM_UID + 1)
lock = threading.Lock()


def start_spire_agent():
    os.makedirs("/run/spire/data", exist_ok=True)
    open("/run/spire/bundle.pem", "w").write(ENV["SPIRE_BUNDLE"])
    open("/run/spire/agent.conf", "w").write(SPIRE_CONF % ENV["SPIRE_SERVER"])
    subprocess.Popen(["/opt/spire/bin/spire-agent", "run", "-config", "/run/spire/agent.conf", "-joinToken", ENV["JOIN_TOKEN"]])


def registrar(path, body):
    c = http.client.HTTPSConnection(ENV["SPIRE_SERVER"], 8082, context=client_ctx(0), timeout=10)
    try:
        c.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
        if dict(c.sock.getpeercert().get("subjectAltName", ())).get("URI") != REGISTRAR_ID:
            raise ValueError("registrar presented the wrong SPIFFE ID")
        r = c.getresponse()
        out = json.load(r)
    finally:
        c.close()
    if r.status != 200:
        raise ValueError(out["error"])
    return out


def private_dir(path, uid):
    os.makedirs(path, 0o700, exist_ok=True)  # exists already only for /run/avos/vm after a service restart
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
        above = find(parent)  # a sub-agent's cgroup sits inside its parent's: one freeze covers the branch
        p["cgroup"] = f"{above['cgroup'] if above else CG_BASE}/{p['id']}"
        principals[uid] = p
    return uid, p


def find(spiffe_id):
    return next((p for p in principals.values() if p["spiffe_id"] == spiffe_id), None)


def branch(spiffe_id):
    return [p["spiffe_id"] for p in principals.values() if p["spiffe_id"] == spiffe_id or p["spiffe_id"].startswith(spiffe_id + "/")]


def cgroup(spiffe_id, file):
    p = find(spiffe_id)
    if not p:
        raise LookupError(f"{spiffe_id} is not live here")
    return f"{p['cgroup']}/{file}"


def halt(spiffe_id):
    t0 = time.time()
    open(cgroup(spiffe_id, "cgroup.freeze"), "w").write("1")
    while "frozen 1" not in open(cgroup(spiffe_id, "cgroup.events")).read():  # M6 is measured on this edge
        if time.time() - t0 > 5:
            raise TimeoutError("not frozen after 5 s")
        time.sleep(0.001)
    return {"freeze_ms": round((time.time() - t0) * 1000, 1), "nodes": branch(spiffe_id)}


def resume(spiffe_id):
    open(cgroup(spiffe_id, "cgroup.freeze"), "w").write("0")
    return {"nodes": branch(spiffe_id)}


def kill(spiffe_id):
    nodes = branch(spiffe_id)
    open(cgroup(spiffe_id, "cgroup.kill"), "w").write("1")  # SIGKILL to the whole branch; each exit is ledgered
    return {"nodes": nodes}


def spawn(name, parent, argv):
    uid, p = register(name, parent)
    threading.Thread(target=run_principal, args=(uid, p, argv), daemon=True).start()
    return p


def record_exit(p, reason, svid_ms):
    """Retry until the registrar answers (it may be restarting); an exit it never hears of is printed here and ends up
    as `vm destroyed` at teardown."""
    body = {"spiffe_id": p["spiffe_id"], "reason": reason, "svid_ms": svid_ms, "attempts": attempts.get(p["spiffe_id"], 0)}
    for i in range(40):
        try:
            return registrar("/exit", body)
        except ValueError:
            return None  # answered with a refusal (already recorded): nothing to retry
        except OSError as e:
            err = e
            time.sleep(min(2.0, 0.1 * 2 ** i))
    print(json.dumps({"vm": VM, "exit_unrecorded": p["spiffe_id"], "error": repr(err)}), flush=True)


def run_principal(uid, p, argv, env=None, cwd=None, out=None):
    # Its own uid and a private dir for its SVID. Its stdout joins this service's journal (or `out`); each line names its writer.
    t0 = time.time()
    home = private_dir(f"/run/avos/{p['id']}", uid)
    svid = {}
    fetch = threading.Thread(target=lambda: svid.update(ms=fetch_svid(uid, home, t0)), daemon=True)
    fetch.start()  # in parallel: harnesses never ask for their SVID, and they need not wait for it
    base = {"PATH": "/usr/bin:/bin", "HOME": home, "AVOS_DIR": home, "AVOS_SPAWN": SPAWN_SOCK, "AVOS_T0": str(t0)}
    # Into its cgroup as root first (Popen's user= would switch before preexec_fn could), then drop to its uid.
    os.makedirs(p["cgroup"])
    enter = 'echo $$ > "$0/cgroup.procs" && u=$1 && shift && exec setpriv --reuid="$u" --regid="$u" --clear-groups -- "$@"'
    rc = subprocess.Popen(["/bin/sh", "-c", enter, p["cgroup"], str(uid), *argv], env={**base, **(env or {})},
                          cwd=cwd or home, stdin=subprocess.DEVNULL, stdout=out, stderr=out and subprocess.STDOUT).wait()
    fetch.join()  # before /exit deletes the entry
    record_exit(p, f"signal {-rc}" if rc < 0 else f"exit {rc}", svid.get("ms"))
    shutil.rmtree(home, ignore_errors=True)
    with lock:
        del principals[uid]
    cg = p["cgroup"]
    while cg != CG_BASE:  # a parent's cgroup goes when its last child has
        try:
            os.rmdir(cg)
        except OSError:
            break
        cg = os.path.dirname(cg)
    return rc


def vm_svid():
    # Limit: fetched once (TTL 1 h, longer than any job); re-fetch before expiry if VMs start living longer.
    t0 = time.time()
    ms = fetch_svid(VM_UID, "/run/avos/vm", t0)
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
        headers={"Content-Type": "application/json"},
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
    env = {**HARNESS_ENV, "HOME": WORK, "TAU2_DATA_DIR": data, "TAU2_NL_JUDGE": f"openai/{ENV['JUDGE_MODEL']}"}
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
    env = {**HARNESS_ENV, "HOME": WORK, "MSWEA_COST_TRACKING": "ignore_errors",
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
# LiteLLM wants some key; the real one is attached by the gateway. Its model price map would be fetched from the
# internet at import, which no VM can reach.
HARNESS_ENV = {"PATH": "/usr/bin:/bin", "OPENAI_API_KEY": "via-gateway", "OPENAI_BASE_URL": BASE,
               "OPENAI_API_BASE": BASE, "LITELLM_LOCAL_MODEL_COST_MAP": "True"}


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


svids = {}  # uid -> (client TLS context with its SVID, fetched at)
attempts = {}  # principal -> requests the forwarder accepted from it (M8 guest_requests); kept for this process's life
attempts_lock = threading.Lock()


def bump(who):
    with attempts_lock:
        n = attempts[who] = attempts.get(who, 0) + 1
    return n


class Stamper:
    """The agent -> gateway half of a connection, request by request: each request head gets `X-Avos-Attempt: n`
    (an agent's own value is dropped), n counted when the head is complete. Bodies are passed by Content-Length or
    chunk framing; after CONNECT (or a head that never ends) the rest is passed through untouched."""

    def __init__(self, who):
        self.who, self.buf, self.state, self.left = who, b"", "head", 0

    def feed(self, data):
        self.buf += data
        out = []
        while self.buf:
            if self.state == "tunnel":
                out.append(self.buf)
                self.buf = b""
            elif self.left:
                n = min(self.left, len(self.buf))
                out.append(self.buf[:n])
                self.buf, self.left = self.buf[n:], self.left - n
            elif self.state == "head":
                if self.buf.startswith(b"\r\n"):
                    self.buf = self.buf[2:]
                    continue
                i = self.buf.find(b"\r\n\r\n")
                if i < 0:
                    if len(self.buf) > 65536:
                        self.state = "tunnel"
                        continue
                    break
                lines, self.buf = self.buf[:i].split(b"\r\n"), self.buf[i + 4:]
                head, fields = [lines[0]], {}
                for line in lines[1:]:
                    k, _, v = line.partition(b":")
                    if k.strip().lower() != b"x-avos-attempt":
                        head.append(line)
                        fields[k.strip().lower()] = v.strip().lower()
                out.append(b"\r\n".join(head + [b"X-Avos-Attempt: %d" % bump(self.who)]) + b"\r\n\r\n")
                if lines[0].split(b" ", 1)[0].upper() == b"CONNECT":
                    self.state = "tunnel"
                elif b"chunked" in fields.get(b"transfer-encoding", b""):
                    self.state = "size"
                else:
                    self.left = int(fields.get(b"content-length", b"0"))
            else:  # chunk-size line (then its data + CRLF as `left`), or trailer lines up to an empty one
                i = self.buf.find(b"\r\n")
                if i < 0:
                    break
                line, self.buf = self.buf[:i + 2], self.buf[i + 2:]
                out.append(line)
                if self.state == "size":
                    n = int(line.split(b";")[0], 16)
                    self.left, self.state = (n + 2, "size") if n else (0, "trailer")
                elif line == b"\r\n":
                    self.state = "head"
        return b"".join(out)


def peer_uid(port):
    # The client end of a loopback connection to the forwarder, as listed in /proc/net/tcp, carries its owner's uid.
    local, remote = f"0100007F:{port:04X}", f"0100007F:{FWD_PORT:04X}"
    for line in open("/proc/net/tcp").readlines()[1:]:
        f = line.split()
        if f[1] == local and f[2] == remote:
            return int(f[7])


def client_ctx(uid):
    if uid == 0:
        path, as_uid = "/run/avos/vm", VM_UID  # root is the VM's own control plane: it acts as the VM
    elif uid in principals:
        path, as_uid = f"/run/avos/{principals[uid]['id']}", uid
    else:
        return None
    ctx, at = svids.get(uid, (None, 0))
    if time.time() - at > SVID_REFRESH_S:
        if fetch_svid(as_uid, path, time.time()) is None:
            return None
        ctx = ssl.create_default_context(cafile=f"{path}/bundle.0.pem")
        ctx.check_hostname = False  # the gateway is checked by SPIFFE ID below, not by DNS name
        ctx.load_cert_chain(f"{path}/svid.0.pem", f"{path}/svid.0.key")
        svids[uid] = (ctx, time.time())
    return ctx


def pipe(a, b, a_to_b=bytes):
    """Copy bytes both ways until either side closes, from this one thread; what `a` sends goes through a_to_b. A TLS
    socket must never be read by one thread while another writes it (OpenSSL objects are not thread-safe): with a
    thread per direction, a TLS 1.3 session ticket arriving during the write lost the request about once in 2,600
    calls, and the caller hung."""
    peer = {a: b, b: a}
    sel = selectors.DefaultSelector()
    try:
        for s in peer:
            s.setblocking(False)
            sel.register(s, selectors.EVENT_READ)
        while True:
            # Bytes a TLS socket has already decrypted and buffered do not wake the selector.
            ready = [s for s in peer if isinstance(s, ssl.SSLSocket) and s.pending()] or \
                [k.fileobj for k, _ in sel.select()]
            for src in ready:
                try:
                    data = src.recv(65536)
                except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
                    continue  # TLS records that carry no data, such as a session ticket
                if not data:
                    return
                if src is a:
                    data = a_to_b(data)
                send_all(peer[src], data)
    except OSError:
        pass
    finally:
        sel.close()
        for s in peer:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def send_all(s, data):
    view = memoryview(data)
    while view:
        try:
            view = view[s.send(view):]
        except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
            with selectors.DefaultSelector() as w:
                w.register(s, selectors.EVENT_READ | selectors.EVENT_WRITE)
                if not w.select(30):
                    raise TimeoutError("peer stopped reading for 30 s")


class Forward(socketserver.BaseRequestHandler):
    def handle(self):
        uid = peer_uid(self.client_address[1])
        who = ENV["VM_SPIFFE_ID"] if uid == 0 else principals[uid]["spiffe_id"] if uid in principals else None
        if not who:
            body = json.dumps({"error": f"uid {uid} is not a principal"}).encode()
            self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
                                 b"Connection: close\r\n\r\n%s" % (len(body), body))
            return
        # The first request is read (and counted) before the gateway is dialled: if the host cannot be reached, the
        # attempt still exists here and shows up missing there.
        stamp, first = Stamper(who), b""
        self.request.settimeout(30)
        while not first:
            data = self.request.recv(65536)
            if not data:
                return
            first = stamp.feed(data)
        ctx = client_ctx(uid)
        if not ctx:
            return
        with ctx.wrap_socket(socket.create_connection((ENV["SPIRE_SERVER"], 8443), timeout=10)) as up:
            san = dict(up.getpeercert().get("subjectAltName", ()))
            if san.get("URI") != GATEWAY_ID:
                return
            up.settimeout(None)
            send_all(up, first)
            pipe(self.request, up, stamp.feed)


def serve_forwarder():
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer.daemon_threads = True
    socketserver.ThreadingTCPServer(("127.0.0.1", FWD_PORT), Forward).serve_forever()


class Health(BaseHTTPRequestHandler):
    CONTROL = {"/halt": halt, "/resume": resume, "/kill": kill}

    def do_POST(self):
        if self.path != "/spawn" and self.path not in self.CONTROL:
            return self.send_error(404)
        if self.client_address[0] != ENV["SPIRE_SERVER"]:  # spawns and controls come from the host, not from agents
            return self.reply(403, b'{"error": "top-level spawns and controls come from the host"}')
        try:
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/spawn":
                out = spawn(req["name"], None, req["argv"])
            else:
                out = self.CONTROL[self.path](req["spiffe_id"])
            self.reply(200, json.dumps(out).encode())
        except Exception as e:
            self.reply(400, json.dumps({"error": repr(e)}).encode())

    def do_GET(self):
        if self.path == "/health":
            with attempts_lock:
                counts = dict(attempts)
            body = {"vm": VM, "status": "alive", "uptime_s": round(time.time() - START, 1), "model": MODEL, **state,
                    "attempts": counts, "restarted": RESTARTED}
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
# /run is per boot: if the marker exists, this process is a restart and its counters miss what came before.
RESTARTED = os.path.exists("/run/avos/agent.started")
open("/run/avos/agent.started", "a").close()
private_dir("/run/avos/vm", VM_UID)
start_spire_agent()
threading.Thread(target=vm_svid, daemon=True).start()
threading.Thread(target=serve_shim, daemon=True).start()
threading.Thread(target=serve_forwarder, daemon=True).start()
threading.Thread(target=run_job if "job" in state else loop, daemon=True).start()
ThreadingHTTPServer(("0.0.0.0", 80), Health).serve_forever()
