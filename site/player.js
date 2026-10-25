// The 60-second run: Control → Trace → Retrain → Score, driven by data/cfg.json (built from measured runs by build.py).
const $ = (s, r = document) => r.querySelector(s);
const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
const fmt = { pct: v => `${Math.round(v * 100)}%`, usd: v => `$${v.toFixed(v < 0.01 ? 4 : 2)}`, int: v => Math.round(v).toLocaleString("en-US") };

function rng(seed) { return () => (seed = (seed * 1664525 + 1013904223) >>> 0) / 2 ** 32; }

// The tree the run grows: two microVMs, a harness session each, agents and subagents. Names follow the SPIFFE paths
// the platform mints (spiffe://avoslab.com/vm/<vm>/agent/<name>).
const TREE = [
  { id: "vm-01", kind: "microVM", at: 4.2 },
  { id: "vm-01/support", kind: "session", parent: "vm-01", at: 5.0 },
  { id: "vm-01/support/triage", kind: "agent", parent: "vm-01/support", at: 6.0, label: "triage" },
  { id: "vm-01/support/triage/lookup-order", kind: "subagent", parent: "vm-01/support/triage", at: 8.0, label: "lookup-order" },
  { id: "vm-01/support/triage/issue-refund", kind: "subagent", parent: "vm-01/support/triage", at: 9.5, label: "issue-refund" },
  { id: "vm-02", kind: "microVM", at: 4.8 },
  { id: "vm-02/tau2", kind: "session", parent: "vm-02", at: 5.6 },
  { id: "vm-02/tau2/retail-agent", kind: "agent", parent: "vm-02/tau2", at: 6.6, label: "retail-agent" },
  { id: "vm-02/tau2/retail-agent/search", kind: "subagent", parent: "vm-02/tau2/retail-agent", at: 10.5, label: "search" },
  { id: "vm-02/tau2/retail-agent/summarize", kind: "subagent", parent: "vm-02/tau2/retail-agent", at: 11.5, label: "summarize" },
];
const VIOLATOR = "vm-01/support/triage/issue-refund";
const BEATS = [[0, 26, 0], [26, 36, 1], [36, 42, 2], [42, 60, 3]];
const DURATION = 60;

export async function mount(root, { autoplay = false, cfgUrl = "data/cfg.json" } = {}) {
  const cfg = await (await fetch(cfgUrl)).json();
  const ui = build(root, cfg);
  let state, last = null, playing = false;

  function reset() {
    state = { t: 0, nodes: new Map(), stopped: new Set(), filter: null, events: 0, tokens: 0, spend: 0, rand: rng(300),
              next: 0, red: false, autoStopped: false, runs: [], labelled: 0, lossDrawn: 0, scored: 0 };
    ui.reset();
  }

  const isFrozen = id => [...state.stopped].some(s => id === s || id.startsWith(s + "/"));

  function stop(id, auto = false) {
    if (isFrozen(id)) return;
    state.stopped.add(id);
    const under = [...state.nodes.keys()].filter(n => n === id || n.startsWith(id + "/")).length;
    const ms = cfg.control.stop_ms_median + Math.round(state.rand() * (cfg.control.stop_ms_worst - cfg.control.stop_ms_median));
    const receipt = "hr_" + Math.floor(state.rand() * 0xffff).toString(16).padStart(4, "0");
    ui.freeze(id);
    ui.trace({ kind: "ctl.halt", principal: id, note: `${receipt} · ${under} node${under > 1 ? "s" : ""} frozen in ${ms} ms · siblings unaffected`, cls: "halt" }, state);
    ui.receipt(id, receipt, ms, auto);
  }

  function emit(id) {
    const s = cfg.trace_sample.events.filter(e => e.kind === "llm.call" || e.kind === "authz.decision");
    const e = s[Math.floor(state.rand() * s.length)];
    const ev = { kind: e.kind, principal: id };
    if (e.kind === "llm.call") {
      const role = /gemma/i.test(e.model || "") ? "simulator" : /deepseek/i.test(e.model || "") ? "judge" : "policy";
      ev.note = `${role} · ${e.prompt_tokens || 0}+${e.completion_tokens || 0} tok · ${e.ms || 0} ms`;
      state.tokens += (e.prompt_tokens || 0) + (e.completion_tokens || 0);
      state.spend += ((e.prompt_tokens || 0) * 0.1 + (e.completion_tokens || 0) * 0.15) / 1e6;
    } else ev.note = `allow · ${id.split("/").pop()}`;
    state.events++;
    ui.trace(ev, state);
  }

  function step(dt) {
    const t0 = state.t, t = state.t = Math.min(DURATION, t0 + dt);
    for (const n of TREE) if (n.at <= t && !state.nodes.has(n.id)) { state.nodes.set(n.id, n); ui.addNode(n, { stop, focus }); }
    if (t >= 14 && !state.red) {
      state.red = true; ui.violate(VIOLATOR);
      ui.trace({ kind: "tool.call", principal: VIOLATOR, note: "issue_refund amount $480 · policy limit $200", cls: "bad" }, state);
    }
    if (t >= 20 && !state.autoStopped) { state.autoStopped = true; ui.cursorTo(VIOLATOR, () => stop(VIOLATOR, true)); }
    if (t < 36) {
      const live = [...state.nodes.keys()].filter(id => !isFrozen(id) && state.nodes.get(id).kind !== "microVM");
      while (state.next <= t && live.length) { emit(live[Math.floor(state.rand() * live.length)]); state.next += t < 26 ? 0.18 : 0.07; }
    }
    if (t >= 26 && t < 36) ui.label(Math.min(1, (t - 26) / 8), cfg);
    if (t >= 36) ui.retrain(Math.min(1, (t - 36) / 6), cfg);
    if (t >= 42) ui.score(Math.min(1, (t - 42) / 8), cfg);
    ui.overlay(t < 4 ? "hook" : t >= 52 ? "end" : null);
    ui.tick(state, BEATS.find(([a, b]) => t >= a && t < b)?.[2] ?? 3);
    if (t >= DURATION) pause();
  }

  function focus(id) { state.filter = state.filter === id ? null : id; ui.filter(state.filter); }
  function frame(ts) { if (!playing) return; if (last != null) step((ts - last) / 1000); last = ts; requestAnimationFrame(frame); }
  function play() { if (state.t >= DURATION) reset(); playing = true; last = null; ui.playing(true); requestAnimationFrame(frame); }
  function pause() { playing = false; ui.playing(false); }

  ui.onPlay(() => (playing ? pause() : play()));
  $(".p-overlay", root).onclick = () => (playing ? pause() : play());
  ui.onRestart(() => { const was = playing; pause(); reset(); step(0); if (was) play(); });
  root.addEventListener("keydown", e => { if (e.code === "Space") { e.preventDefault(); playing ? pause() : play(); } });
  new IntersectionObserver(([en]) => { if (!en.isIntersecting && playing) pause(); }).observe(root);

  reset(); step(0);
  if (autoplay) play();
}

function build(root, cfg) {
  const s = cfg.score, X = s.x.value, Y = s.y.value;
  root.tabIndex = 0;
  root.classList.add("player");
  root.innerHTML = `
    <div class="p-bar">
      <div class="p-id"><b>Principal</b> agent cloud · live run</div>
      <ol class="p-beats"><li>Control</li><li>Trace</li><li>Retrain</li><li>Score</li></ol>
      <div class="p-counters">
        <div><span data-c="agents">0</span><small>agents live</small></div>
        <div><span data-c="tokens">0</span><small>tokens</small></div>
        <div><span data-c="spend">$0.00</span><small>spend</small></div>
        <div><span data-c="events">0</span><small>events traced</small></div>
        <div><span data-c="clock">00:00.0</span><small>run clock</small></div>
      </div>
    </div>
    <div class="p-grid">
      <section class="p-panel p-tree"><h3>Agent tree</h3>
        <div class="p-legend"><i class="k-microVM"></i>microVM <i class="k-session"></i>harness session <i class="k-agent"></i>agent <i class="k-subagent"></i>subagent</div>
        <ul class="tree"></ul><div class="p-receipt" hidden></div></section>
      <section class="p-panel p-trace"><h3>Trace · <span class="p-scope">all agents</span> <em class="p-count">0 events</em></h3><ol class="stream"></ol></section>
      <section class="p-panel p-right">
        <div class="p-sub p-label"><h3>Label</h3><div class="runs"></div><p class="p-note"></p></div>
        <div class="p-sub p-retrain"><h3>Retrain</h3><svg class="loss" viewBox="0 0 200 70" preserveAspectRatio="none"><path/></svg>
          <div class="prog"><i></i></div><p class="p-note"></p></div>
        <div class="p-sub p-score"><h3>Score</h3><div class="bars"></div><p class="p-head"></p></div>
      </section>
    </div>
    <div class="p-overlay" hidden></div>
    <div class="p-cursor" hidden></div>
    <div class="p-ctl"><button class="p-play" aria-label="Play">▶</button><div class="p-track"><i></i></div>
      <span class="p-time">0:00 / 1:00</span><button class="p-restart">Restart</button></div>`;

  const tree = $(".tree", root), stream = $(".stream", root), items = new Map();
  const pass4 = { base: s.base.pass4, retrained: s.retrained.pass4, opus: s.opus.pass4 };

  return {
    reset() {
      tree.innerHTML = ""; stream.innerHTML = ""; items.clear();
      $(".runs", root).innerHTML = ""; $(".p-label .p-note", root).textContent = "";
      $(".loss path", root).setAttribute("d", ""); $(".prog i", root).style.width = "0";
      $(".p-retrain .p-note", root).textContent = ""; $(".bars", root).innerHTML = ""; $(".p-head", root).textContent = "";
      $(".p-receipt", root).hidden = true; $(".p-scope", root).textContent = "all agents";
    },
    addNode(n, { stop, focus }) {
      const li = el("li", `node k-${n.kind}`);
      const row = el("div", "row");
      row.append(el("i", "dot"), el("span", "name", n.label || n.id.split("/").pop()), el("span", "kind", n.kind));
      const b = el("button", "stop", "■"); b.title = "Stop this branch"; b.onclick = e => { e.stopPropagation(); stop(n.id); };
      row.append(b); row.onclick = () => focus(n.id);
      li.append(row, el("ul"));
      (n.parent ? $("ul", items.get(n.parent)) : tree).append(li);
      items.set(n.id, li);
    },
    violate(id) { items.get(id)?.classList.add("bad"); },
    freeze(id) { for (const [k, li] of items) if (k === id || k.startsWith(id + "/")) li.classList.add("frozen"); },
    receipt(id, r, ms, auto) {
      const box = $(".p-receipt", root); box.hidden = false;
      box.innerHTML = `<b>halt receipt ${r}</b><span>${id.split("/").pop()} frozen in ${ms} ms</span><span>${auto ? "siblings keep running" : "stopped by you"}</span>`;
    },
    cursorTo(id, then) {
      const c = $(".p-cursor", root), btn = $(".stop", items.get(id)); if (!btn) return then();
      const a = root.getBoundingClientRect(), b = btn.getBoundingClientRect();
      c.hidden = false; c.style.transform = `translate(${a.width * 0.55}px, ${a.height * 0.7}px)`;
      requestAnimationFrame(() => { c.style.transform = `translate(${b.left - a.left + 4}px, ${b.top - a.top + 4}px)`; });
      setTimeout(() => { c.classList.add("click"); then(); setTimeout(() => { c.hidden = true; c.classList.remove("click"); }, 500); }, 900);
    },
    trace(ev, st) {
      const li = el("li", `ev ${ev.cls || ""} kind-${ev.kind.replace(".", "-")}`);
      li.dataset.p = ev.principal;
      li.append(el("span", "t", st.t.toFixed(1).padStart(4, "0")), el("span", "k", ev.kind), el("span", "who", ev.principal.split("/").pop()), el("span", "note", ev.note || ""));
      if (st.filter && ev.principal !== st.filter && !ev.principal.startsWith(st.filter + "/")) li.hidden = true;
      stream.prepend(li); while (stream.children.length > 80) stream.lastChild.remove();
      $(".p-count", root).textContent = `${fmt.int(st.events)} events`;
    },
    filter(id) {
      $(".p-scope", root).textContent = id ? id.split("/").pop() : "all agents";
      for (const li of stream.children) li.hidden = !!id && li.dataset.p !== id && !li.dataset.p.startsWith(id + "/");
      for (const [k, li] of items) li.classList.toggle("focus", k === id);
    },
    label(p, cfg) {
      const box = $(".runs", root), n = 40;
      while (box.children.length < Math.round(p * n)) {
        const i = box.children.length, pass = (i * 37 % 100) / 100 < s.retrained.pass1;
        box.append(el("i", pass ? "pass" : "fail"));
      }
      if (p > 0.7) box.classList.add("drop");
      $(".p-label .p-note", root).textContent = `${cfg.retrain.runs} passed runs kept · failed runs dropped · τ² reward + judge`;
    },
    retrain(p, cfg) {
      const L = cfg.retrain.loss, k = Math.max(2, Math.round(p * L.length)), mx = Math.max(...L);
      $(".loss path", root).setAttribute("d", L.slice(0, k).map((v, i) => `${i ? "L" : "M"}${(i / (L.length - 1) * 200).toFixed(1)},${(66 - v / mx * 60).toFixed(1)}`).join(""));
      $(".prog i", root).style.width = `${p * 100}%`;
      $(".p-retrain .p-note", root).textContent = `${cfg.retrain.method} · ${cfg.retrain.base} · ${cfg.retrain.runs} own passed runs · ${cfg.retrain.gpu_hours} GPU-h · $${cfg.retrain.usd}`;
    },
    score(p) {
      const bars = $(".bars", root);
      if (!bars.children.length) for (const [k, lbl] of [["base", "Open 9B, untrained"], ["retrained", "Open 9B, retrained"], ["opus", "Claude Opus 5.5"]]) {
        const r = el("div", `bar b-${k}`); r.append(el("span", "l", lbl), el("span", "v"), el("i")); bars.append(r);
      }
      const mx = 0.25;
      for (const k of Object.keys(pass4)) {
        const r = $(`.b-${k}`, bars); r.querySelector("i").style.width = `${(pass4[k] * p / mx) * 100}%`;
        r.querySelector(".v").textContent = `pass^4 ${(pass4[k] * p).toFixed(3)}`;
      }
      if (p >= 1) $(".p-head", root).innerHTML = `<b>${fmt.pct(X)}</b> of Opus's pass^4 at <b>1/${Math.round(Y)}</b> of the cost per task`;
    },
    overlay(which) {
      const o = $(".p-overlay", root);
      if (!which) { o.hidden = true; o.dataset.w = ""; return; }
      if (o.dataset.w === which) return;
      o.dataset.w = which; o.hidden = false;
      o.innerHTML = which === "hook"
        ? `<small>Open-source agent cloud · one minute</small><h2>Control every agent.<br>Retrain on their traces.</h2>
           <p>${fmt.pct(X)} of Claude Opus 5.5's score at 1/${Math.round(Y)} of the cost, on held-out τ²-bench retail.</p>`
        : `<small>Measured · ${cfg.sample ? "sample numbers" : "data in the repo"}</small><h2>Stop any agent, at any depth.<br>Turn its traces into a cheaper model.</h2>
           <ul><li><b>${cfg.control.stop_ms_median} ms</b> to stop one agent with ${cfg.control.vms_running} VMs running · ${cfg.control.sibling_events_lost} sibling events lost</li>
           <li><b>${cfg.trace.complete}/${cfg.trace.trials}</b> trials traced complete · ${fmt.int(cfg.trace.events_in_store)} events queryable in ${cfg.trace.query_s} s</li>
           <li><b>pass^4 ${s.retrained.pass4.toFixed(2)}</b> retrained 9B vs <b>${s.opus.pass4.toFixed(2)}</b> Opus 5.5 · <b>${Y.toFixed(0)}×</b> cheaper per task</li></ul>
           <p class="src">github.com/smahmudrahat/avos · eval, data and code</p>`;
    },
    tick(st, beat) {
      const live = [...st.nodes.keys()].filter(id => st.nodes.get(id).kind !== "microVM" && ![...st.stopped].some(x => id === x || id.startsWith(x + "/"))).length;
      const set = (k, v) => ($(`[data-c=${k}]`, root).textContent = v);
      set("agents", live); set("tokens", fmt.int(st.tokens)); set("spend", `$${st.spend.toFixed(4)}`); set("events", fmt.int(st.events));
      set("clock", `${String(Math.floor(st.t / 60)).padStart(2, "0")}:${(st.t % 60).toFixed(1).padStart(4, "0")}`);
      $(".p-track i", root).style.width = `${st.t / DURATION * 100}%`;
      $(".p-time", root).textContent = `0:${String(Math.floor(st.t)).padStart(2, "0")} / 1:00`;
      [...root.querySelectorAll(".p-beats li")].forEach((li, i) => li.classList.toggle("on", i === beat));
    },
    playing(on) { const b = $(".p-play", root); b.textContent = on ? "❚❚" : "▶"; b.setAttribute("aria-label", on ? "Pause" : "Play"); },
    onPlay(f) { $(".p-play", root).onclick = f; },
    onRestart(f) { $(".p-restart", root).onclick = f; },
  };
}
