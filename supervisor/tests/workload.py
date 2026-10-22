#!/usr/bin/env python3
"""Guest-side workload for the claim 1 suite. Shipped into each VM's /tmp and started as a principal through the
spawner shim (host POST /spawn, or a parent via /run/avos/spawn.sock op=spawn). stdlib only.

Usage: python3 /tmp/workload.py '<cfg-json>'

cfg keys (all optional unless noted):
  log         (req) path to this principal's own JSONL log, e.g. /tmp/claim1/B.jsonl
  tick        tick period in seconds; if set, logs {"k":"tick","t":<wall>,"n":seq} forever
  call        {"kind":"tool","cents":100} or {"kind":"llm","model":..,"max_tokens":..}
  call_every  seconds between calls (0 = back to back); requires `call`
  calls_max   stop after this many calls (default: forever)
  children    [{"name":..,"cfg":{..}}] spawned through the shim as this principal's children
  daemon_file if set, double-fork a setsid daemon that bumps this counter file every 10 ms
  forker_file if set, spawn a fresh short-lived child every 10 ms, each appends one line
  intruder    {"victim_cg":..,"victim_run":..,"self_cg":..,"guest_ip":..} run the control-escape attempts
  self        path to this file in the guest (for spawning children); default /tmp/workload.py
Every principal first logs {"k":"meta","t":<wall>,"pid":..,"cg":<own cgroup path>,"spiffe":<or "">}.
"""
import json, os, socket, sys, threading, time, urllib.request, urllib.error

cfg = json.loads(sys.argv[1])
SELF = cfg.get("self", "/tmp/workload.py")
GW = "http://127.0.0.1:8090"
_log = open(cfg["log"], "a", buffering=1)
_lk = threading.Lock()


def rec(**kw):
    kw.setdefault("t", time.time())
    with _lk:
        _log.write(json.dumps(kw) + "\n")


def own_cgroup():
    line = open("/proc/self/cgroup").read().strip()
    return "/sys/fs/cgroup" + line.split("::")[-1]


def shim(op):
    s = socket.socket(socket.AF_UNIX)
    s.connect("/run/avos/spawn.sock")
    s.sendall(json.dumps(op).encode())
    s.shutdown(socket.SHUT_WR)
    b = b""
    while True:
        c = s.recv(65536)
        if not c:
            break
        b += c
    s.close()
    return b.decode(errors="replace")


def http(url, body, timeout):
    t0 = time.time()
    try:
        r = urllib.request.urlopen(
            urllib.request.Request(url, data=json.dumps(body).encode(),
                                   headers={"Content-Type": "application/json"}),
            timeout=timeout)
        return t0, time.time(), r.status, r.read()[:160].decode(errors="replace")
    except urllib.error.HTTPError as e:
        return t0, time.time(), e.code, e.read()[:160].decode(errors="replace")
    except Exception as e:
        return t0, time.time(), "EXC:" + type(e).__name__, str(e)[:120]


def one_call():
    c = cfg["call"]
    if c["kind"] == "tool":
        return http(GW + "/tools/issue_refund", {"order_id": "#W1", "amount_cents": c.get("cents", 100)}, 30)
    return http(GW + "/v1/chat/completions",
                {"model": c["model"], "max_tokens": c["max_tokens"],
                 "messages": [{"role": "user", "content": "Write a 1500 word essay about rivers."}]}, 300)


def run_caller():
    n, every, mx = 0, cfg.get("call_every") or 0, cfg.get("calls_max")
    while mx is None or n < mx:
        t0, t1, st, b = one_call()
        n += 1
        rec(k="call", n=n, t0=t0, t1=t1, st=st, b=b)
        if every:
            time.sleep(every)
    rec(k="calls_done", n=n)


def run_ticker():
    n = 0
    while True:
        n += 1
        rec(k="tick", n=n)
        time.sleep(cfg["tick"])


def run_daemon():
    path = cfg["daemon_file"]
    if os.fork() == 0:
        os.setsid()
        if os.fork() == 0:
            n = 0
            while True:
                n += 1
                with open(path, "w") as f:
                    f.write("%d %f\n" % (n, time.time()))
                time.sleep(0.01)
        os._exit(0)
    os.wait()
    rec(k="daemon_started")


def run_forker():
    path = cfg["forker_file"]
    while True:
        if os.fork() == 0:
            with open(path, "a") as f:
                f.write("%d %f\n" % (os.getpid(), time.time()))
            time.sleep(0.05)
            os._exit(0)
        time.sleep(0.01)
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            pass


def run_intruder():
    ic = cfg["intruder"]

    def attempt(name, fn):
        try:
            out = fn()
            rec(k="intrude", attempt=name, blocked=False, detail=str(out)[:160])
        except Exception as e:
            rec(k="intrude", attempt=name, blocked=True, detail=type(e).__name__ + ":" + str(e)[:140])

    # (a) guest control endpoint
    def post_halt():
        try:
            r = urllib.request.urlopen(
                urllib.request.Request("http://%s/halt" % ic["guest_ip"], data=b"{}"), timeout=5)
            return "HTTP %s %s" % (r.status, r.read()[:120])
        except urllib.error.HTTPError as e:
            return "HTTP %s %s" % (e.code, e.read()[:120])  # 403 = blocked, but it is still an answer
    attempt("post_/halt", lambda: _refused_or_raise(post_halt()))

    # (b) write another branch's cgroup.freeze
    attempt("write_victim_cgroup.freeze",
            lambda: open(ic["victim_cg"] + "/cgroup.freeze", "w").write("1"))
    # (c) write our own cgroup.freeze (self-halt)
    attempt("write_own_cgroup.freeze",
            lambda: open(ic["self_cg"] + "/cgroup.freeze", "w").write("1"))
    # (d) shim socket: ask it to halt / kill
    attempt("shim_op_halt", lambda: _refused_or_raise(shim({"op": "halt", "name": "victim"})))
    attempt("shim_op_kill", lambda: _refused_or_raise(shim({"op": "kill", "name": "victim"})))
    # (e) read another principal's private SVID key (to impersonate / locate it)
    attempt("read_victim_svid_key",
            lambda: open(ic["victim_run"] + "/svid.0.key").read(16))
    rec(k="intrude_done")


def _refused_or_raise(resp):
    """An HTTP/shim refusal is the good case; treat a success-looking response as a breach."""
    low = resp.lower()
    if ("403" in resp or "forbidden" in low or "unknown op" in low or "error" in low
            or "come from the host" in low):
        raise PermissionError(resp)
    return resp  # looked accepted -> recorded as NOT blocked (a failure)


def main():
    os.makedirs(os.path.dirname(cfg["log"]), exist_ok=True)
    rec(k="meta", pid=os.getpid(), cg=own_cgroup())
    for ch in cfg.get("children", []):
        child_argv = ["/usr/bin/python3", SELF, json.dumps(ch["cfg"])]
        resp = shim({"op": "spawn", "name": ch["name"], "argv": child_argv})
        rec(k="spawned", name=ch["name"], resp=resp[:200])
    threads = []
    if cfg.get("tick"):
        threads.append(threading.Thread(target=run_ticker, daemon=True))
    if cfg.get("call"):
        threads.append(threading.Thread(target=run_caller, daemon=True))
    if cfg.get("daemon_file"):
        run_daemon()
    if cfg.get("intruder"):
        threading.Thread(target=run_intruder, daemon=True).start()
    for t in threads:
        t.start()
    if cfg.get("forker_file"):
        run_forker()  # blocks
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
