"use strict";

const DECAY_PER_S = 0.9;      // meter fall-off, fraction of full scale per second
const METER_FLOOR_DB = 60;    // bottom of the meter scale
const SEND_INTERVAL_MS = 60;  // at most one volume request per interval while dragging
const LOCK_MS = 600;          // ignore server echoes this long after the user touched a control

const EQ_BANDS = [["low", "Low"], ["mid", "Mid"], ["high", "High"]];
const EQ_STEP = 0.5;          // dB per keyboard step; dragging snaps to the same grid

const $ = (sel) => document.querySelector(sel);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const state = { max: 100, eqRange: 0 };
const strips = new Map();   // strip id -> Strip (mixer tab)
const eqStrips = new Map(); // strip id -> EqStrip (EQ tab)

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function post(path, body) {
  return fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).catch(() => {});
}

function peakToFraction(peak) {
  if (!(peak > 0.001)) return 0;
  return clamp((20 * Math.log10(peak) + METER_FLOOR_DB) / METER_FLOOR_DB, 0, 1);
}

// Name and subtitle, shared by the strips of both tabs.
function applyLabel(strip, item) {
  if (strip.nameEl.textContent !== item.name) strip.nameEl.textContent = item.name;
  if (strip.subEl.textContent !== item.sub) strip.subEl.textContent = item.sub;
  strip.el.title = item.sub ? `${item.name} - ${item.sub}` : item.name;
}

function makeLabel(strip) {
  const label = el("div", "label");
  strip.nameEl = el("div", "name");
  strip.subEl = el("div", "sub");
  label.append(strip.nameEl, strip.subEl);
  return label;
}

const fmtDb = (v) => (v === 0 ? "0" : (v > 0 ? "+" : "−") + Math.abs(v).toFixed(Number.isInteger(v) ? 0 : 1));

class Strip {
  constructor(id, group) {
    this.id = id;
    this.group = group;
    this.value = 0;
    this.mute = false;
    this.peak = 0;       // latest level from the server (0..1 on the meter scale)
    this.shown = 0;      // level currently drawn (decays smoothly)
    this.drawn = -1;
    this.dragging = false;
    this.volLock = 0;
    this.muteLock = 0;
    this.sendTimer = null;
    this.sent = null;
    this.scaleMax = 0;
    this.build();
  }

  build() {
    const kind = this.id.split(":")[0];
    this.el = el("div", `strip ${kind}`);

    const label = makeLabel(this);

    const channel = el("div", "channel");
    const scale = el("div", "scale");
    this.ticks = el("div", "ticks");
    scale.append(this.ticks);

    this.fader = el("div", "fader");
    this.fader.tabIndex = 0;
    this.fader.setAttribute("role", "slider");
    this.fader.setAttribute("aria-orientation", "vertical");
    this.fader.setAttribute("aria-valuemin", "0");
    this.track = el("div", "track");
    this.fill = el("div", "fill");
    this.thumb = el("div", "thumb");
    this.track.append(el("div", "rail"), this.fill, this.thumb);
    this.fader.append(this.track);

    const meter = el("div", "meter");
    this.cover = el("div", "cover");
    meter.append(this.cover);
    channel.append(scale, this.fader, meter);

    this.readout = el("output", "readout");
    this.muteBtn = el("button", "mute", "Mute");
    this.muteBtn.type = "button";

    this.el.append(label, channel, this.readout, this.muteBtn);
    this.wire();
  }

  wire() {
    const valueAt = (e) => {
      const r = this.track.getBoundingClientRect();
      return Math.round(clamp(1 - (e.clientY - r.top) / r.height, 0, 1) * state.max);
    };
    this.fader.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      this.fader.setPointerCapture(e.pointerId);
      this.dragging = true;
      this.setValue(valueAt(e));
      e.preventDefault();
      this.fader.focus({ preventScroll: true });
    });
    this.fader.addEventListener("pointermove", (e) => {
      if (this.dragging) this.setValue(valueAt(e));
    });
    const release = () => {
      if (!this.dragging) return;
      this.dragging = false;
      this.volLock = performance.now() + LOCK_MS;
    };
    this.fader.addEventListener("pointerup", release);
    this.fader.addEventListener("pointercancel", release);
    this.fader.addEventListener("dblclick", () => this.setValue(Math.min(100, state.max)));
    this.fader.addEventListener("keydown", (e) => {
      const step = { ArrowUp: 1, ArrowRight: 1, ArrowDown: -1, ArrowLeft: -1, PageUp: 10, PageDown: -10 }[e.key];
      if (step !== undefined) this.setValue(clamp(this.value + step, 0, state.max));
      else if (e.key === "Home") this.setValue(0);
      else if (e.key === "End") this.setValue(state.max);
      else return;
      e.preventDefault();
    });
    this.muteBtn.addEventListener("click", () => {
      this.mute = !this.mute;
      this.muteLock = performance.now() + LOCK_MS;
      this.render();
      post("/api/mute", { id: this.id, mute: this.mute });
    });
  }

  setValue(v) {
    this.value = clamp(v, 0, state.max);
    this.volLock = performance.now() + LOCK_MS;
    this.render();
    this.queueSend();
  }

  // Send immediately, then at most once per interval; the trailing send
  // guarantees the final position always reaches the server.
  queueSend() {
    if (this.sendTimer) return;
    this.sent = this.value;
    post("/api/volume", { id: this.id, volume: this.value });
    this.sendTimer = setTimeout(() => {
      this.sendTimer = null;
      if (this.sent !== this.value) this.queueSend();
    }, SEND_INTERVAL_MS);
  }

  update(item) {
    applyLabel(this, item);
    this.fader.setAttribute("aria-label", `${item.name} volume`);
    const now = performance.now();
    if (!this.dragging && now > this.volLock) this.value = item.volume;
    if (now > this.muteLock) this.mute = item.mute;
    this.render();
  }

  render() {
    const pct = clamp((this.value / state.max) * 100, 0, 100);
    this.thumb.style.bottom = `${pct}%`;
    this.fill.style.height = `${pct}%`;
    this.readout.textContent = `${this.value}%`;
    this.fader.setAttribute("aria-valuenow", String(this.value));
    this.fader.setAttribute("aria-valuetext", `${this.value}%`);
    this.fader.setAttribute("aria-valuemax", String(state.max));
    this.muteBtn.setAttribute("aria-pressed", String(this.mute));
    this.muteBtn.textContent = this.mute ? "Muted" : "Mute";
    this.el.classList.toggle("muted", this.mute);
    if (this.scaleMax !== state.max) this.buildScale();
  }

  buildScale() {
    this.scaleMax = state.max;
    this.ticks.replaceChildren();
    for (let v = 0; v <= state.max; v += 25) {
      const t = el("span", "tick", String(v));
      t.style.bottom = `${(v / state.max) * 100}%`;
      this.ticks.append(t);
    }
  }

  // Returns true while the meter still needs animation frames.
  tickMeter(dt) {
    this.shown = Math.max(this.peak, this.shown - dt * DECAY_PER_S);
    if (this.shown < 0.002) this.shown = 0;
    if (Math.abs(this.shown - this.drawn) > 0.004 || (this.shown === 0 && this.drawn !== 0)) {
      this.drawn = this.shown;
      this.cover.style.transform = `scaleY(${1 - this.shown})`;
    }
    return this.shown > 0 || this.peak > 0;
  }
}

// ---- equalizer ----------------------------------------------------------------
// One vertical +/- slider (dB) for a single band; owns its drag/keyboard handling.
class BandSlider {
  constructor(label, onInput) {
    this.value = 0;
    this.lock = 0;        // ignore server echoes until then
    this.dragging = false;
    this.onInput = onInput;

    this.el = el("div", "fader eqslider");
    this.el.tabIndex = 0;
    this.el.setAttribute("role", "slider");
    this.el.setAttribute("aria-orientation", "vertical");
    this.el.setAttribute("aria-label", `${label} gain`);
    this.track = el("div", "track");
    this.fill = el("div", "fill");
    this.thumb = el("div", "thumb");
    this.track.append(el("div", "rail"), el("div", "zero"), this.fill, this.thumb);
    this.el.append(this.track);
    this.readout = el("output", "eqvalue");
    this.wire();
  }

  wire() {
    const valueAt = (e) => {
      const r = this.track.getBoundingClientRect();
      const v = (clamp(1 - (e.clientY - r.top) / r.height, 0, 1) * 2 - 1) * state.eqRange;
      const snapped = Math.round(v / EQ_STEP) * EQ_STEP;
      return Math.abs(snapped) <= EQ_STEP ? 0 : snapped; // a little detent at 0 dB
    };
    this.el.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      this.el.setPointerCapture(e.pointerId);
      this.dragging = true;
      this.set(valueAt(e));
      e.preventDefault();
      this.el.focus({ preventScroll: true });
    });
    this.el.addEventListener("pointermove", (e) => {
      if (this.dragging) this.set(valueAt(e));
    });
    const release = () => {
      if (!this.dragging) return;
      this.dragging = false;
      this.lock = performance.now() + LOCK_MS;
    };
    this.el.addEventListener("pointerup", release);
    this.el.addEventListener("pointercancel", release);
    this.el.addEventListener("dblclick", () => this.set(0));
    this.el.addEventListener("keydown", (e) => {
      const step = { ArrowUp: EQ_STEP, ArrowRight: EQ_STEP, ArrowDown: -EQ_STEP, ArrowLeft: -EQ_STEP, PageUp: 3, PageDown: -3 }[e.key];
      if (step !== undefined) this.set(this.value + step);
      else if (e.key === "Home") this.set(-state.eqRange);
      else if (e.key === "End") this.set(state.eqRange);
      else if (e.key === "0" || e.key === "Delete" || e.key === "Backspace") this.set(0);
      else return;
      e.preventDefault();
    });
  }

  set(v) {
    this.value = clamp(v, -state.eqRange, state.eqRange);
    this.lock = performance.now() + LOCK_MS;
    this.render();
    this.onInput(this.value);
  }

  render() {
    const range = state.eqRange || 1;
    const pct = (clamp(this.value / range, -1, 1) + 1) * 50;   // 0..100, 0 dB at 50
    this.thumb.style.bottom = `${pct}%`;
    this.fill.style.bottom = `${Math.min(pct, 50)}%`;           // grows out of the 0 dB line
    this.fill.style.height = `${Math.abs(pct - 50)}%`;
    this.readout.textContent = fmtDb(this.value);
    this.readout.classList.toggle("off", this.value === 0);
    this.el.setAttribute("aria-valuemin", String(-range));
    this.el.setAttribute("aria-valuemax", String(range));
    this.el.setAttribute("aria-valuenow", String(this.value));
    this.el.setAttribute("aria-valuetext", `${fmtDb(this.value)} dB`);
  }
}

// Low / Mid / High sliders of one application stream.
class EqStrip {
  constructor(id, group) {
    this.id = id;
    this.group = group;
    this.sliders = {};
    this.pending = {};   // band -> value not yet sent
    this.sendTimer = null;
    this.scaleRange = 0;
    this.build();
  }

  build() {
    this.el = el("div", "strip eq");
    const label = makeLabel(this);

    const scale = el("div", "scale");
    this.ticks = el("div", "ticks");
    scale.append(this.ticks);
    const sliders = [scale], values = [el("div")], names = [el("div")];
    for (const [band, text] of EQ_BANDS) {
      const slider = new BandSlider(text, (v) => this.queueSend(band, v));
      this.sliders[band] = slider;
      sliders.push(slider.el);
      values.push(slider.readout);
      names.push(el("div", "eqname", text));
    }
    const grid = el("div", "eqgrid");
    grid.append(...sliders, ...values, ...names);

    this.flatBtn = el("button", "mute", "Flat");
    this.flatBtn.type = "button";
    this.flatBtn.addEventListener("click", () => this.flat());
    this.el.append(label, grid, this.flatBtn);
  }

  // Same throttling as the faders: send at once, then at most one request per
  // interval, and the trailing send guarantees the final position arrives.
  queueSend(band, v) {
    this.pending[band] = v;
    this.syncFlat();
    if (!this.sendTimer) this.flush();
  }

  flush() {
    const gains = this.pending;
    this.pending = {};
    post("/api/eq", { id: this.id, gains });
    this.sendTimer = setTimeout(() => {
      this.sendTimer = null;
      if (Object.keys(this.pending).length) this.flush();
    }, SEND_INTERVAL_MS);
  }

  flat() {
    clearTimeout(this.sendTimer);
    this.sendTimer = null;
    this.pending = {};
    const gains = {};
    for (const [band] of EQ_BANDS) {
      const s = this.sliders[band];
      s.value = 0;
      s.lock = performance.now() + LOCK_MS;
      s.render();
      gains[band] = 0;
    }
    this.syncFlat();
    post("/api/eq", { id: this.id, gains });
  }

  syncFlat() {
    this.flatBtn.disabled = EQ_BANDS.every(([band]) => this.sliders[band].value === 0);
  }

  update(item) {
    applyLabel(this, item);
    const now = performance.now();
    for (const [band, text] of EQ_BANDS) {
      const s = this.sliders[band];
      s.el.setAttribute("aria-label", `${item.name} ${text} gain`);
      if (!s.dragging && now > s.lock) s.value = (item.eq && item.eq[band]) || 0;
      s.render();
    }
    this.syncFlat();
    if (this.scaleRange !== state.eqRange) this.buildScale();
  }

  buildScale() {
    this.scaleRange = state.eqRange;
    this.ticks.replaceChildren();
    for (const [v, pct] of [[state.eqRange, 100], [0, 50], [-state.eqRange, 0]]) {
      const t = el("span", "tick", fmtDb(v));
      t.style.bottom = `${pct}%`;
      this.ticks.append(t);
    }
  }
}

// ---- meter animation ------------------------------------------------------
let raf = 0;
let lastFrame = 0;
function frame(now) {
  const dt = Math.min(0.1, (now - lastFrame) / 1000);
  lastFrame = now;
  let active = false;
  for (const s of strips.values()) active = s.tickMeter(dt) || active;
  raf = active ? requestAnimationFrame(frame) : 0;
}
function kickMeters() {
  if (raf) return;
  lastFrame = performance.now();
  raf = requestAnimationFrame(frame);
}
function applyMeters(levels) {
  for (const s of strips.values()) s.peak = peakToFraction(levels[s.id] || 0);
  kickMeters();
}

// ---- state ------------------------------------------------------------------
function syncGroup(map, Cls, group, items, container) {
  const seen = new Set();
  items.forEach((item, i) => {
    seen.add(item.id);
    let strip = map.get(item.id);
    if (!strip) {
      strip = new Cls(item.id, group);
      map.set(item.id, strip);
    }
    // Only touch the DOM when the order really changed (re-inserting a node
    // would interrupt a drag in progress).
    if (container.children[i] !== strip.el) container.insertBefore(strip.el, container.children[i] || null);
    strip.update(item);
  });
  for (const [id, strip] of map) {
    if (strip.group === group && !seen.has(id)) {
      strip.el.remove();
      map.delete(id);
    }
  }
}

function applyState(s) {
  state.max = s.max_volume;
  state.eqRange = s.eq_range_db || 0;
  const input = [...(s.mic ? [s.mic] : []), ...s.recording];
  syncGroup(strips, Strip, "playback", s.playback, $("#playback"));
  syncGroup(strips, Strip, "input", input, $("#input"));
  syncGroup(strips, Strip, "master", s.master ? [s.master] : [], $("#master"));
  $("#empty").hidden = s.playback.length > 0;
  $("#g-input").hidden = input.length === 0;
  $("#g-master").hidden = !s.master;

  // EQ tab: one strip per application stream (none if the server has no EQ)
  syncGroup(eqStrips, EqStrip, "eq", state.eqRange ? s.playback : [], $("#eq-strips"));
  $("#eq-empty").hidden = !state.eqRange || s.playback.length > 0;
  $("#eq-unavailable").hidden = state.eqRange > 0;
  $("#eq-note").hidden = !state.eqRange;
  $("#eq-dot").hidden = !state.eqRange || !s.playback.some((it) => it.eq && Object.values(it.eq).some((g) => g !== 0));
}

// ---- tabs -------------------------------------------------------------------
const VIEWS = ["mixer", "eq"];
function showView(name, focus) {
  if (!VIEWS.includes(name)) name = "mixer";
  for (const view of VIEWS) {
    const tab = $(`#tab-${view}`);
    tab.setAttribute("aria-selected", String(view === name));
    tab.tabIndex = view === name ? 0 : -1;
    $(`#view-${view}`).hidden = view !== name;
  }
  if (focus) $(`#tab-${name}`).focus();
  // Remember the tab across reloads (in the URL only; the server keeps no state).
  history.replaceState(null, "", name === "mixer" ? location.pathname + location.search : `#${name}`);
}

function initTabs() {
  for (const view of VIEWS) {
    const tab = $(`#tab-${view}`);
    tab.addEventListener("click", () => showView(view));
    tab.addEventListener("keydown", (e) => {
      const dir = { ArrowLeft: -1, ArrowRight: 1 }[e.key];
      if (!dir) return;
      showView(VIEWS[(VIEWS.indexOf(view) + dir + VIEWS.length) % VIEWS.length], true);
      e.preventDefault();
    });
  }
  showView(location.hash.slice(1));
  window.addEventListener("hashchange", () => showView(location.hash.slice(1)));
}

// ---- connection -------------------------------------------------------------
function setStatus(kind, text) {
  const node = $("#status");
  node.dataset.state = kind;
  node.textContent = text;
}

function connect() {
  const es = new EventSource("/api/events");
  es.onopen = () => setStatus("live", "live");
  es.addEventListener("state", (e) => applyState(JSON.parse(e.data)));
  es.addEventListener("meters", (e) => applyMeters(JSON.parse(e.data)));
  es.onerror = () => {
    setStatus("down", "reconnecting…");
    applyMeters({});
    if (es.readyState === EventSource.CLOSED) setTimeout(connect, 2000);
  };
}

initTabs();
connect();
