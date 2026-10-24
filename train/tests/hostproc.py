"""Detached server start / whole-session stop for the host checks (stdlib only; runs on the GPU host and in selftest).

  hostproc.py start --pid-file F --log L -- CMD...   fork, setsid, 0/1/2 -> /dev/null,L,L, exec CMD; prints the pid, returns at once
  hostproc.py alive --pid-file F                      exit 0 if the session leader is alive
  hostproc.py stop  --pid-file F --port P             TERM the session, wait, KILL, fuser -k the port; prints JSON
                                                      {"leftover": [{"pid", "comm", "gpu"}], "port_open": bool}

The pid in F is the session leader's, so pid == pgid == sid: every process the server forks (vLLM's EngineCore)
is found by its session id, not by name or by port.
"""
import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

PRODUCTION_PORT = 8000


def session_pids(sid):
    out = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True, text=True).stdout
    pids = []
    for line in out.splitlines():
        parts = line.split(None, 1)
        if not parts:
            continue
        pid = int(parts[0])
        try:
            if os.getsid(pid) == sid:
                pids.append((pid, parts[1] if len(parts) > 1 else "?"))
        except (ProcessLookupError, PermissionError):
            pass
    return pids


def gpu_pids():
    if not shutil.which("nvidia-smi"):
        return set()
    out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader"], capture_output=True, text=True).stdout
    return {int(x) for x in out.split() if x.strip().isdigit()}


def port_open(port):
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def kill_port(port, sig):
    if shutil.which("fuser"):
        subprocess.run(["fuser", "-k", f"-{sig}", f"{port}/tcp"], capture_output=True)
    else:
        out = subprocess.run(["lsof", "-t", "-i", f"TCP:{port}", "-s", "TCP:LISTEN"], capture_output=True, text=True).stdout
        for p in out.split():
            try:
                os.kill(int(p), getattr(signal, "SIG" + sig))
            except (ProcessLookupError, PermissionError):
                pass


def start(a):
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        os.setsid()
        null = os.open(os.devnull, os.O_RDONLY)
        log = os.open(a.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(null, 0)
        os.dup2(log, 1)
        os.dup2(log, 2)
        os.closerange(3, w)
        os.closerange(w + 1, 1024)
        with open(a.pid_file, "w") as f:
            f.write(str(os.getpid()))
        os.write(w, b"1")
        os.close(w)
        try:
            os.execvp(a.cmd[0], a.cmd)
        finally:
            os._exit(127)
    os.close(w)
    os.read(r, 1)  # the child has setsid'd and written its pid; nothing of ours is left in the session
    print(pid)


def read_pid(a):
    try:
        return int(open(a.pid_file).read().strip())
    except (OSError, ValueError):
        return None


def alive(a):
    pid = read_pid(a)
    if pid is None:
        return 1
    try:
        os.kill(pid, 0)
        return 0
    except ProcessLookupError:
        return 1
    except PermissionError:
        return 0


def _signal_session(sid, sig):
    try:
        os.killpg(sid, sig)
    except (ProcessLookupError, PermissionError):
        pass
    for pid, _ in session_pids(sid):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _wait_gone(sid, seconds):
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds and session_pids(sid):
        time.sleep(0.5)


def stop(a):
    if a.port == PRODUCTION_PORT:
        print(json.dumps({"error": f"refusing to touch port {PRODUCTION_PORT} (production vLLM)"}))
        return 2
    sid = read_pid(a)
    if sid is not None:
        _signal_session(sid, signal.SIGTERM)
        _wait_gone(sid, a.term_wait)
        _signal_session(sid, signal.SIGKILL)
        _wait_gone(sid, 10)
    kill_port(a.port, "TERM")
    time.sleep(1)
    kill_port(a.port, "KILL")
    time.sleep(1)
    gpu = gpu_pids()
    leftover = [{"pid": p, "comm": c, "gpu": p in gpu} for p, c in (session_pids(sid) if sid is not None else [])]
    print(json.dumps({"leftover": leftover, "port_open": port_open(a.port)}))
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="op", required=True)
    s = sub.add_parser("start")
    s.add_argument("--pid-file", required=True)
    s.add_argument("--log", required=True)
    s.add_argument("cmd", nargs=argparse.REMAINDER)
    s = sub.add_parser("alive")
    s.add_argument("--pid-file", required=True)
    s = sub.add_parser("stop")
    s.add_argument("--pid-file", required=True)
    s.add_argument("--port", type=int, required=True)
    s.add_argument("--term-wait", type=float, default=30)
    a = ap.parse_args()
    if a.op == "start":
        if a.cmd and a.cmd[0] == "--":
            a.cmd = a.cmd[1:]
        if not a.cmd:
            ap.error("start needs a command")
        return start(a)
    return {"alive": alive, "stop": stop}[a.op](a)


if __name__ == "__main__":
    sys.exit(main() or 0)
