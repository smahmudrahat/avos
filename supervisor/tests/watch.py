#!/usr/bin/env python3
"""Guest-side freeze watcher. Watches one cgroup.events file with poll(POLLPRI) and prints a line on every
frozen-state edge, with the guest wall-clock time of the edge. Independent of anything ctl.py prints.

Usage: python3 /tmp/watch.py <cgroup-dir> [<cgroup-dir> ...]
Each edge prints:  EDGE <cgroup-dir> <frozen 0|1> <guest_wall_time>
Flushed immediately so the host can read it live over a persistent ssh channel.
"""
import select, sys, time


def frozen_of(path):
    with open(path) as f:
        for line in f:
            if line.startswith("frozen "):
                return line.split()[1]
    return "?"


def main():
    dirs = sys.argv[1:]
    files = {}
    state = {}
    po = select.poll()
    for d in dirs:
        p = d + "/cgroup.events"
        f = open(p)
        f.read()
        files[f.fileno()] = (d, f)
        state[d] = frozen_of(p)
        po.register(f.fileno(), select.POLLPRI | select.POLLERR)
        print("EDGE %s %s %f" % (d, state[d], time.time()), flush=True)
    while True:
        for fd, _ev in po.poll():
            d, f = files[fd]
            f.seek(0)
            cur = frozen_of(d + "/cgroup.events")
            if cur != state[d]:
                state[d] = cur
                print("EDGE %s %s %f" % (d, cur, time.time()), flush=True)


if __name__ == "__main__":
    main()
