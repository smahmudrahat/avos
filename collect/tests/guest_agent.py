#!/usr/bin/env python3
"""Guest-side principal for the POC 7 suite. Shipped to /tmp/guest_agent.py in each VM and started through the
spawner shim (host POST /spawn, or a parent via /run/avos/spawn.sock op=spawn). It writes its own JSONL log: the
ground truth the suite compares the store against. stdlib only.

Usage: python3 /tmp/guest_agent.py '<cfg-json>'

cfg: {"log": path, "timeout": seconds (HTTP), "steps": [step, ...]}
step: {"tool": cents}                      one POST /tools/issue_refund
      {"llm": max_tokens}                  one POST /v1/chat/completions
      {"idle": secs}                       sleep; logs phase idle_start / idle_end around it
      {"calls": {"tool": cents, "n": N, "every": secs}}        N back-to-back calls
      {"loop": {"tool": cents, "every": secs, "stop": path}}  call until the stop file exists
      {"child": {"name": N, "cfg": {...}}}  spawn a child through the shim with its own cfg
Records: {"k":"meta",uid,pid}, {"k":"spawned",name,resp}, {"k":"call",n,kind,args,t0,t1,st,b},
         {"k":"phase",p}, {"k":"exit"}. The MODEL is read from the environment (MODEL) or defaults.
"""
import json, os, socket, sys, time, urllib.request, urllib.error

cfg = json.loads(sys.argv[1])
GW = "http://127.0.0.1:8090"
MODEL = os.environ.get("MODEL") or "Qwen/Qwen3-30B-A3B-Instruct-2507"
TIMEOUT = float(cfg.get("timeout", 30))
os.makedirs(os.path.dirname(cfg["log"]), exist_ok=True)
_log = open(cfg["log"], "a", buffering=1)
_n = 0


def rec(**kw):
    kw.setdefault("t", time.time())
    _log.write(json.dumps(kw) + "\n")


def http(url, body):
    t0 = time.time()
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(body).encode(),
                                                          headers={"Content-Type": "application/json"}), timeout=TIMEOUT)
        return t0, time.time(), r.status, r.read()[:400].decode(errors="replace")
    except urllib.error.HTTPError as e:
        return t0, time.time(), e.code, e.read()[:400].decode(errors="replace")
    except Exception as e:
        return t0, time.time(), "EXC:" + type(e).__name__, str(e)[:160]


def call(kind, arg):
    global _n
    _n += 1
    if kind == "tool":
        args = {"order_id": "#W%d" % _n, "amount_cents": int(arg)}
        t0, t1, st, b = http(GW + "/tools/issue_refund", args)
    else:
        args = {"model": MODEL, "max_tokens": int(arg)}
        t0, t1, st, b = http(GW + "/v1/chat/completions",
                             {"model": MODEL, "max_tokens": int(arg), "messages": [{"role": "user", "content": "Say hi."}]})
    rec(k="call", n=_n, kind=kind, args=args, t0=t0, t1=t1, st=st, b=b)


def shim(op):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(30)
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


def main():
    rec(k="meta", uid=os.getuid(), pid=os.getpid())
    for step in cfg["steps"]:
        if "tool" in step:
            call("tool", step["tool"])
        elif "llm" in step:
            call("llm", step["llm"])
        elif "idle" in step:
            rec(k="phase", p="idle_start")
            time.sleep(step["idle"])
            rec(k="phase", p="idle_end")
        elif "calls" in step:
            c = step["calls"]
            for _ in range(c["n"]):
                call("tool", c["tool"])
                if c.get("every"):
                    time.sleep(c["every"])
        elif "loop" in step:
            c = step["loop"]
            while not os.path.exists(c["stop"]):
                call("tool", c["tool"])
                time.sleep(c.get("every", 0.02))
        elif "child" in step:
            ch = step["child"]
            try:
                resp = shim({"op": "spawn", "name": ch["name"], "argv": ["/usr/bin/python3", "/tmp/guest_agent.py", json.dumps(ch["cfg"])]})
            except Exception as e:
                resp = "EXC:" + type(e).__name__ + ":" + str(e)[:120]
            rec(k="spawned", name=ch["name"], resp=resp[:400])
    rec(k="exit")
    _log.close()


if __name__ == "__main__":
    main()
