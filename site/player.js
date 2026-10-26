// The 60-second run: Control → Trace → Retrain → Score. Control and Trace replay, event by event, what one microVM
// wrote to the host ledger in a recorded session (cfg.replay); Label, Retrain and Score show measured results.
// Nothing here is generated: no random numbers, no invented events.
const $ = (s, r = document) => r.querySelector(s);
const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
const pct = v => `${Math.round(v * 100)}%`;
const int = v => Math.round(v).toLocaleString("en-US");

const DURATION = 60, REPLAY = [4, 30];          // video seconds the recording is replayed over
const BEATS = [[0, 20, 0], [20, 36, 1], [36, 42, 2], [42, 60, 3]];
const leaf = who => who.split("/").pop();
const kindOf = who => (who === "vm" ? "microVM" : who.includes("/sub/") ? "subagent" : "agent");

export async function mount(root, { cfgUrl = "data/cfg.json" } = {}) {
  const cfg = await (await fetch(cfgUrl)).json();
  const R = cfg.replay, EV = R.events, speed = R.duration_s / (REPLAY[1] - REPLAY[0]);
  const ui = build(root, cfg, speed);
  let st, last = null, playing = false;

  function reset() {
    st = { t: 0, i: 0, live: new Set(), filter: null, events: 0, allowed: 0, denied: 0, firstHalt: null };
    ui.reset();
  }

  function apply(e) {
    st.events++;
    if (e.kind === "vm.boot") { ui.node("vm", null, `started in ${e.boot_ms} ms`); st.live.add("vm"); }
    if (e.kind === "agent.spawn") { ui.node(e.who, e.parent); st.live.add(e.who); }
    if (e.kind === "agent.exit" || e.kind === "vm.exit") { st.live.delete(e.who); ui.mark(e.who, "exited", true); }
    if (e.kind === "authz.decision" && e.action === "tool.call") {
      if (e.decision === "deny") { st.denied++; ui.flash(e.who); } else st.allowed++;
    }
    if (e.kind === "halt") {
      for (const n of e.nodes || [e.who]) ui.mark(n, e.action === "net-cut" ? "cut" : "frozen", true);
      if (!st.firstHalt && e.action === "freeze") { st.firstHalt = e; ui.receipt(e, during(e)); }
    }
    if (e.kind === "resume") for (const n of e.nodes || [e.who]) { ui.mark(n, "frozen", false); ui.mark(n, "cut", false); }
    ui.trace(e, st);
  }

  // What happened while the first halted branch was frozen, counted from the recording itself.
  function during(h) {
    const end = EV.find(x => x.kind === "resume" && x.t > h.t && x.who === h.who)?.t ?? R.duration_s;
    const frozen = new Set(h.nodes);
    const win = EV.filter(x => x.t > h.t && x.t < end);
    const sib = win.filter(x => x.kind === "authz.decision" && x.decision === "allow" && !frozen.has(x.who));
    return { seconds: end - h.t, frozen_events: win.filter(x => frozen.has(x.who)).length,
             sibling_allowed: sib.length, sibling_names: [...new Set(sib.map(x => (x.who === "vm" ? "sealed box" : leaf(x.who))))] };
  }

  function step(dt) {
    st.t = Math.min(DURATION, st.t + dt);
    const rec = Math.max(0, Math.min(R.duration_s, (st.t - REPLAY[0]) * speed));
    if (st.t >= REPLAY[0]) while (st.i < EV.length && EV[st.i].t <= rec) apply(EV[st.i++]);
    if (st.t >= 30) ui.label(Math.min(1, (st.t - 30) / 5));
    if (st.t >= 36) ui.retrain(Math.min(1, (st.t - 36) / 6));
    if (st.t >= 42) ui.score(Math.min(1, (st.t - 42) / 8));
    ui.overlay(st.t < REPLAY[0] ? "hook" : st.t >= 52 ? "end" : null);
    ui.tick(st, rec, BEATS.find(([a, b]) => st.t >= a && st.t < b)?.[2] ?? 3);
    if (st.t >= DURATION) pause();
  }

  function frame(ts) { if (!playing) return; if (last != null) step((ts - last) / 1000); last = ts; requestAnimationFrame(frame); }
  function play() { if (st.t >= DURATION) { reset(); step(0); } playing = true; last = null; ui.playing(true); requestAnimationFrame(frame); }
  function pause() { playing = false; ui.playing(false); }
  const toggle = () => (playing ? pause() : play());

  ui.onPlay(toggle);
  ui.onOverlay(toggle);
  ui.onRestart(() => { const was = playing; pause(); reset(); step(0); if (was) play(); });
  ui.onFocus(id => { st.filter = st.filter === id ? null : id; ui.filter(st.filter); });
  root.addEventListener("keydown", e => { if (e.code === "Space") { e.preventDefault(); toggle(); } });
  new IntersectionObserver(([en]) => { if (!en.isIntersecting && playing) pause(); }).observe(root);
  reset(); step(0);
}

function build(root, cfg, speed) {
  const s = cfg.score, X = s.x.value, Y = s.y.value, R = cfg.replay;
  root.tabIndex = 0;
  root.classList.add("player");
  root.innerHTML = `
    <div class="p-bar">
      <div class="p-id"><b>Recording</b>from our servers, ${R.events[0].ts.slice(0, 10)}, shown at ${speed.toFixed(1)}× speed</div>
      <ol class="p-beats"><li>Control</li><li>Record</li><li>Retrain</li><li>Score</li></ol>
      <div class="p-counters">
        <div><span data-c="agents">0</span><small>agents running</small></div>
        <div><span data-c="allowed">0</span><small>actions allowed</small></div>
        <div><span data-c="denied">0</span><small>blocked by our rules</small></div>
        <div><span data-c="events">0</span><small>actions recorded</small></div>
        <div><span data-c="clock">00.0 s</span><small>elapsed</small></div>
      </div>
    </div>
    <div class="p-grid">
      <section class="p-panel p-tree"><h3>Agents</h3>
        <div class="p-legend"><i class="k-microVM"></i>sealed box <i class="k-agent"></i>agent <i class="k-subagent"></i>subagent</div>
        <ul class="tree"></ul><div class="p-receipt" hidden></div></section>
      <section class="p-panel p-trace"><h3>Actions by <span class="p-scope">all agents</span> <em class="p-count">0 recorded</em></h3><ol class="stream"></ol></section>
      <section class="p-panel p-right">
        <div class="p-sub p-label"><h3>Sort</h3><div class="lab"></div><p class="p-note"></p></div>
        <div class="p-sub p-retrain"><h3>Retrain</h3><svg class="loss" viewBox="0 0 200 70" preserveAspectRatio="none"><path/></svg>
          <div class="prog"><i></i></div><p class="p-note"></p></div>
        <div class="p-sub p-score"><h3>Score</h3><div class="bars"></div><p class="p-head"></p></div>
      </section>
    </div>
    <div class="p-overlay" hidden></div>
    <div class="p-ctl"><button class="p-play" aria-label="Play">▶</button><div class="p-track"><i></i></div>
      <span class="p-time">0:00 / 1:00</span><button class="p-restart">Restart</button></div>`;

  const tree = $(".tree", root), stream = $(".stream", root), items = new Map();
  let focusCb = () => {};
  const pass4 = { base: s.base.pass4, retrained: s.retrained.pass4, opus: s.opus.pass4 };

  const describe = e => {
    if (e.kind === "authz.decision") {
      const what = (e.resource || "").replace(/^\w+::/, "").replace(/"/g, "");
      const amt = e.amount_cents != null ? ` $${(e.amount_cents / 100).toFixed(2)}` : "";
      const deny = e.decision === "deny";
      return [`${e.decision} ${what}${amt}${deny ? ` (${(e.policy_ids || []).join(", ")})` : ""}`, deny ? "bad" : ""];
    }
    if (e.kind === "tool.call") return [`${e.tool || "tool"} ran`, ""];
    if (e.kind === "llm.call") return [`${(e.model || "").split("/").pop()}, ${e.status}, ${e.prompt_tokens ?? 0}+${e.completion_tokens ?? 0} tokens, ${e.ms ?? 0} ms`, ""];
    const n = (e.nodes || []).length;
    if (e.kind === "halt") return [`${e.action} ${e.receipt}, ${n} node${n === 1 ? "" : "s"}, ${e.ms} ms`, "halt"];
    if (e.kind === "resume") return [`${e.action || "resume"} ${e.receipt || ""}, ${e.ms ?? 0} ms`, "halt"];
    if (e.kind === "vm.boot") return [`sealed box started in ${e.boot_ms} ms`, ""];
    return ["", ""];
  };

  return {
    reset() {
      tree.innerHTML = ""; stream.innerHTML = ""; items.clear();
      $(".lab", root).innerHTML = ""; $(".p-label .p-note", root).textContent = "";
      $(".loss path", root).setAttribute("d", ""); $(".prog i", root).style.width = "0";
      $(".p-retrain .p-note", root).textContent = ""; $(".bars", root).innerHTML = ""; $(".p-head", root).textContent = "";
      $(".p-receipt", root).hidden = true; $(".p-scope", root).textContent = "all agents";
    },
    node(who, parent, note) {
      if (items.has(who)) return;
      const li = el("li", `node k-${kindOf(who)}`), row = el("div", "row");
      row.append(el("i", "dot"), el("span", "name", who === "vm" ? "sealed box" : leaf(who)), el("span", "kind", note || kindOf(who)));
      row.onclick = () => focusCb(who);
      li.append(row, el("ul"));
      const host = parent && parent !== "vm" ? items.get(parent) : items.get("vm");
      (host && who !== "vm" ? $("ul", host) : tree).append(li);
      items.set(who, li);
    },
    mark(who, cls, on) { items.get(who)?.classList.toggle(cls, on); },
    flash(who) { const li = items.get(who); if (!li) return; li.classList.remove("bad"); void li.offsetWidth; li.classList.add("bad"); },
    receipt(h, d) {
      const box = $(".p-receipt", root); box.hidden = false;
      box.innerHTML = `<b>Stop receipt ${h.receipt}</b><span>${h.action} ${h.nodes.map(leaf).join(" + ")} in ${h.ms} ms</span>
        <span>Paused for ${d.seconds.toFixed(1)} s. The stopped agents took ${d.frozen_events} actions. The others had ${d.sibling_allowed} actions allowed: ${d.sibling_names.join(", ") || "the other agents"}.</span>`;
    },
    trace(e, st) {
      const [note, cls] = describe(e);
      const li = el("li", `ev ${cls} kind-${e.kind.replace(".", "-")}`);
      li.dataset.p = e.who;
      li.append(el("span", "t", e.t.toFixed(1).padStart(4, "0")), el("span", "k", e.kind), el("span", "who", e.who === "vm" ? "sealed box" : leaf(e.who)), el("span", "note", note));
      if (st.filter && e.who !== st.filter && !e.who.startsWith(st.filter + "/")) li.hidden = true;
      stream.prepend(li); while (stream.children.length > 80) stream.lastChild.remove();
      $(".p-count", root).textContent = `${int(st.events)} recorded`;
    },
    filter(id) {
      $(".p-scope", root).textContent = id ? (id === "vm" ? "sealed box" : leaf(id)) : "all agents";
      for (const li of stream.children) li.hidden = !!id && li.dataset.p !== id && !li.dataset.p.startsWith(id + "/");
      for (const [k, li] of items) li.classList.toggle("focus", k === id);
    },
    label(p) {
      const box = $(".lab", root);
      if (!box.children.length) for (const f of cfg.labels) {
        const r = el("div", "lrow"); r.append(el("span", "l", `${f.fleet}, ${int(f.runs)} runs`), el("span", "v", `${f.kept} kept`));
        const bar = el("div", "lbar"); bar.append(el("i", "kept"), el("i", "drop")); r.append(bar); box.append(r);
      }
      [...box.children].forEach((r, i) => {
        const f = cfg.labels[i];
        r.querySelector(".kept").style.width = `${f.kept / f.runs * 100 * p}%`;
        r.querySelector(".drop").style.width = `${f.dropped / f.runs * 100 * p}%`;
      });
      $(".p-label .p-note", root).textContent = `We keep runs that passed the test and a second check by an AI judge. ${cfg.retrain.runs} kept runs trained the model.`;
    },
    retrain(p) {
      const L = cfg.retrain.loss, k = Math.max(2, Math.round(p * L.length)), mx = Math.max(...L);
      $(".loss path", root).setAttribute("d", L.slice(0, k).map((v, i) => `${i ? "L" : "M"}${(i / (L.length - 1) * 200).toFixed(1)},${(66 - v / mx * 60).toFixed(1)}`).join(""));
      $(".prog i", root).style.width = `${p * 100}%`;
      $(".p-retrain .p-note", root).textContent = `Training took ${cfg.retrain.gpu_hours} GPU-hours and cost $${cfg.retrain.usd}. The line shows its error rate falling.`;
    },
    score(p) {
      const bars = $(".bars", root);
      if (!bars.children.length) for (const [k, lbl] of [["base", "Open model, untrained"], ["retrained", "Open model, retrained"], ["opus", "Claude Opus 5.5"]]) {
        const r = el("div", `bar b-${k}`); r.append(el("span", "l", lbl), el("span", "v"), el("i")); bars.append(r);
      }
      for (const k of Object.keys(pass4)) {
        const r = $(`.b-${k}`, bars); r.querySelector("i").style.width = `${pass4[k] * p / 0.25 * 100}%`;
        r.querySelector(".v").textContent = `${(pass4[k] * p * 100).toFixed(1)}% solved 4 of 4`;
      }
      if (p >= 1) $(".p-head", root).innerHTML = `<b>${pct(X)}</b> of Opus's Solved 4 of 4 score, a statistical tie, at <b>1/${Math.round(Y)}</b> of the cost per task`;
    },
    overlay(which) {
      const o = $(".p-overlay", root);
      if (!which) { o.hidden = true; o.dataset.w = ""; return; }
      if (o.dataset.w === which) return;
      o.dataset.w = which; o.hidden = false;
      const c = cfg.control, t = cfg.trace;
      o.innerHTML = which === "hook"
        ? `<small>The result first</small><h2>${pct(X)} of Claude Opus 5.5's score,<br>at 1/${Math.round(Y)} of the cost per task.</h2>
           <p>We retrained a small open model on its own successful work. Then we tested it on customer-service tasks it had never seen.</p>
           <p>The score is the stricter Solved 4 of 4 measure, and the gap is within the margin of error. Here is how it got there.</p>
           <p class="src">A real recording and measured results. All data is on GitHub.</p>`
        : `<small>Measured results</small><h2>Stop any agent, even one started by another agent.<br>Turn its work into a cheaper model.</h2>
           <ul><li><b>${c.stop_ms_median} ms</b> to stop one agent, with ${c.vms_running} agents running. The others lost ${c.sibling_events_lost} actions.</li>
           <li><b>${t.complete} of ${t.trials}</b> test runs recorded in full</li>
           <li><b>${pct(X)}</b> of Claude Opus 5.5's Solved 4 of 4 score, a statistical tie, at <b>${Y.toFixed(0)}×</b> lower cost per task</li></ul>
           <p class="src">Code and data: github.com/smahmudrahat/avos</p>`;
    },
    tick(st, rec, beat) {
      const set = (k, v) => ($(`[data-c=${k}]`, root).textContent = v);
      set("agents", [...st.live].filter(w => w !== "vm").length); set("allowed", int(st.allowed)); set("denied", int(st.denied));
      set("events", int(st.events)); set("clock", `${rec.toFixed(1).padStart(4, "0")} s`);
      $(".p-track i", root).style.width = `${st.t / DURATION * 100}%`;
      $(".p-time", root).textContent = `0:${String(Math.floor(st.t)).padStart(2, "0")} / 1:00`;
      [...root.querySelectorAll(".p-beats li")].forEach((li, i) => li.classList.toggle("on", i === beat));
    },
    playing(on) { const b = $(".p-play", root); b.textContent = on ? "❚❚" : "▶"; b.setAttribute("aria-label", on ? "Pause" : "Play"); },
    onPlay(f) { $(".p-play", root).onclick = f; },
    onOverlay(f) { $(".p-overlay", root).onclick = f; },
    onRestart(f) { $(".p-restart", root).onclick = f; },
    onFocus(f) { focusCb = f; },
  };
}
