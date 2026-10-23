// Kiester front end — plain ES2020, no build step, no dependencies.
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const mediaUrl = rel => "/media/" + String(rel).split("/").map(encodeURIComponent).join("/");
// audio always starts at the model default (on): it is never remembered between visits or carried across models
const AUDIO_KEYS = ["audio", "enable_audio", "generate_audio"];
const ACTIVE = ["queued", "uploading", "submitting", "running", "downloading"];
const STATUS_LABEL = {queued: "queued", uploading: "uploading inputs", submitting: "submitting", running: "generating",
                      downloading: "saving", success: "done", fail: "failed"};
const uid = () => (crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2));
const store = {  // localStorage can throw (private mode, blocked storage) — never let it break the page
  get(k, d) { try { const v = localStorage.getItem("kst." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("kst." + k, JSON.stringify(v)); } catch {} },
};

// ------------------------------------------------------------------ api
async function api(method, path, body) {
  const opt = {method, headers: {"X-Kiester": "1"}};
  if (body instanceof FormData) opt.body = body;
  else if (body !== undefined) { opt.body = JSON.stringify(body); opt.headers["Content-Type"] = "application/json"; }
  let r;
  try { r = await fetch(path, opt); }
  catch { throw new Error("Server not reachable — is kiester running?"); }
  let data = null;
  try { data = await r.json(); } catch {}
  if (!r.ok) throw new Error((data && data.error) || `${r.status} ${r.statusText}`);
  return data;
}

function toast(msg, kind = "", ms = 5000) {
  const t = document.createElement("div");
  t.className = "toast " + kind; t.textContent = msg; t.onclick = () => t.remove();
  $("#toasts").append(t);
  if (ms) setTimeout(() => t.remove(), kind === "bad" ? ms * 2 : ms);
}
const fail = e => toast(e.message || String(e), "bad");

// ------------------------------------------------------------------ state
const S = {
  reg: null, creditUsd: 0.005, kind: store.get("kind", "video"),
  draft: {},             // per kind: {model, mode, params:{}, slots:{key:[asset]}, prompt}
  balance: null, health: {},
  jobs: new Map(),       // id -> job (history + active)
  order: [],             // ids shown in history, newest first
  lastSync: 0, exhausted: false, sel: new Set(),
  libTarget: null, busy: false, estimateSeq: 0,
};

const model = () => S.reg[S.kind][d().model];
const mode = () => model().modes[d().mode];
const pspec = () => [...model().params, ...(mode().extra || [])];
function d() { return S.draft[S.kind]; }

function freshDraft(kind) {
  const keys = Object.keys(S.reg[kind]);
  const m = keys[0];
  return {model: m, mode: Object.keys(S.reg[kind][m].modes)[0], params: {}, slots: {}, prompt: ""};
}

function loadDrafts() {
  for (const kind of ["video", "image"]) {
    const saved = store.get("draft." + kind, null);
    const dr = freshDraft(kind);
    if (saved && S.reg[kind][saved.model]) {
      dr.model = saved.model;
      dr.mode = S.reg[kind][saved.model].modes[saved.mode] ? saved.mode : Object.keys(S.reg[kind][saved.model].modes)[0];
      dr.params = Object.fromEntries(Object.entries(saved.params || {}).filter(([k]) => !AUDIO_KEYS.includes(k))); dr.prompt = saved.prompt || ""; dr.slots = saved.slots || {};
    }
    S.draft[kind] = dr;
  }
}
let saveTimer;
function saveDraft() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => { for (const k of ["video", "image"]) store.set("draft." + k, S.draft[k]); store.set("kind", S.kind); }, 300);
}

// ------------------------------------------------------------------ composer
function renderComposer() {
  const reg = S.reg[S.kind];
  // model picker, grouped by vendor
  const groups = {};
  for (const [k, m] of Object.entries(reg)) (groups[m.vendor || "Other"] ||= []).push([k, m]);
  $("#model").innerHTML = Object.entries(groups).map(([v, ms]) =>
    `<optgroup label="${esc(v)}">${ms.map(([k, m]) => `<option value="${esc(k)}">${esc(m.label)}${m.price ? "" : " · price unlisted"}</option>`).join("")}</optgroup>`).join("");
  $("#model").value = d().model;
  $("#modes").innerHTML = Object.entries(model().modes).map(([k, mo]) =>
    `<button type="button" data-mode="${esc(k)}" class="${k === d().mode ? "active" : ""}">${esc(mo.label)}</button>`).join("");
  $("#prompt").value = d().prompt;
  $("#prompt").placeholder = S.kind === "video"
    ? "Describe the shot: subject, action, camera move, lighting, sound…"
    : "Describe the image: subject, style, composition, lighting…";
  renderSlots(); renderParams(); renderHealth(); updateHint(); estimate();
}

function renderSlots() {
  const slots = mode().slots;
  $("#slots").innerHTML = slots.map(s => {
    const items = d().slots[s.key] || [], max = s.max || 1;
    return `<div class="slot" data-slot="${esc(s.key)}">
      <div class="head"><span>${esc(s.label)} ${s.required ? '<span class="req">required</span>' : '<span>optional</span>'}</span>
        <span>${max > 1 ? `${items.length}/${max}` : ""}</span></div>
      <div class="thumbs">${items.map((a, i) => `
        <div class="thumb" title="${esc(a.label || "")} ${a.w ? a.w + "×" + a.h : ""}">
          <img src="${esc(a.url)}" alt="" loading="lazy">
          ${max > 1 ? `<span class="n">${s.key.includes("reference") ? "@Image" : "#"}${i + 1}</span>` : ""}
          <button class="x" data-rm="${i}" title="Remove">✕</button>
          ${max > 1 && items.length > 1 ? `<span class="mv">${i ? `<button data-mv="${i}" data-dir="-1" title="Move earlier">◀</button>` : ""}${i < items.length - 1 ? `<button data-mv="${i}" data-dir="1" title="Move later">▶</button>` : ""}</span>` : ""}
        </div>`).join("")}
        ${items.length < max ? `<button type="button" class="addbtn" data-add>＋ Add<br><small>drop / paste</small></button>` : ""}
      </div></div>`;
  }).join("");
}

function setSlot(key, assets) {
  const s = mode().slots.find(x => x.key === key); if (!s) return;
  d().slots[key] = assets.slice(0, s.max || 1);
  renderSlots(); updateHint(); saveDraft();
}
function addToSlot(key, assets) {
  const s = mode().slots.find(x => x.key === key); if (!s) return;
  const max = s.max || 1;
  let cur = max === 1 ? [] : (d().slots[key] || []).filter(a => !assets.some(b => b.id === a.id));
  const merged = [...cur, ...assets];
  if (merged.length > max) toast(`${s.label}: only ${max} allowed; kept the first ${max}.`);
  setSlot(key, merged);
}
// first slot that still has room, for paste / "use as input"
function defaultSlot() {
  const slots = mode().slots;
  return (slots.find(s => !(d().slots[s.key] || []).length) || slots.find(s => (d().slots[s.key] || []).length < (s.max || 1)) || slots[0] || {}).key;
}

async function uploadFiles(files) {
  files = [...files].filter(f => f.type.startsWith("image/") || /\.(heic|heif|jpe?g|png|webp|gif|bmp|tiff?)$/i.test(f.name));
  if (!files.length) { toast("No image files in that drop.", "bad"); return []; }
  const big = files.find(f => f.size > 150e6);
  if (big) { toast(`${big.name} is over 150 MB.`, "bad"); return []; }
  const fd = new FormData(); files.forEach(f => fd.append("files", f));
  const t = toast(`Uploading ${files.length} image${files.length > 1 ? "s" : ""}…`, "", 0);
  try {
    const r = await api("POST", "/api/assets", fd);
    r.errors.forEach(e => toast(e, "bad"));
    r.assets.forEach(a => (a.notes || []).forEach(n => toast(`${a.label || a.id}: ${n}`)));
    return r.assets;
  } catch (e) { fail(e); return []; }
  finally { $$(".toast").filter(x => x.textContent.startsWith("Uploading")).forEach(x => x.remove()); }
}

function renderParams() {
  const vals = d().params;
  $("#params").innerHTML = pspec().map(p => {
    const v = vals[p.key] ?? p.default, label = esc(p.label || p.key.replace(/_/g, " "));
    if (p.type === "bool")
      return `<label class="toggle full"><input type="checkbox" data-p="${esc(p.key)}" ${v ? "checked" : ""}> ${label}</label>`;
    if (p.type === "select" && p.opts.length <= 4 && p.opts.join("").length < 26)
      return `<div class="field full"><span>${label}</span><div class="seg">${p.opts.map((o, i) =>
        `<button type="button" data-p="${esc(p.key)}" data-i="${i}" class="${String(o) === String(v) ? "active" : ""}">${esc(o)}</button>`).join("")}</div></div>`;
    if (p.type === "select")
      return `<label class="field"><span>${label}</span><select data-p="${esc(p.key)}">${p.opts.map((o, i) =>
        `<option value="${i}" ${String(o) === String(v) ? "selected" : ""}>${esc(o)}</option>`).join("")}</select></label>`;
    return `<label class="field"><span>${label}${p.min != null && p.key !== "seed" ? ` <small>${p.min}–${p.max}</small>` : ""}</span>
      <input type="number" data-p="${esc(p.key)}" value="${esc(v)}" ${p.min != null ? `min="${p.min}"` : ""} ${p.max != null ? `max="${p.max}"` : ""} step="1"></label>`;
  }).join("");
}

function readParam(el) {
  const p = pspec().find(x => x.key === el.dataset.p); if (!p) return;
  let v;
  if (p.type === "bool") v = el.checked;
  else if (p.type === "select") v = p.opts[+(el.dataset.i ?? el.value)];
  else {
    v = parseInt(el.value, 10);
    if (Number.isNaN(v)) v = p.default;
    if (p.min != null && v < p.min) v = p.min;
    if (p.max != null && v > p.max) v = p.max;
  }
  d().params[p.key] = v; saveDraft();
}

function updateHint() {
  const mo = mode(), out = [];
  if (mo.hint) out.push(esc(mo.hint));
  const pr = $("#prompt").value;
  $("#pcount").textContent = pr.length ? `${pr.length.toLocaleString()} chars` : "";
  // @ImageN references beyond the images actually attached are silently ignored by the vendor
  const ref = mo.slots.find(s => s.key.includes("reference"));
  if (ref) {
    const n = (d().slots[ref.key] || []).length;
    const used = [...pr.matchAll(/@image\s*(\d+)/gi)].map(m => +m[1]);
    const bad = used.filter(i => i < 1 || i > n);
    if (bad.length) out.push(`<span class="warn">@Image${bad[0]} is referenced but only ${n} image${n === 1 ? " is" : "s are"} attached.</span>`);
    else if (n && !used.length) out.push(`<span class="warn">Tip: mention the images as @Image1…@Image${n} in the prompt.</span>`);
  }
  if (pr.length > 20000) out.push(`<span class="warn">Prompt is over the 20,000-character limit.</span>`);
  $("#hint").innerHTML = out.join("<br>");
}

function renderHealth() {
  const key = `${S.kind}/${d().model}/${d().mode}`, h = S.health[key] || [];
  const p = model().price_note ? ` · ${esc(model().price_note)}` : "";
  if (!h.length) { $("#health").innerHTML = `<span>No runs of this mode yet${p}</span>`; return; }
  const dots = h.map(x => `<span class="dot ${x.ok ? "ok" : "bad"}" title="${esc(new Date(x.t * 1000).toLocaleString())}${x.error ? " — " + esc(x.error) : ""}"></span>`).join("");
  const lastBad = !h[0].ok && h[0].error;
  $("#health").innerHTML = `<span>Recent:</span>${dots}${lastBad ? ` <span class="err">last: ${esc(h[0].error.slice(0, 90))}</span>` : ""}`;
}

let estTimer;
function estimate() {
  clearTimeout(estTimer);
  estTimer = setTimeout(async () => {
    const seq = ++S.estimateSeq;
    try {
      const r = await api("POST", "/api/estimate", {kind: S.kind, model_key: d().model, mode_key: d().mode, params: d().params});
      if (seq !== S.estimateSeq) return;
      if (r.credits == null) { $("#estimate").innerHTML = `Cost: <span class="warn">not listed for this model — check kie.ai pricing</span>`; S.est = null; return; }
      S.est = r.credits;
      const over = S.balance != null && r.credits > S.balance;
      $("#estimate").innerHTML = `Est. <b>${r.credits.toLocaleString()} credits</b> ≈ <b>$${r.usd.toFixed(2)}</b>` +
        (r.source === "history" ? ` <span class="muted">(from your past runs)</span>` : "") +
        (over ? ` <span class="warn">— more than your balance (${S.balance.toLocaleString()})</span>` : "");
    } catch (e) { if (seq === S.estimateSeq) $("#estimate").textContent = ""; }
  }, 200);
}

async function generate() {
  if (S.busy) return;
  const dr = d(), mo = mode();
  dr.prompt = $("#prompt").value;
  if (!dr.prompt.trim()) { toast("Write a prompt first.", "bad"); $("#prompt").focus(); return; }
  for (const s of mo.slots) if (s.required && !(dr.slots[s.key] || []).length) { toast(`${s.label} is required for this mode.`, "bad"); return; }
  if (S.est && S.est >= 200 && !confirm(`This will cost about ${S.est} credits ($${(S.est * S.creditUsd).toFixed(2)}). Continue?`)) return;
  S.busy = true; $("#go").disabled = true;
  try {
    const slots = Object.fromEntries(mo.slots.map(s => [s.key, (dr.slots[s.key] || []).map(a => a.id)]));
    const r = await api("POST", "/api/jobs", {client_id: uid(), kind: S.kind, model_key: dr.model, mode_key: dr.mode,
                                              prompt: dr.prompt, params: dr.params, slots});
    toast("Queued — you can keep working; it runs in the background.", "ok", 3000);
    await refreshJob(r.id); pollSoon();
  } catch (e) {
    if (/balance is/.test(e.message) && confirm(e.message + "\n\nSubmit anyway?")) {
      S.busy = false; $("#go").disabled = false; return generateForce();
    }
    fail(e);
  } finally { S.busy = false; $("#go").disabled = false; }
}
async function generateForce() {
  const dr = d(), mo = mode();
  try {
    const slots = Object.fromEntries(mo.slots.map(s => [s.key, (dr.slots[s.key] || []).map(a => a.id)]));
    const r = await api("POST", "/api/jobs", {client_id: uid(), kind: S.kind, model_key: dr.model, mode_key: dr.mode,
                                              prompt: dr.prompt, params: dr.params, slots, force: true});
    await refreshJob(r.id); pollSoon();
  } catch (e) { fail(e); }
}

// Switching model keeps whatever still fits: prompt, params with valid values, images by slot key (else by position).
function switchModel(key, modeKey) {
  const old = d(), m = S.reg[S.kind][key];
  const nm = modeKey && m.modes[modeKey] ? modeKey
    : m.modes[old.mode] ? old.mode
    : Object.keys(m.modes).find(k => !!m.modes[k].slots.length === !!mode().slots.length) || Object.keys(m.modes)[0];
  const oldSlots = Object.values(old.slots).flat();
  old.model = key; old.mode = nm;
  const keep = {};
  for (const p of pspec()) {
    const v = old.params[p.key];
    if (v === undefined || p.key === "seed" || AUDIO_KEYS.includes(p.key)) continue;   // "random" is 0 on some models and -1 on others
    if (p.type === "select" && !p.opts.some(o => String(o).toLowerCase() === String(v).toLowerCase())) continue;
    if (p.type === "select") keep[p.key] = p.opts.find(o => String(o).toLowerCase() === String(v).toLowerCase());
    else keep[p.key] = v;
  }
  old.params = keep;
  remapSlots(old.slots, oldSlots);
  saveDraft(); renderComposer();
}
function remapSlots(prev, flat) {
  const out = {};
  for (const s of mode().slots) if (prev[s.key]?.length) out[s.key] = prev[s.key].slice(0, s.max || 1);
  if (!Object.keys(out).length && flat.length) {
    let rest = [...flat];
    for (const s of mode().slots) { const n = s.max || 1; if (rest.length) { out[s.key] = rest.slice(0, n); rest = rest.slice(n); } }
  }
  d().slots = out;
}

// ------------------------------------------------------------------ jobs: data
async function refreshJob(id) {
  const j = await api("GET", `/api/jobs/${id}`);
  const prev = S.jobs.get(id);
  S.jobs.set(id, j);
  if (!S.order.includes(id) && j.kind === S.kind) S.order.unshift(id);
  if (prev && ACTIVE.includes(prev.status) && !ACTIVE.includes(j.status)) announce(j);
  renderActive(); renderHistory();
  return j;
}

function filters() {
  const p = new URLSearchParams({kind: S.kind});
  if ($("#search").value.trim()) p.set("q", $("#search").value.trim());
  if ($("#fModel").value) p.set("model", $("#fModel").value);
  if ($("#fStatus").value) p.set("status", $("#fStatus").value);
  if ($("#fFav").checked) p.set("fav", "1");
  return p;
}

async function loadHistory(more = false) {
  const p = filters(); p.set("limit", 40);
  if (more) { const last = S.jobs.get(S.order.at(-1)); if (last) p.set("before", last.created); }
  try {
    const rows = await api("GET", "/api/jobs?" + p);
    if (!more) { S.order = []; S.sel.clear(); }
    for (const j of rows) { S.jobs.set(j.id, j); if (!S.order.includes(j.id)) S.order.push(j.id); }
    S.exhausted = rows.length < 40;
    if (!more) S.lastSync = Math.max(S.lastSync, ...rows.map(r => r.updated || 0), 0);
    renderHistory();
  } catch (e) { fail(e); }
}

// one cheap request: everything updated since last sync, across kinds (so the active panel is complete)
let pollTimer, polling = false;
async function poll() {
  if (polling) return; polling = true;
  try {
    const rows = await api("GET", `/api/jobs?since=${S.lastSync - 1}&limit=200`);
    for (const j of rows) {
      const prev = S.jobs.get(j.id);
      S.jobs.set(j.id, j);
      S.lastSync = Math.max(S.lastSync, j.updated || 0);
      if (j.kind === S.kind && !S.order.includes(j.id) && matchesFilters(j)) S.order.unshift(j.id);
      if (prev && ACTIVE.includes(prev.status) && !ACTIVE.includes(j.status)) announce(j);
    }
    if (rows.length) { S.order.sort((a, b) => S.jobs.get(b).created - S.jobs.get(a).created); renderActive(); renderHistory(); }
    else renderActive();
    $("#balance").classList.remove("off");
  } catch (e) {
    $("#balance").textContent = "server offline"; $("#balance").classList.add("low");
  } finally { polling = false; }
  pollSoon();
}
function pollSoon() {
  clearTimeout(pollTimer);
  const busy = [...S.jobs.values()].some(j => ACTIVE.includes(j.status));
  pollTimer = setTimeout(poll, document.hidden ? 15000 : busy ? 3000 : 10000);
}
function matchesFilters(j) {
  const qy = $("#search").value.trim().toLowerCase();
  if (qy && !(`${j.prompt} ${j.notes}`.toLowerCase().includes(qy))) return false;
  if ($("#fModel").value && j.model_key !== $("#fModel").value) return false;
  if ($("#fStatus").value && j.status !== $("#fStatus").value) return false;
  if ($("#fFav").checked && !j.favorite) return false;
  return true;
}

function announce(j) {
  if (j.status === "success") {
    toast(`Done: ${j.model_label} — ${j.prompt.slice(0, 60)}`, "ok");
    loadCredits(true); loadHealth(); loadStats();
  } else if (j.status === "fail") {
    toast(`Failed: ${j.error || "unknown error"}`, "bad"); loadHealth();
  }
  if (document.hidden && "Notification" in window && Notification.permission === "granted") {
    try {
      const n = new Notification(j.status === "success" ? "Kiester: generation finished" : "Kiester: generation failed",
        {body: (j.status === "fail" ? (j.error || "") + "\n" : "") + j.prompt.slice(0, 120), tag: j.id,
         icon: j.poster ? mediaUrl(j.poster) : undefined});
      n.onclick = () => { window.focus(); openDetail(j.id); n.close(); };
    } catch {}
  }
}

// ------------------------------------------------------------------ jobs: render
const fmtAgo = t => {
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 60) return `${Math.floor(s)}s`; if (s < 3600) return `${Math.floor(s / 60)}m ${Math.floor(s % 60)}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor(s % 3600 / 60)}m`;
};
const fmtDate = t => {
  const dt = new Date(t * 1000), now = new Date();
  return dt.toDateString() === now.toDateString() ? dt.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})
    : dt.toLocaleDateString([], {month: "short", day: "numeric"}) + " " + dt.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
};

function renderActive() {
  const act = [...S.jobs.values()].filter(j => ACTIVE.includes(j.status)).sort((a, b) => b.created - a.created);
  $("#activeWrap").hidden = !act.length;
  document.title = act.length ? `(${act.length}) Kiester` : "Kiester";
  $("#active").innerHTML = act.map(j => `
    <div class="act" data-id="${esc(j.id)}">
      <div class="spin"></div>
      <div class="t"><div class="p">${esc(j.prompt)}</div>
        <div class="s">${esc(j.model_label)} · ${esc(j.mode_label)} · ${STATUS_LABEL[j.status]} · ${fmtAgo(j.submitted || j.created)}${j.est_credits ? ` · ~${j.est_credits} cr` : ""}</div></div>
      <button class="small" data-act="detail">Log</button>
      ${j.status === "running" ? `<button class="small" data-act="dismiss" title="Stop tracking (kie has no cancel)">Dismiss</button>` : ""}
    </div>`).join("");
}
setInterval(() => { if (!$("#activeWrap").hidden) renderActive(); }, 1000);

function paramTags(j) {
  const p = j.params || {}, out = [];
  for (const k of ["resolution", "quality", "duration", "aspect_ratio"]) if (p[k] != null && p[k] !== "") out.push(k === "duration" ? `${p[k]}s` : p[k]);
  for (const k of AUDIO_KEYS) if (p[k]) out.push("♪");
  return out;
}

function jobCard(j) {
  const isVid = j.kind === "video", out = j.outputs || [];
  let media;
  if (j.status === "success" && out.length) {
    media = isVid
      ? (j.poster ? `<img src="${mediaUrl(j.poster)}" alt="" loading="lazy">` : `<video src="${mediaUrl(out[0])}#t=0.3" preload="metadata" muted></video>`)
      : `<img src="${mediaUrl(out[0])}" alt="" loading="lazy">`;
  } else if (ACTIVE.includes(j.status)) media = `<div class="ph"><div class="spin" style="margin:0 auto 6px"></div>${STATUS_LABEL[j.status]}…</div>`;
  else if (j.status === "success") media = `<div class="ph">Finished on kie but not saved locally.<br>Use “Retry download”.</div>`;
  else media = `<div class="ph">✕ ${esc((j.error || "failed").slice(0, 140))}</div>`;
  const canSel = isVid && j.status === "success" && out.length;
  return `<div class="job ${S.sel.has(j.id) ? "sel" : ""}" data-id="${esc(j.id)}">
    <div class="media" data-act="detail">${media}
      ${canSel ? `<input type="checkbox" class="selbox" data-act="sel" ${S.sel.has(j.id) ? "checked" : ""} title="Select for stitching">` : ""}
      <span class="chip ${esc(j.status)}">${STATUS_LABEL[j.status] || esc(j.status)}${out.length > 1 ? ` · ${out.length}` : ""}</span>
    </div>
    <div class="jb">
      <div class="jm"><span class="tag">${esc(j.model_label)}</span><span>${esc(j.mode_label)}</span>
        ${paramTags(j).map(t => `<span>${esc(t)}</span>`).join("")}
        <span title="${esc(new Date(j.created * 1000).toLocaleString())}">${fmtDate(j.created)}</span>
        ${j.credits ? `<span title="$${(j.credits * S.creditUsd).toFixed(2)}">${j.credits} cr</span>` : ""}</div>
      <div class="jp" data-act="expand">${esc(j.prompt)}</div>
      ${j.status !== "fail" && j.error ? `<div class="je">${esc(j.error)}</div>` : ""}
      ${j.notes ? `<div class="muted">📝 ${esc(j.notes)}</div>` : ""}
      <div class="ja">
        <button class="fav ${j.favorite ? "on" : ""}" data-act="fav" title="Favorite">★</button>
        ${j.model_key !== "_stitch" ? `<button data-act="reuse" title="Load prompt, settings and images into the composer">Reuse</button>
        <button data-act="rerun" title="Run again with identical settings (new seed if random)">Rerun</button>` : ""}
        ${out.length ? `<a class="btn small" href="${mediaUrl(out[0])}" download>⬇</a>` : ""}
        <button data-act="menu" title="More">⋯</button>
      </div>
    </div></div>`;
}

function renderHistory() {
  const ids = S.order.filter(id => S.jobs.get(id)?.kind === S.kind);
  $("#history").innerHTML = ids.length ? ids.map(id => jobCard(S.jobs.get(id))).join("")
    : `<div class="muted" style="padding:30px;text-align:center">${$("#search").value || $("#fModel").value || $("#fStatus").value || $("#fFav").checked ? "Nothing matches these filters." : "No generations yet. Write a prompt and press Generate."}</div>`;
  $("#more").hidden = S.exhausted || !ids.length;
  $("#selbar").hidden = S.sel.size === 0;
  $("#selcount").textContent = `${S.sel.size} selected (in click order)`;
  $("#stitchBtn").disabled = S.sel.size < 2;
}

// ------------------------------------------------------------------ jobs: actions
async function patchJob(id, body) {
  const j = await api("PATCH", `/api/jobs/${id}`, body);
  S.jobs.set(id, {...S.jobs.get(id), ...j}); renderHistory(); return j;
}

async function reuse(id, opts = {}) {
  let j = S.jobs.get(id);
  if (!j.assets) j = await api("GET", `/api/jobs/${id}`);
  const m = S.reg[j.kind]?.[j.model_key];
  if (!m || !m.modes[j.mode_key]) { toast("That model/mode is no longer in the registry; prompt copied instead."); $("#prompt").value = j.prompt; return null; }
  if (j.kind !== S.kind) switchKind(j.kind);
  const dr = d();
  dr.model = j.model_key; dr.mode = j.mode_key; dr.params = {...j.params}; dr.prompt = j.prompt;
  dr.slots = {};
  for (const [k, list] of Object.entries(j.assets || {})) dr.slots[k] = list;
  const missing = Object.entries(j.slots || {}).some(([k, v]) => (j.assets?.[k] || []).length < v.length);
  if (missing) toast("Some input images of that job were deleted; re-add them.", "bad");
  saveDraft(); renderComposer();
  if (!opts.quiet) { toast("Loaded into the composer."); window.scrollTo({top: 0, behavior: "smooth"}); $("#prompt").focus(); }
  return j;
}

async function rerun(id) {
  let j = await api("GET", `/api/jobs/${id}`);
  const slots = Object.fromEntries(Object.entries(j.assets || {}).map(([k, v]) => [k, v.map(a => a.id)]));
  if (Object.entries(j.slots || {}).some(([k, v]) => (slots[k] || []).length < v.length)) {
    toast("Input images are missing; use Reuse and re-add them.", "bad"); return;
  }
  try {
    const r = await api("POST", "/api/jobs", {client_id: uid(), kind: j.kind, model_key: j.model_key, mode_key: j.mode_key,
                                              prompt: j.prompt, params: j.params, slots});
    toast("Rerun queued.", "ok", 2500); await refreshJob(r.id); pollSoon();
  } catch (e) { fail(e); }
}

// put the first/last frame of a video into the composer's start-frame slot (for chaining shots)
async function continueFrom(id, which) {
  try {
    const a = await api("POST", `/api/jobs/${id}/frame`, {which});
    const j = S.jobs.get(id);
    if (S.kind !== "video") switchKind("video");
    // prefer staying on the same model, in a mode whose first slot is a single image
    const m = S.reg.video[j.model_key];
    const mk = m && Object.keys(m.modes).find(k => m.modes[k].slots.length && !(m.modes[k].slots[0].max > 1));
    if (mk && (d().model !== j.model_key || !mode().slots.length)) { d().model = j.model_key; d().mode = mk; d().slots = {}; renderComposer(); }
    else if (!mode().slots.length) {
      const alt = Object.entries(S.reg.video).find(([, mm]) => Object.values(mm.modes).some(mo => mo.slots.length));
      if (alt) switchModel(alt[0], Object.keys(alt[1].modes).find(k => alt[1].modes[k].slots.length));
    }
    const slot = which === "last" ? mode().slots[0].key : (mode().slots[1] || mode().slots[0]).key;
    addToSlot(slot, [a]);
    toast(`${which === "last" ? "Last" : "First"} frame added to “${mode().slots.find(s => s.key === slot).label}”.`, "ok");
    window.scrollTo({top: 0, behavior: "smooth"});
  } catch (e) { fail(e); }
}

async function outputAsInput(id, index = 0) {
  try {
    const a = await api("POST", `/api/jobs/${id}/as-asset`, {index});
    const key = defaultSlot();
    if (!key) {
      // current mode has no image slots: jump to an edit / image-to-video mode of the same kind
      const mk = Object.keys(model().modes).find(k => model().modes[k].slots.length);
      if (!mk) { toast("This model has no image-input mode.", "bad"); return; }
      d().mode = mk; renderComposer();
    }
    addToSlot(defaultSlot() || mode().slots[0].key, [a]);
    toast("Image added as input.", "ok"); window.scrollTo({top: 0, behavior: "smooth"});
  } catch (e) { fail(e); }
}

async function del(id, files) {
  const j = S.jobs.get(id);
  if (!confirm(files ? "Delete this entry AND its files from disk? This cannot be undone." : "Remove from history (files stay on disk)?")) return;
  try {
    await api("DELETE", `/api/jobs/${id}${files ? "?files=1" : ""}`);
    S.jobs.delete(id); S.order = S.order.filter(x => x !== id); S.sel.delete(id);
    renderHistory(); loadStats();
    if ($("#detDlg").open && $("#detDlg").dataset.id === id) $("#detDlg").close();
    toast(files ? "Deleted with files." : "Removed from history.");
  } catch (e) { fail(e); }
  return j;
}

function copy(text, what) {
  (navigator.clipboard?.writeText(text) || Promise.reject()).then(() => toast(`${what} copied.`, "", 2000), () => {
    const t = document.createElement("textarea"); t.value = text; document.body.append(t); t.select();
    try { document.execCommand("copy"); toast(`${what} copied.`, "", 2000); } catch { toast("Copy failed.", "bad"); } t.remove();
  });
}

function openMenu(btn, id) {
  closeMenu();
  const j = S.jobs.get(id), out = j.outputs || [], vid = j.kind === "video";
  const items = [];
  if (vid && out.length) items.push(["Continue from last frame", () => continueFrom(id, "last")],
                                     ["Use first frame as end frame", () => continueFrom(id, "first")]);
  if (!vid && out.length) out.forEach((_, i) => items.push([out.length > 1 ? `Use image ${i + 1} as input` : "Use as input image", () => outputAsInput(id, i)]));
  if (out.length) items.push(["Open file in new tab", () => window.open(mediaUrl(out[0]), "_blank", "noopener")]);
  if (j.result_urls?.length && (j.status === "success" || j.status === "fail") && out.length < j.result_urls.length)
    items.push(["Retry download", async () => { try { await api("POST", `/api/jobs/${id}/redownload`); refreshJob(id); pollSoon(); } catch (e) { fail(e); } }]);
  items.push(["Copy prompt", () => copy(j.prompt, "Prompt")]);
  if (j.task_id) items.push(["Copy kie task id", () => copy(j.task_id, "Task id")]);
  items.push(["Details & log", () => openDetail(id)], "-");
  if (!ACTIVE.includes(j.status)) items.push(["Remove from history", () => del(id, false)], ["Delete with files", () => del(id, true), "danger"]);
  else if (j.status === "running") items.push(["Dismiss (stop tracking)", () => dismiss(id), "danger"]);
  const m = document.createElement("div"); m.className = "menu";
  m.innerHTML = items.map((it, i) => it === "-" ? "<hr>" : `<button data-i="${i}" class="${it[2] || ""}">${esc(it[0])}</button>`).join("");
  m.onclick = e => { const b = e.target.closest("button"); if (!b) return; closeMenu(); items[+b.dataset.i][1](); };
  document.body.append(m);
  const r = btn.getBoundingClientRect(), mw = m.offsetWidth, mh = m.offsetHeight;
  m.style.left = Math.max(8, Math.min(window.scrollX + r.right - mw, window.scrollX + innerWidth - mw - 8)) + "px";
  m.style.top = (r.bottom + mh + 8 > innerHeight ? window.scrollY + r.top - mh - 4 : window.scrollY + r.bottom + 4) + "px";
}
function closeMenu() { $$(".menu").forEach(m => m.remove()); }

async function dismiss(id) {
  if (!confirm("kie.ai has no cancel: the task may still finish and be billed. Stop tracking it here?")) return;
  try { S.jobs.set(id, await api("POST", `/api/jobs/${id}/dismiss`)); renderActive(); renderHistory(); } catch (e) { fail(e); }
}

async function stitch() {
  const ids = [...S.sel];
  if (ids.length < 2) return;
  $("#stitchBtn").disabled = true; $("#stitchBtn").textContent = "Stitching…";
  try {
    const j = await api("POST", "/api/stitch", {ids});
    S.jobs.set(j.id, j); S.order.unshift(j.id); S.sel.clear(); renderHistory();
    toast("Stitched video saved.", "ok"); openDetail(j.id);
  } catch (e) { fail(e); }
  finally { $("#stitchBtn").textContent = "Stitch selected into one video"; renderHistory(); }
}

// ------------------------------------------------------------------ detail dialog
async function openDetail(id) {
  let j;
  try { j = await api("GET", `/api/jobs/${id}`); } catch (e) { return fail(e); }
  S.jobs.set(id, {...S.jobs.get(id), ...j});
  const dlg = $("#detDlg"); dlg.dataset.id = id;
  $("#detTitle").textContent = `${j.model_label} · ${j.mode_label}`;
  const out = j.outputs || [];
  const media = out.length
    ? (j.kind === "video"
      ? `<video src="${mediaUrl(out[0])}" controls autoplay loop playsinline></video>`
      : out.map(o => `<a href="${mediaUrl(o)}" target="_blank" rel="noopener"><img class="big" src="${mediaUrl(o)}" alt=""></a>`).join(""))
    : `<div class="muted" style="padding:40px;text-align:center;background:var(--card2);border-radius:10px">${ACTIVE.includes(j.status) ? STATUS_LABEL[j.status] + "…" : esc(j.error || "No output")}</div>`;
  const inputs = Object.entries(j.assets || {}).map(([k, list]) => list.length
    ? `<div class="muted">${esc(mode_slot_label(j, k))}</div><div class="thumbs">${list.map(a => `<div class="thumb"><img src="${esc(a.url)}" alt=""></div>`).join("")}</div>` : "").join("");
  const kv = [
    ["Status", `${STATUS_LABEL[j.status] || j.status}${j.error ? ` — <span class="${j.status === "fail" ? "warn" : ""}">${esc(j.error)}</span>` : ""}`],
    ["Created", esc(new Date(j.created * 1000).toLocaleString())],
    ["Gen time", j.cost_time ? `${j.cost_time}s` : "—"],
    ["Credits", j.credits != null ? `${j.credits} ($${(j.credits * S.creditUsd).toFixed(2)})` : (j.est_credits ? `~${j.est_credits} (estimate)` : "—")],
    ["Model id", esc(j.model_id || "")], ["Task id", esc(j.task_id || "—")],
    ...Object.entries(j.params || {}).map(([k, v]) => [esc(k), esc(typeof v === "boolean" ? (v ? "on" : "off") : v)]),
  ];
  $("#detBody").innerHTML = `<div class="det">
    <div>${media}${out.length ? `<div class="ja" style="margin-top:8px">${out.map((o, i) => `<a class="btn small" href="${mediaUrl(o)}" download>⬇ ${esc(o.split("/").pop())}</a>`).join("")}</div>` : ""}</div>
    <div>
      <div class="kv">${kv.map(([k, v]) => `<b>${k}</b><span>${v}</span>`).join("")}</div>
      <h3 style="margin-top:14px">Prompt <button class="small" data-dact="copy">Copy</button></h3>
      <pre>${esc(j.prompt)}</pre>
      ${inputs ? `<h3>Inputs</h3>${inputs}` : ""}
      <h3 style="margin-top:14px">Notes</h3>
      <textarea id="detNotes" placeholder="Private notes (searchable)">${esc(j.notes || "")}</textarea>
      <div class="ja" style="margin-top:8px">
        ${j.model_key !== "_stitch" ? `<button data-dact="reuse">Reuse</button><button data-dact="rerun">Rerun</button>` : ""}
        <button data-dact="fav" class="fav ${j.favorite ? "on" : ""}">★ Favorite</button>
      </div>
      <h3 style="margin-top:14px">Log</h3>
      <pre>${(j.events || []).map(e => `${new Date(e.ts * 1000).toLocaleTimeString()}  ${esc(e.msg)}`).join("\n") || "—"}</pre>
    </div></div>`;
  if (!dlg.open) dlg.showModal();
  $("#detNotes").addEventListener("change", e => patchJob(id, {notes: e.target.value}).then(() => toast("Notes saved.", "", 1500)).catch(fail));
}
function mode_slot_label(j, key) {
  const s = S.reg[j.kind]?.[j.model_key]?.modes[j.mode_key]?.slots.find(x => x.key === key);
  return s ? s.label : key;
}

$("#detBody").addEventListener("click", e => {
  const b = e.target.closest("[data-dact]"); if (!b) return;
  const id = $("#detDlg").dataset.id, j = S.jobs.get(id);
  if (b.dataset.dact === "copy") copy(j.prompt, "Prompt");
  if (b.dataset.dact === "reuse") { $("#detDlg").close(); reuse(id); }
  if (b.dataset.dact === "rerun") { $("#detDlg").close(); rerun(id); }
  if (b.dataset.dact === "fav") patchJob(id, {favorite: !j.favorite}).then(x => b.classList.toggle("on", !!x.favorite)).catch(fail);
});
$("#detDlg").addEventListener("close", () => $$("#detBody video").forEach(v => v.pause()));

// ------------------------------------------------------------------ library dialog
async function openLibrary(slotKey) {
  S.libTarget = slotKey;
  const s = mode().slots.find(x => x.key === slotKey);
  $("#libTarget").textContent = s ? `Click images to add them to “${s.label}”${(s.max || 1) > 1 ? ` (up to ${s.max})` : ""}. Drop files anywhere in this window to upload.` : "";
  $("#libDlg").showModal();
  await renderLibrary();
}
async function renderLibrary() {
  try {
    const list = await api("GET", "/api/assets?limit=300");
    const inSlot = new Set((d().slots[S.libTarget] || []).map(a => a.id));
    $("#libGrid").innerHTML = list.length ? list.map(a => `
      <div class="libitem" data-aid="${esc(a.id)}" style="${inSlot.has(a.id) ? "border-color:var(--acc)" : ""}" title="${esc(a.label || "")}">
        <img src="${esc(a.url)}" alt="" loading="lazy">
        <div class="meta">${a.w}×${a.h} · ${esc(a.source)}</div>
        <button class="x" data-hide title="Hide from library">✕</button>
      </div>`).join("") : `<div class="muted">No images yet — upload some.</div>`;
    S.lib = Object.fromEntries(list.map(a => [a.id, a]));
  } catch (e) { fail(e); }
}
$("#libGrid").addEventListener("click", async e => {
  const it = e.target.closest(".libitem"); if (!it) return;
  const a = S.lib[it.dataset.aid];
  if (e.target.closest("[data-hide]")) {
    await api("DELETE", `/api/assets/${a.id}`).catch(fail); renderLibrary(); return;
  }
  addToSlot(S.libTarget, [a]);
  const s = mode().slots.find(x => x.key === S.libTarget);
  if ((s?.max || 1) === 1 || (d().slots[S.libTarget] || []).length >= s.max) $("#libDlg").close();
  else renderLibrary();
});
$("#libUpload").addEventListener("change", async e => {
  const got = await uploadFiles(e.target.files); e.target.value = "";
  if (got.length) { addToSlot(S.libTarget, got); renderLibrary(); }
});

// ------------------------------------------------------------------ header: balance, stats, health
async function loadCredits(fresh) {
  try {
    const r = await api("GET", "/api/credits" + (fresh ? "?fresh=1" : ""));
    const b = $("#balance");
    if (r.credits == null) {
      b.textContent = "balance ?"; b.title = r.error || ""; b.classList.add("low");
      if (/^401\b/.test(r.error || "") && $("#keyScreen").hidden && S.keySource !== "env")
        showKeyScreen({reason: "kie.ai no longer accepts the saved API key (it may have been reset). Paste the current one."});
      return;
    }
    S.balance = r.credits;
    b.innerHTML = `<b>${r.credits.toLocaleString()}</b> cr · $${r.usd.toFixed(2)}`;
    b.title = `Spent last 24 h: ${(+r.spent_24h).toLocaleString()} credits over ${r.jobs_24h} jobs. Click to refresh.` + (r.error ? `\nLast refresh failed: ${r.error}` : "");
    b.classList.toggle("low", r.credits < 200);
    estimate();
  } catch (e) { $("#balance").textContent = "offline"; }
}
async function loadStats() {
  try {
    const s = await api("GET", "/api/stats");
    $("#stats").textContent = `${s.jobs} jobs · ${s.ok} ok · ${s.failed} failed · ${(+s.credits_total).toLocaleString()} credits ($${s.usd_total}) total · ${s.disk_mb} MB on disk · ${s.free_gb} GB free`
      + (s.free_gb < 5 ? " ⚠ low disk" : "");
  } catch {}
}
async function loadHealth() {
  try { S.health = await api("GET", "/api/health"); renderHealth(); } catch {}
}

function fillModelFilter() {
  $("#fModel").innerHTML = `<option value="">All models</option>` +
    Object.entries(S.reg[S.kind]).map(([k, m]) => `<option value="${esc(k)}">${esc(m.label)}</option>`).join("") +
    (S.kind === "video" ? `<option value="_stitch">Stitched</option>` : "");
}

function switchKind(kind) {
  if (kind === S.kind) return;
  d().prompt = $("#prompt").value;
  S.kind = kind; saveDraft();
  $$("#tabs button").forEach(b => b.classList.toggle("active", b.dataset.kind === kind));
  $("#search").value = ""; $("#fStatus").value = ""; $("#fFav").checked = false;
  fillModelFilter(); renderComposer(); S.order = []; loadHistory();
}

// ------------------------------------------------------------------ events
$("#tabs").addEventListener("click", e => { const b = e.target.closest("button"); if (b) switchKind(b.dataset.kind); });
$("#model").addEventListener("change", e => switchModel(e.target.value));
$("#modes").addEventListener("click", e => {
  const b = e.target.closest("button[data-mode]"); if (!b || b.dataset.mode === d().mode) return;
  const flat = Object.values(d().slots).flat(), prev = d().slots;
  d().mode = b.dataset.mode; remapSlots(prev, flat); saveDraft(); renderComposer();
});
$("#prompt").addEventListener("input", e => { d().prompt = e.target.value; updateHint(); saveDraft(); });
$("#params").addEventListener("click", e => {
  const b = e.target.closest(".seg button[data-p]"); if (!b) return;
  $$(`.seg button[data-p="${b.dataset.p}"]`).forEach(x => x.classList.toggle("active", x === b));
  readParam(b); estimate();
});
$("#params").addEventListener("change", e => {
  if (!e.target.dataset.p) return;
  readParam(e.target);
  const p = pspec().find(x => x.key === e.target.dataset.p);
  if (p?.type === "int") e.target.value = d().params[p.key];   // show the clamped value
  estimate();
});
$("#params").addEventListener("input", e => { if (e.target.type === "number") { readParam(e.target); estimate(); } });
$("#reset").addEventListener("click", () => { d().params = {}; saveDraft(); renderParams(); estimate(); toast("Parameters reset to defaults.", "", 2000); });
$("#go").addEventListener("click", generate);
document.addEventListener("keydown", e => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); generate(); }
  if (e.key === "Escape") closeMenu();
});

// slots: add / remove / reorder / drag-drop
$("#slots").addEventListener("click", e => {
  const slot = e.target.closest(".slot"); if (!slot) return;
  const key = slot.dataset.slot, items = [...(d().slots[key] || [])];
  if (e.target.closest("[data-add]")) return openLibrary(key);
  const rm = e.target.closest("[data-rm]");
  if (rm) { items.splice(+rm.dataset.rm, 1); return setSlot(key, items); }
  const mv = e.target.closest("[data-mv]");
  if (mv) { const i = +mv.dataset.mv, j = i + +mv.dataset.dir; [items[i], items[j]] = [items[j], items[i]]; setSlot(key, items); }
});
let dragDepth = 0;
$("#slots").addEventListener("dragover", e => { const s = e.target.closest(".slot"); if (s) { e.preventDefault(); $$(".slot").forEach(x => x.classList.toggle("drag", x === s)); } });
$("#slots").addEventListener("dragleave", e => { if (!e.relatedTarget || !$("#slots").contains(e.relatedTarget)) $$(".slot").forEach(x => x.classList.remove("drag")); });
$("#slots").addEventListener("drop", async e => {
  const s = e.target.closest(".slot"); $$(".slot").forEach(x => x.classList.remove("drag"));
  if (!s) return; e.preventDefault(); e.stopPropagation();
  const got = await uploadFiles(e.dataTransfer.files);
  if (got.length) addToSlot(s.dataset.slot, got);
});
// dropping outside a slot must not navigate away from the page (and lose the draft)
window.addEventListener("dragover", e => e.preventDefault());
window.addEventListener("drop", async e => {
  e.preventDefault();
  if (!e.dataTransfer?.files?.length) return;
  const got = await uploadFiles(e.dataTransfer.files);
  if (!got.length) return;
  if ($("#libDlg").open) { addToSlot(S.libTarget, got); renderLibrary(); return; }
  const key = defaultSlot();
  if (key) addToSlot(key, got); else toast("Uploaded to the library (this mode takes no images).");
});
// paste an image from the clipboard (screenshots, copied images)
document.addEventListener("paste", async e => {
  const files = [...(e.clipboardData?.items || [])].filter(i => i.kind === "file" && i.type.startsWith("image/")).map(i => i.getAsFile());
  if (!files.length) return;
  e.preventDefault();
  const key = $("#libDlg").open ? S.libTarget : defaultSlot();
  const got = await uploadFiles(files);
  if (!got.length) return;
  if (key) { addToSlot(key, got); if ($("#libDlg").open) renderLibrary(); }
  else toast("Pasted image saved to the library (this mode takes no images).");
});

// history interactions
$("#history").addEventListener("click", e => {
  const card = e.target.closest(".job"); if (!card) return;
  const id = card.dataset.id, j = S.jobs.get(id), act = e.target.closest("[data-act]")?.dataset.act;
  if (act === "sel") {
    e.stopPropagation();
    if (e.target.checked) S.sel.add(id); else S.sel.delete(id);
    renderHistory(); return;
  }
  if (act === "detail") return openDetail(id);
  if (act === "expand") return e.target.classList.toggle("open");
  if (act === "fav") return patchJob(id, {favorite: !j.favorite}).catch(fail);
  if (act === "reuse") return reuse(id);
  if (act === "rerun") return rerun(id);
  if (act === "menu") { e.stopPropagation(); return openMenu(e.target.closest("button"), id); }
});
// hover a video card to preview it without opening it
$("#history").addEventListener("mouseover", e => {
  const m = e.target.closest(".media"); if (!m || m.dataset.hov) return;
  const j = S.jobs.get(m.closest(".job").dataset.id);
  if (j?.kind !== "video" || !j.outputs?.length || j.status !== "success") return;
  m.dataset.hov = "1";
  const v = document.createElement("video");
  Object.assign(v, {src: mediaUrl(j.outputs[0]), muted: true, loop: true, playsInline: true, autoplay: true});
  v.style.cssText = "position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:#000";
  m.append(v);
  m.addEventListener("mouseleave", () => { v.pause(); v.removeAttribute("src"); v.load(); v.remove(); delete m.dataset.hov; }, {once: true});
});
$("#active").addEventListener("click", e => {
  const b = e.target.closest("[data-act]"); if (!b) return;
  const id = b.closest(".act").dataset.id;
  if (b.dataset.act === "detail") openDetail(id);
  if (b.dataset.act === "dismiss") dismiss(id);
});
document.addEventListener("click", e => { if (!e.target.closest(".menu")) closeMenu(); });
window.addEventListener("resize", closeMenu);

let searchTimer;
$("#search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => loadHistory(), 250); });
["#fModel", "#fStatus", "#fFav"].forEach(s => $(s).addEventListener("change", () => loadHistory()));
$("#more").addEventListener("click", () => loadHistory(true));
$("#stitchBtn").addEventListener("click", stitch);
$("#selClear").addEventListener("click", () => { S.sel.clear(); renderHistory(); });
$("#balance").addEventListener("click", () => loadCredits(true));

$("#notifBtn").addEventListener("click", async () => {
  if (!("Notification" in window)) return toast("This browser has no desktop notifications.", "bad");
  if (Notification.permission === "denied") return toast("Notifications are blocked for this site in browser settings.", "bad");
  const p = await Notification.requestPermission();
  updateNotifBtn();
  toast(p === "granted" ? "You'll get a desktop notification when a job finishes while this tab is in the background." : "Notifications not enabled.");
});
function updateNotifBtn() {
  const on = "Notification" in window && Notification.permission === "granted";
  $("#notifBtn").style.opacity = on ? 1 : .5;
  $("#notifBtn").title = on ? "Desktop notifications are on" : "Enable desktop notifications when jobs finish";
}

// dialogs: ✕ buttons and backdrop clicks close them
$$("dialog").forEach(dl => {
  dl.addEventListener("click", e => { if (e.target === dl || e.target.closest("[data-close]")) dl.close(); });
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) { poll(); loadCredits(); } });
window.addEventListener("beforeunload", () => { d().prompt = $("#prompt").value; for (const k of ["video", "image"]) store.set("draft." + k, S.draft[k]); });

// ------------------------------------------------------------------ API key screen
// Shown on first run (no key saved), when kie rejects the saved key, or from the 🔑 button.
function showKeyScreen({reason = "", cancellable = false} = {}) {
  $("#keyScreen").hidden = false;
  $("#keyTitle").textContent = reason ? "Your kie.ai API key needs attention" : cancellable ? "Change your kie.ai API key" : "Connect Kiester to your kie.ai account";
  $("#keyErr").textContent = reason;
  $("#keyCancel").hidden = !cancellable;
  $("#keyInput").value = ""; $("#keyInput").focus();
  return new Promise(res => { S.keyDone = res; });
}
$("#keyShow").addEventListener("click", () => { const i = $("#keyInput"); i.type = i.type === "password" ? "text" : "password"; i.focus(); });
$("#keyCancel").addEventListener("click", () => { $("#keyScreen").hidden = true; S.keyDone?.(false); });
$("#keyForm").addEventListener("submit", async e => {
  e.preventDefault();
  const key = $("#keyInput").value.trim();
  if (!key) { $("#keyErr").textContent = "Paste your API key first."; return; }
  $("#keySave").disabled = true; $("#keySave").textContent = "Checking with kie.ai…"; $("#keyErr").textContent = "";
  try {
    const r = await api("POST", "/api/key", {key});
    $("#keyScreen").hidden = true; $("#keyInput").value = "";
    toast(`Connected. Balance: ${r.credits.toLocaleString()} credits.`, "ok");
    S.keyDone?.(true); loadCredits(true);
  } catch (err) { $("#keyErr").textContent = err.message; }
  finally { $("#keySave").disabled = false; $("#keySave").textContent = "Save and connect"; }
});
$("#keyBtn").addEventListener("click", () => {
  if (S.keySource === "env") return toast("The API key comes from the KIE_API_KEY environment variable; change it there.");
  showKeyScreen({cancellable: true});
});

// ------------------------------------------------------------------ boot
(async function boot() {
  try {
    const c = await api("GET", "/api/config");
    S.reg = c.registry; S.creditUsd = c.credit_usd; S.keySource = c.key_source;
    if (!c.has_key) await showKeyScreen();
    if (!c.ffmpeg) toast("ffmpeg is missing, so thumbnails, frame grabs and stitching are off. Rerun the installer to fix.", "bad", 0);
  } catch (e) {
    document.body.insertAdjacentHTML("afterbegin", `<div class="toast bad" style="margin:20px">${esc(e.message)}</div>`); return;
  }
  if (!S.reg[S.kind]) S.kind = "video";
  loadDrafts();
  // a saved draft may reference images that were since hidden or deleted
  try {
    const ids = new Set((await api("GET", "/api/assets?limit=500")).map(a => a.id));
    for (const k of ["video", "image"]) for (const [sk, list] of Object.entries(S.draft[k].slots)) S.draft[k].slots[sk] = list.filter(a => ids.has(a.id));
  } catch {}
  $$("#tabs button").forEach(b => b.classList.toggle("active", b.dataset.kind === S.kind));
  fillModelFilter(); renderComposer(); updateNotifBtn();
  await Promise.all([loadHistory(), loadCredits(), loadHealth(), loadStats()]);
  // pick up anything already in flight (any kind) for the active panel
  try { for (const j of await api("GET", "/api/jobs?status=active&limit=50")) S.jobs.set(j.id, j); } catch {}
  renderActive(); pollSoon();
  setInterval(() => loadCredits(), 60000);
})();
