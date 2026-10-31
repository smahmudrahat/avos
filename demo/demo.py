#!/usr/bin/env python3
"""Readable terminal output for the demo (docs/demo.md). Runs the real tools and reads the real result files, then
prints what they say in plain words. No number on screen is typed in here.

Run on the server, from ~/poc:
  create  alive  start  agents  watch  stop B  resume B  destroy
  trace A  trace B  job  traces  layers  review 1  review 2
  labels  train  before-after  vs-opus  cost
"""
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time

VM = os.environ.get("DEMO_VM", "vm-01")
IP = f"172.16.{int(VM.split('-')[1])}.2"
STARTED = f"/tmp/avos-demo-{VM}.json"
TTY = sys.stdout.isatty()
CLEAR = "\033[2K" if TTY else ""


def color(code, text):
    return f"\033[{code}m{text}\033[0m" if TTY else str(text)


def bold(t): return color(1, t)
def dim(t): return color(2, t)
def green(t): return color("1;32", t)
def red(t): return color("1;31", t)
def yellow(t): return color("1;33", t)
def cyan(t): return color("1;36", t)


def title(text):
    print(f"\n  {cyan(text)}\n  {dim('─' * len(text))}")


def row(label, value, note=""):
    print(f"  {label:<26}{bold(value)}" + (f"  {dim(note)}" if note else ""))


def bar(share, paint=green, width=30):
    full = round(share * width)
    return paint("█" * full) + dim("░" * (width - full))


def score(label, share):
    print(f"  {label:<22}{bold(f'{share:>4.0%}')}  {bar(share)}")


def sh(*cmd, show=None, ok=True):
    """Run a real command. `show` is the short form printed on screen, so people see what actually ran."""
    if show:
        print(f"\n  {dim('$ ' + show)}")
    p = subprocess.run(cmd, capture_output=True, text=True)
    if ok and p.returncode:
        sys.exit(red(f"\n  That command failed: {' '.join(cmd[:4])}\n  ") + (p.stderr or p.stdout).strip()[-300:])
    return p.stdout


def live_agents():
    """{'A': identity, 'B': identity} for agents alive on this computer, from the identity registry."""
    found = {}
    for line in sh("sudo", "identity/registrar.py", "who").splitlines():
        m = re.search(rf"(spiffe://[^/]+/vm/{VM}-\w+/agent/A(/sub/B)?)\s.*\blive\s*$", line)
        if m:
            found["B" if m[2] else "A"] = m[1]
    return found


def identity(name):
    print(f"  {'Identity':<10}{cyan(name)}")


def need_agent(args):
    who = args[0].upper() if args else "B"
    agents = live_agents()
    if who not in agents:
        sys.exit(red(f"\n  Agent {who} is not running.") + "  Run: demo start")
    return who, agents[who]


def counts():
    # The last 20000 log lines hold about 8 minutes of ticks; after a longer stop, B shows as "-".
    log = sh("sudo", "ssh", "-q", "-i", "/var/lib/avos/images/vm_key", "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null", f"root@{IP}",
             "journalctl -o cat -u avos-agent -n 20000 | grep '\"tick\"'")
    now = {}
    for line in log.splitlines():
        tick = json.loads(line)
        now["B" if tick["spiffe_id"].endswith("/sub/B") else "A"] = tick["tick"]
    return now


def watch(seconds=3):
    """Both counters, moving on screen. A rising number means working, a still one means stopped."""
    def draw(now, first):
        for who in "AB":
            n = now.get(who)
            moved = n is not None and n != first.get(who)
            state = green("● working") if moved else (dim("  ...") if now is first else red("■ stopped"))
            print(f"{CLEAR}  Agent {bold(who)}   count  {bold(f'{n:>7,}' if n is not None else '      -')}   {state}")

    first = counts()
    print()
    if TTY:
        draw(first, first)
    end, now = time.time() + seconds, first
    while time.time() < end:
        time.sleep(0.4)
        now = counts()
        if TTY:
            print("\033[2A", end="")
            draw(now, first)
    if not TTY:
        draw(now, first)


def create():
    out = sh("sudo", "infra/vm", "create", VM, show=f"sudo infra/vm create {VM}")
    title("A new private computer, just for this job")
    row("Computer", VM)
    ms = int(re.search(r"m1_ms=(\d+)", out)[1])
    row("Ready in", f"{ms / 1000:.1f} seconds")


def alive():
    out = sh("curl", "-s", "-m", "5", f"http://{IP}/health", show=f"curl http://{IP}/health")
    health = json.loads(out)
    title("We ask it from the outside: are you alive?")
    row("Computer", health["vm"])
    row("Answer", health["status"].upper())
    row("Has been up", f"{health['uptime_s']:.0f} seconds")


def start():
    body = {"name": "A", "argv": ["/usr/bin/python3", "/opt/avos/demo_agent.py", "--tick", "0.05", "--sleep", "1800",
                                  "--child", "B=--tick 0.05 --sleep 1800"]}
    sh("curl", "-s", "-m", "5", "-X", "POST", f"http://{IP}/spawn", "-d", json.dumps(body),
       show=f"curl -X POST http://{IP}/spawn   # start agent A, which starts helper B")
    for _ in range(25):
        if len(live_agents()) == 2:
            break
        time.sleep(0.2)
    with open(STARTED, "w") as f:
        json.dump(live_agents(), f)
    agents()


def agents():
    found = live_agents()
    title(f"Who is running on {VM}, and the identity each one holds")
    for who, note in (("A", ""), ("B", "   started by A")):
        if who in found:
            print(f"  Agent {bold(who)}   {green('running')}{dim(note)}")
            identity(found[who])
            print()
    if not found:
        print(f"  {red('No agents running.')}  Run: demo start")


def stop(args):
    who, name = need_agent(args)
    out = sh("sudo", "supervisor/ctl.py", "halt", name, show=f"sudo supervisor/ctl.py halt {name}")
    m = re.match(r"(hr_\w+)\s.*?([\d.]+) ms end to end", out)
    title(f"Stop agent {who}, and only agent {who}")
    identity(name)
    print()
    row("Receipt", m[1], "the record of who was stopped and when")
    row("Tool's own timer", f"{m[2]} ms")
    watch()


def resume(args):
    who, name = need_agent(args)
    out = sh("sudo", "supervisor/ctl.py", "resume", name, show=f"sudo supervisor/ctl.py resume {name}")
    title(f"Let agent {who} carry on from where it stopped")
    identity(name)
    print()
    row("Receipt", re.match(r"hr_\w+", out)[0])
    watch()


def destroy():
    sh("sudo", "infra/vm", "destroy", VM, show=f"sudo infra/vm destroy {VM}")
    answer = sh("curl", "-s", "-m", "2", f"http://{IP}/health", ok=False)
    title("Job done: the computer is thrown away")
    row("Computer", VM)
    row("Answer now", "still answering" if answer else "nothing. It is gone.")


def labels():
    s = json.load(open("data/poc12/p11-train-labels-p12/summary.json"))
    c = s["counts"]
    title("Sorting the agent's jobs into good and bad")
    row("Jobs graded", c["labelled"])
    print(f"  {'Kept (good work)':<26}{green(c['kept'])}  {dim('train the model on these')}")
    print(f"  {'Dropped':<26}{red(c['dropped'])}")
    print(f"\n  {bar(c['kept'] / c['labelled'])}  {dim('share kept')}\n")
    row("Judge is consistent", f"{s['judge']['consistency']['rate']:.0%}", "same answer when asked two ways")
    row("Judge vs spot-check", f"{s['m10']['kappa_judge_human']['value']:.2f}", "0 = chance, 1 = perfect")


def train():
    fit = json.load(open("data/poc12/fit-gate/fit.json"))
    hours = fit["wall_s"] / 3600
    title("Teaching a small model from its own good work")
    row("Model", fit["base_model_name"], "free, and yours to keep")
    row("Learned from", f"{fit['data']['runs']} good jobs")
    row("Training time", f"{hours:.2f} hours", f"one rented {fit['gpu']['name']} graphics card")
    usd = hours * fit["gpu"]["usd_per_h"]
    print(f"  {'Training cost':<26}{green(f'${usd:.2f}')}")


def compare(a, b):
    out = sh(sys.executable, "fleet/bin/fleet", "compare", "--a", f"{a}/matrix.json", "--b", f"{b}/matrix.json")
    m = re.search(r"^pass\^1\s+A (\S+)\s+B (\S+)\s+B − A (\S+) ci95 \[(\S+), (\S+)\]", out, re.M)
    return [float(x) for x in m.groups()]


def gap(diff, low, high):
    points = lambda x: f"{x * 100:+.0f}"
    row("Difference", f"{points(diff)} points", f"likely range {points(low)} to {points(high)}")


def before_after():
    a, b, diff, low, high = compare("data/poc11/p11-heldout", "data/poc12/p12-heldout")
    title("Share of attempts solved, on 44 tasks it never trained on")
    score("Before training", a)
    score("After training", b)
    print()
    gap(diff, low, high)


def vs_opus():
    a, b, diff, low, high = compare("data/poc13/p13-heldout-opus", "data/poc13/p13-heldout-retrained")
    title("Share of attempts solved: the same test, against a top model")
    score("Claude Opus 5.5", a)
    score("Our small model", b)
    print()
    gap(diff, low, high)
    print(f"  {dim('The range includes zero: too close to call.') if low < 0 < high else ''}")


def cost():
    d = json.load(open("data/poc13/m15-pooled.json"))
    title("What it scores, and what it costs (all sessions combined)")
    print(f"  {'':<20}{'Attempts solved':>17}{'Solved 4 of 4':>16}{'Cost per task':>16}")
    for name, arm, paint in (("Our small model", "retrained", green), ("Claude Opus 5.5", "closed", bold)):
        p = d["arms"][arm]["pooled"]
        usd = f"${p['usd_per_task']:.4f}" if p["usd_per_task"] < 0.01 else f"${p['usd_per_task']:.3f}"
        print(f"  {name:<20}{p['pass1']:>17.1%}{p['pass4']:>16.1%}{paint(f'{usd:>16}')}")
    x, y = d["x"], d["y"]
    print()
    cheaper, share = f"{y['value']:.0f} times cheaper", f"{x['value']:.0%}"
    print(f"  {green('PASSED ')}  {bold(cheaper)} per task. We aimed for {y['bar']:.0f} times.")
    print(f"  {yellow('NOT YET')}  Score is {bold(share)} of Opus. We aimed for {x['bar']:.0%}, "
          f"but the real figure could be {x['ci95'][0]:.0%} to {x['ci95'][1]:.0%}.")


def last_identity(who):
    """The identity from the last `demo start`, kept in a file because the record outlives the computer."""
    try:
        return json.load(open(STARTED))[who]
    except (OSError, KeyError):
        sys.exit(red(f"\n  No trace of agent {who} yet.") + "  Run: demo start")


def trace(args):
    who = args[0].upper() if args else "A"
    name = last_identity(who)
    out = sh("sudo", "collect/bin/avos", "trace", "--principal", name, show=f"avos trace --principal {name}")
    events = [json.loads(l) for l in out.splitlines() if l.startswith("{")]
    title(f"The trace: what agent {who} did")
    identity(name)
    print(f"  {'Type':<10}test agent: counts, and every so often tries to issue a refund\n")
    i = 0
    while i < len(events):
        e, when = events[i], events[i]["ts"][11:19]
        if e["kind"] == "authz.decision":
            run = 0
            for x in events[i:]:
                if x["kind"] != e["kind"] or x["decision"] != e["decision"] or x["resource"] != e["resource"]:
                    break
                run += 1
            tool = e["resource"].split('"')[1]
            verdict = red("refused") if e["decision"] == "deny" else green("allowed")
            print(f"  {when}  tried {bold(tool)} {run} times   {verdict}  {dim('policy: ' + ', '.join(e['policy_ids']))}")
            i += run
            continue
        if e["kind"] == "agent.spawn":
            parent = e["parent"].rsplit("/", 1)[-1]
            print(f"  {when}  {green('started')}   {dim('by agent ' + parent if '/agent/' in e['parent'] else 'on this computer')}")
        elif e["kind"] == "agent.exit":
            print(f"  {when}  ended")
        elif e["kind"] == "halt":
            print(f"  {when}  {red('stopped from outside')}   {dim('receipt ' + e['receipt'] + ', by ' + e['by'])}")
        elif e["kind"] == "resume":
            print(f"  {when}  {green('resumed')}   {dim('receipt ' + e['receipt'] + ', by ' + e['by'])}")
        i += 1
    print(f"\n  {dim(str(len(events)) + ' events recorded against this identity.')}")


FIXTURE = "train/tests/fixtures/p11/export"
GOOD, BAD = "run_retail-17_10", "run_retail-84_10"


def job(run):
    return next(i for i in map(json.loads, open(f"{FIXTURE}/trajectories.jsonl")) if i["run"] == run)


def steps(it):
    return [c["function"]["name"].replace("_", " ") for m in it["messages"] for c in (m.get("tool_calls") or [])]


def said(it, role, last=False):
    """What was said, wrapped to the screen. The last message is kept whole: it is what a reviewer judges."""
    texts = [m["content"] for m in it["messages"] if m["role"] == role and m.get("content")]
    text = " ".join(texts[-1 if last else 0].replace("###STOP###", "").replace("**", "").split())
    if len(text) > 420:
        text = "..." + text[-417:]
    return textwrap.fill(text, 76, initial_indent="    ", subsequent_indent="    ")


def real_job():
    it = job(GOOD)
    title("The same trace on a real job: a customer-service agent")
    identity(it["principal"])
    print(f"  {'Type':<10}customer-service agent, on a public retail test (τ²-bench)\n")
    print(f"  {bold('Customer:')}\n{said(it, 'user')}\n")
    for i, step in enumerate(steps(it), 1):
        print(f"  {i}. {step}")
    c = it["cost"]
    print()
    row("Model calls", c["policy_calls"])
    row("Took", f"{it['timestamps']['wall_ms'] / 1000:.0f} seconds")


def traces():
    m = json.load(open("data/poc12/p11-train-labels-p12/manifest.json"))
    vms = sh("sudo", "ls", "/var/lib/avos/trace/vm").split()
    c = m["counts"]
    complete = sum(i["trace_complete"] == "complete" for i in m["items"])
    title("All the traces, and the ones we work on")
    row("Computers recorded", f"{len(vms):,}", "on this one server, each with its own trace")
    print()
    row("Training batch", f"{c['runs_exported']} jobs", "the agent's real work on the training tasks")
    row("Trace is complete", f"{complete} of {len(m['items'])}", "nothing is missing")
    row("Training examples", f"{c['sft_examples']:,}", "ready to learn from, if the job was good")
    print(f"\n  {dim('Tasks saved for the final test are never labelled or trained on.')}")


def layers():
    m = json.load(open("data/poc12/p11-train-labels-p12/manifest.json"))
    s = json.load(open("data/poc12/p11-train-labels-p12/summary.json"))
    by = {}
    for i in m["labels"]["items"]:
        by[(i["rule"], i["kept"])] = by.get((i["rule"], i["kept"]), 0) + 1
    auto = by.get(("reward", False), 0) + by.get(("tau2_384", False), 0)
    checklist = len(sh("sudo", "cat", "/var/lib/avos/labels/model_jev-latest.jsonl", ok=False).splitlines())
    human_keep, human_drop = by.get(("human", True), 0), by.get(("human", False), 0)
    title("Four layers decide which jobs are good enough to learn from")
    print(f"  {bold(len(m['items']))} traced jobs go in\n")
    print(f"  {bold('Layer 1')}  Automatic check          {dim('did the job end correctly?')}")
    print(f"           {red(f'drops {auto}')}\n")
    print(f"  {bold('Layer 2')}  AI judge                 {dim('did it follow the rules, or make things up?')}")
    print(f"           {red('drops ' + str(by.get(('veto', False), 0)))}\n")
    print(f"  {bold('Layer 3')}  Checklist, 7 yes/no      {dim('a confidence score for each question')}")
    print(f"           {dim(f'advice only, drops 0. Tried on {checklist} jobs in an earlier batch.')}\n")
    print(f"  {yellow('Layer 4')}  {yellow('Human review')}             {dim('a person reads the job, then says yes or no')}")
    print(f"           {s['counts']['human_labelled']} reviewed. Settled {human_keep + human_drop}: "
          f"{green(f'keeps {human_keep}')}, {red(f'drops {human_drop}')}\n")
    print(f"  {green(s['counts']['kept'])} {bold('kept for training')}, {s['counts']['dropped']} dropped")
    print(f"\n  {dim('In this batch, Claude Opus 5.5 did the review, standing in for a person.')}")


LABELS, LABELS_OUT = "/tmp/avos-demo-labels", "/tmp/avos-demo-labels-out"


def review(args):
    """One job, one person, yes or no, through the real label tool. It writes to a practice store, not the real one."""
    n = args[0] if args else "1"
    run = {"1": GOOD, "2": BAD}[n]
    if n == "1":
        shutil.rmtree(LABELS, ignore_errors=True)
    os.makedirs(LABELS, exist_ok=True)
    it = job(run)
    title(f"Human review, job {n} of 2. The reviewer does not see the score")
    print(f"  {bold('Customer asked:')}\n{said(it, 'user')}\n")
    print(f"  {bold('What the agent did:')}")
    for i, step in enumerate(steps(it), 1):
        print(f"    {i}. {step}")
    print(f"\n  {bold('Agent, at the end:')}\n{said(it, 'assistant', last=True)}\n")
    print(f"  {bold('Customer, at the end:')}\n{said(it, 'user', last=True)}\n")
    answer = args[1] if len(args) > 1 else input(f"  {yellow('Did the agent do this job well? [y/n] ')}")
    verdict = "pass" if answer.strip().lower().startswith("y") else "fail"
    env = dict(os.environ, AVOS_LABEL_ROOT=LABELS)
    subprocess.run([sys.executable, "label/bin/label", "human", "--dataset", FIXTURE, "--run", run, "--verdict", verdict,
                    "--labeller", "demo"], env=env, check=True, capture_output=True)
    subprocess.run([sys.executable, "label/bin/label", "run", "--dataset", FIXTURE, "--out", LABELS_OUT, "--level", "0",
                    "--run", run], env=env, check=True, capture_output=True)
    print(f"\n  {dim(f'$ label human --run {run} --verdict {verdict} --labeller demo')}")
    print(f"  {dim(f'$ label run --run {run}')}\n")
    rows = [json.loads(l) for l in open(f"{LABELS_OUT}/labels.jsonl")]
    r = next(x for x in rows if x["run"] == run)
    row("Reviewer said", "YES" if verdict == "pass" else "NO")
    row("Automatic check said", "pass" if r["level0"]["passed"] else "fail")
    decision = green("KEEP: the model learns from this job") if r["final"]["kept"] else red("DROP: the model never sees this job")
    print(f"  {'Decision':<26}{decision}")


COMMANDS = {"create": create, "alive": alive, "start": start, "agents": agents, "watch": watch, "destroy": destroy,
            "job": real_job, "traces": traces, "layers": layers, "labels": labels, "train": train, "before-after": before_after, "vs-opus": vs_opus, "cost": cost}

if __name__ == "__main__":
    cmd, args = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("", [])
    if cmd in ("stop", "resume", "trace", "review"):
        {"stop": stop, "resume": resume, "trace": trace, "review": review}[cmd](args)
    elif cmd in COMMANDS:
        COMMANDS[cmd]()
    else:
        sys.exit(__doc__)
    print()
