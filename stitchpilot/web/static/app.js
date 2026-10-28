// StitchPilot web: the page. All stitching happens on the server; this keeps the state (the same
// format the desktop app saves in projects), sends it for conversion and shows the result.
"use strict";

const $ = (id) => document.getElementById(id);
const store = {  // localStorage can be missing or blocked: everything still works without it
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* ignore */ } },
  session(k, v) {  // this tab only
    try { if (v === undefined) return sessionStorage.getItem(k); v == null ? sessionStorage.removeItem(k) : sessionStorage.setItem(k, v); }
    catch { return null; }
  },
};
const hex = (rgb) => "#" + rgb.map((v) => v.toString(16).padStart(2, "0")).join("");
const rgbOf = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
const same = (a, b) => a && b && a[0] === b[0] && a[1] === b[1] && a[2] === b[2];
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

let OPT = null;          // menus from the server
let state = null;        // the design's settings (desktop project format)
let image = null;        // {id, name, file, width, height}
let result = null;       // last conversion
let shape = null;        // selected shape
let shapeMode = false;
let apiKey = store.get("claude_key") || store.session("claude_key") || "";
const undoStack = [], redoStack = [];

// ------------------------------------------------------------------ server calls
const API = () => (window.STITCHPILOT_API || "").replace(/\/$/, "");  // set by config.js ("" = this site)

async function api(path, body, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  let init = { method: body === undefined ? "GET" : "POST", headers };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) { headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
  const r = await fetch(API() + path, init);
  const data = await r.json().catch(() => ({ error: `The server answered ${r.status}.` }));
  if (!r.ok) { const e = new Error(data.error || "Something went wrong."); e.status = r.status; throw e; }
  return data;
}

function toast(msg, error = false, ms = 5000) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (error ? " error" : "");
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), ms);
}

function busy(on, text = "Stitching…") {
  $("busy").hidden = !on;
  $("busy-text").textContent = text;
}

// ------------------------------------------------------------------ state + undo
function change(fn, { record = true } = {}) {
  if (record) {
    undoStack.push(JSON.stringify(state));
    if (undoStack.length > 80) undoStack.shift();
    redoStack.length = 0;
  }
  fn(state);
  remember();
  syncControls();
  scheduleConvert();
}
function undo() {
  if (!undoStack.length) return;
  redoStack.push(JSON.stringify(state));
  state = JSON.parse(undoStack.pop());
  syncControls(); scheduleConvert();
}
function redo() {
  if (!redoStack.length) return;
  undoStack.push(JSON.stringify(state));
  state = JSON.parse(redoStack.pop());
  syncControls(); scheduleConvert();
}
function remember() {  // machine choices carry over to the next visit
  for (const k of ["machine", "hoop", "fabric", "format", "chart", "max_w", "max_h"]) store.set("pref_" + k, JSON.stringify(state[k]));
}

// ------------------------------------------------------------------ conversion
let timer = null, running = false, again = false;
function scheduleConvert(delay = 350) {
  clearTimeout(timer);
  timer = setTimeout(convert, delay);
}
function canConvert() { return !!image || state.texts.some((t) => t.text.trim()); }

async function convert() {
  if (!canConvert()) { clearResult(); return; }
  if (running) { again = true; return; }
  running = true; again = false;
  busy(true);
  try {
    let r;
    try {
      r = await api("/api/convert", { image_id: image ? image.id : null, state, hoop: $("show-hoop").checked });
    } catch (e) {
      if (e.status !== 410 || !image || !image.file) throw e;
      await upload(image.file, { keepState: true });  // the server forgot the picture: send it again
      r = await api("/api/convert", { image_id: image.id, state, hoop: $("show-hoop").checked });
    }
    result = r;
    showResult();
  } catch (e) {
    toast(e.message, true, 8000);
  } finally {
    running = false;
    busy(false);
    if (again) convert();
  }
}

function clearResult() {
  result = null; shape = null;
  $("view").hidden = true; $("empty").hidden = false;
  $("stats").innerHTML = "<dt>Size</dt><dd>—</dd>";
  $("threads").innerHTML = ""; $("notes").innerHTML = "";
  updateButtons();
}

function showResult() {
  $("empty").hidden = true;
  if (currentTab === "stitches") $("view").hidden = false;
  $("preview").src = result.preview;
  const r = result;
  $("stats").innerHTML = [
    ["Size", `${r.width_mm} × ${r.height_mm} mm <span class="muted">(${(r.width_mm / 25.4).toFixed(2)} × ${(r.height_mm / 25.4).toFixed(2)} in)</span>`],
    ["Stitches", r.stitches.toLocaleString()], ["Colours", r.colours], ["Trims", r.trims],
    ...(r.stops ? [["Stops", `${r.stops} (appliqué)`]] : []), ["Sew time", `≈ ${Math.max(1, Math.round(r.minutes))} min`],
  ].map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
  $("notes").innerHTML = r.notes.map((n) => `<li>${esc(n)}</li>`).join("");
  fillThreads();
  $("cut-row").hidden = !r.applique_pieces;
  updateButtons();
  if (shape) reselectShape(); else drawOverlay();
}

// ------------------------------------------------------------------ threads
function fillThreads() {
  const ul = $("threads");
  ul.innerHTML = "";
  result.threads.forEach((t, i) => {
    const li = document.createElement("li");
    if (!t.visible) li.className = "off";
    const kinds = Object.entries(t.kinds).map(([k, n]) => (k === "run" ? "line" : k)).join(", ");
    li.innerHTML = `<label class="spool" title="Change colour"><input type="color" value="${hex(t.rgb)}"><span style="background:${hex(t.rgb)}"></span></label>
      <div class="info">${i + 1}. ${esc(t.thread || hex(t.rgb))}<small>${t.stitches.toLocaleString()} stitches · ${esc(kinds)}</small></div>
      <button type="button" title="Sew earlier" ${i === 0 ? "disabled" : ""}>▲</button>
      <button type="button" title="Sew later" ${i === result.threads.length - 1 ? "disabled" : ""}>▼</button>
      <button type="button" title="More">⋯</button>
      <input type="checkbox" title="Stitch this colour" ${t.visible ? "checked" : ""}>`;
    const [up, down, more] = li.querySelectorAll("button");
    li.querySelector("input[type=color]").addEventListener("change", (e) => recolour(t, rgbOf(e.target.value)));
    up.addEventListener("click", () => move(i, -1));
    down.addEventListener("click", () => move(i, 1));
    more.addEventListener("click", () => threadMenu(t));
    li.querySelector("input[type=checkbox]").addEventListener("change", (e) => change((s) => {
      s.hidden = s.hidden.filter((c) => !same(c, t.source));
      if (!e.target.checked) s.hidden.push(t.source);
    }));
    ul.appendChild(li);
  });
}
function recolour(t, rgb) {
  change((s) => {
    s.overrides = s.overrides.filter(([k]) => !same(k, t.source));
    if (!same(rgb, t.source)) s.overrides.push([t.source, rgb]);
  });
}
function move(i, step) {
  const order = result.threads.map((t) => t.source);
  const j = i + step;
  [order[i], order[j]] = [order[j], order[i]];
  change((s) => (s.order = order));
}
function threadMenu(t) {
  const dlg = $("menu-dlg");
  $("menu-title").textContent = t.thread || hex(t.rgb);
  const box = $("menu-applique");
  box.checked = state.applique.some((c) => same(c, t.source));
  box.disabled = t.border;
  dlg.returnValue = "";
  dlg.onclose = () => {
    const on = box.checked, was = state.applique.some((c) => same(c, t.source));
    if (on !== was) change((s) => {
      s.applique = s.applique.filter((c) => !same(c, t.source));
      if (on) s.applique.push(t.source);
    });
  };
  dlg.showModal();
}

// ------------------------------------------------------------------ uploads
async function upload(file, { keepState = false } = {}) {
  if (file.size > 20 * 1024 * 1024) { toast("That file is too big (20 MB at most).", true); return; }
  busy(true, "Reading the picture…");
  try {
    let r;
    if (OPT.uploads === "direct") {  // on AWS: the file goes straight to storage, then the server checks it
      const target = await api("/api/upload-url", { name: file.name, size: file.size });
      const fd = new FormData();
      for (const [k, v] of Object.entries(target.fields)) fd.append(k, v);
      fd.append("file", file, file.name);
      const put = await fetch(target.url, { method: "POST", body: fd });
      if (!put.ok) throw new Error("The upload didn't go through. Please try again.");
      r = await api("/api/upload-done", { key: target.key, name: file.name });
    } else {
      const fd = new FormData();
      fd.append("file", file, file.name);
      r = await api("/api/upload", fd);
    }
    if (r.project) {
      undoStack.length = redoStack.length = 0;
      state = { ...OPT.defaults, ...r.state };
      image = r.image_id ? { id: r.image_id, name: r.name, file, width: r.width, height: r.height } : null;
      toast("Project opened ✿");
    } else {
      if (!keepState) {
        undoStack.length = redoStack.length = 0;
        const keep = {};  // machine, size and file choices stay; picture-specific edits don't
        for (const k of ["machine", "hoop", "max_w", "max_h", "fabric", "chart", "snap", "format", "fabric_rgb", "trims"]) keep[k] = state[k];
        state = { ...OPT.defaults, ...keep };
        shape = null;
      }
      image = { id: r.image_id, name: r.name, file, width: r.width, height: r.height };
    }
    $("file-info").innerHTML = image ? `<b>${esc(image.name)}</b> · ${image.width}×${image.height}px` : "Lettering design";
    if ($("original").src.startsWith("blob:")) URL.revokeObjectURL($("original").src);
    // a picture is shown from the file itself; a project's picture comes from the server
    $("original").src = !image ? "" : r.project ? `${API()}/api/picture/${image.id}` : URL.createObjectURL(file);
    syncControls();
    if (!keepState) scheduleConvert(0);
  } catch (e) {
    toast(e.message, true, 8000);
  } finally {
    busy(false);
  }
}

// ------------------------------------------------------------------ controls <-> state
function fillSelect(sel, items, value) {
  sel.innerHTML = "";
  for (const [v, label] of items) {
    const o = document.createElement("option");
    o.value = v; o.textContent = label;
    sel.appendChild(o);
  }
  if (value !== undefined) sel.value = value;
}

function syncControls() {
  const s = state;
  $("machine").value = s.machine;
  const hoops = OPT.machines[s.machine] ? OPT.machines[s.machine].hoops : Object.keys(OPT.hoops);
  fillSelect($("hoop"), [...hoops.map((h) => [h, h]), ["Custom", "Custom"]], hoops.includes(s.hoop) ? s.hoop : "Custom");
  $("max-w").value = s.max_w; $("max-h").value = s.max_h;
  $("fabric").value = s.fabric;
  const cols = [["0", "Automatic"], ...Array.from({ length: 15 }, (_, i) => [String(i + 2), String(i + 2)])];
  if (s.ai_palette && s.ai_palette.length) cols.unshift(["ai", `AI: ${s.ai_palette.length} colours`]);
  fillSelect($("colors"), cols, String(s.colors));
  $("background").value = s.background;
  $("keep-inside").checked = !!s.keep_inside;
  $("simplicity").value = s.simplicity;
  $("border").value = s.border;
  $("border-rgb").value = hex(s.border_rgb);
  $("border-w").value = s.border_w; $("border-off").value = s.border_off;
  $("border-w").disabled = s.border !== "satin"; $("border-off").disabled = s.border === "none";
  $("fabric-rgb").value = hex(s.fabric_rgb);
  $("chart").value = s.chart || "";
  $("snap").checked = !!s.snap;
  $("format").value = s.format;
  fillTexts();
  updateButtons();
}

function fillTexts() {
  const ul = $("texts");
  ul.innerHTML = "";
  state.texts.forEach((t, i) => {
    const li = document.createElement("li");
    const what = t.kind === "monogram" ? "monogram " : Math.abs(t.arc || 0) >= 5 ? "curved " : "";
    li.innerHTML = `<span class="swatch" style="background:${hex(t.rgb)}"></span>
      <span class="label">${what}“${esc(t.text.split("\n")[0])}” · ${t.height_mm} mm</span>
      <button type="button" class="flat small">Edit</button><button type="button" class="flat small" title="Remove">✕</button>`;
    const [edit, del] = li.querySelectorAll("button");
    edit.addEventListener("click", () => textDialog(i));
    del.addEventListener("click", () => change((s) => s.texts.splice(i, 1)));
    ul.appendChild(li);
  });
  state.text_fixes.forEach((f, i) => {
    const li = document.createElement("li");
    li.innerHTML = `<span>✨</span><span class="label">cleaned “${esc(f.text)}” · ${esc(f.font)}${f.bold ? " bold" : ""}</span>
      <button type="button" class="flat small" title="Use the picture's own letters again">✕</button>`;
    li.querySelector("button").addEventListener("click", () => change((s) => s.text_fixes.splice(i, 1)));
    ul.appendChild(li);
  });
}

function updateButtons() {
  const ok = !!result && result.stitches > 0;
  for (const id of ["dl-embroidery", "dl-template", "dl-project", "dl-preview"]) $(id).disabled = !ok;
  $("shape-mode").disabled = !ok || !result.shapes;
  $("rotate").disabled = $("mirror").disabled = !image;
  $("ai-settings").disabled = !image;
  $("clean-text").disabled = !image;
  $("undo").disabled = !undoStack.length; $("redo").disabled = !redoStack.length;
  $("mirror").classList.toggle("on", !!state.mirror);
}

function bindControls() {
  const on = (id, ev, fn) => $(id).addEventListener(ev, fn);
  on("machine", "change", (e) => change((s) => {
    s.machine = e.target.value;
    const m = OPT.machines[s.machine];
    if (m) {
      s.format = m.format; if (m.chart) s.chart = m.chart;
      if (m.default_hoop) { s.hoop = m.default_hoop; fitHoop(s); }
    }
  }));
  on("hoop", "change", (e) => change((s) => { s.hoop = e.target.value; fitHoop(s); }));
  for (const [id, k] of [["max-w", "max_w"], ["max-h", "max_h"]]) {
    on(id, "change", (e) => change((s) => (s[k] = Math.max(10, Math.min(OPT.max_size_mm, +e.target.value || 90)))));
  }
  on("fabric", "change", (e) => change((s) => {
    s.fabric = e.target.value;
    for (const k of ["fill_spacing", "satin_spacing", "pull", "underlay"]) delete s[k];  // the preset decides
  }));
  on("colors", "change", (e) => change((s) => (s.colors = e.target.value === "ai" ? "ai" : +e.target.value)));
  on("background", "change", (e) => change((s) => (s.background = e.target.value)));
  on("keep-inside", "change", (e) => change((s) => (s.keep_inside = e.target.checked)));
  on("simplicity", "change", (e) => change((s) => {
    s.simplicity = +e.target.value;
    for (const k of ["merge", "min_shape", "thin", "smooth"]) delete s[k];  // the slider drives these
  }));
  on("border", "change", (e) => change((s) => (s.border = e.target.value)));
  on("border-rgb", "change", (e) => change((s) => (s.border_rgb = rgbOf(e.target.value))));
  on("border-w", "change", (e) => change((s) => (s.border_w = +e.target.value || 3)));
  on("border-off", "change", (e) => change((s) => (s.border_off = +e.target.value || 0)));
  on("fabric-rgb", "change", (e) => change((s) => (s.fabric_rgb = rgbOf(e.target.value)), { record: false }));
  on("chart", "change", (e) => change((s) => (s.chart = e.target.value || null)));
  on("snap", "change", (e) => change((s) => (s.snap = e.target.checked)));
  on("format", "change", (e) => change((s) => (s.format = e.target.value), { record: false }));
  on("show-hoop", "change", () => scheduleConvert(0));
  on("rotate", "click", () => change((s) => (s.rotate = (s.rotate + 90) % 360)));
  on("mirror", "click", () => change((s) => (s.mirror = !s.mirror)));
  on("undo", "click", undo);
  on("redo", "click", redo);
  on("add-text", "click", () => textDialog(-1, "text"));
  on("add-mono", "click", () => textDialog(-1, "monogram"));
  on("lettering-only", "click", () => {
    image = null; result = null; shape = null;
    undoStack.length = redoStack.length = 0;
    const keep = {};
    for (const k of ["machine", "hoop", "max_w", "max_h", "fabric", "chart", "format", "fabric_rgb"]) keep[k] = state[k];
    state = { ...OPT.defaults, ...keep };
    $("file-info").textContent = "Lettering design (no picture)";
    $("original").removeAttribute("src");
    syncControls(); clearResult();
    textDialog(-1, "text");
  });
  on("file", "change", (e) => { if (e.target.files[0]) upload(e.target.files[0]); e.target.value = ""; });
  on("drop", "keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("file").click(); } });
  document.addEventListener("dragover", (e) => { e.preventDefault(); $("drop").classList.add("over"); });
  document.addEventListener("dragleave", () => $("drop").classList.remove("over"));
  document.addEventListener("drop", (e) => {
    e.preventDefault(); $("drop").classList.remove("over");
    if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]);
  });
  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, textarea, select") || document.querySelector("dialog[open]")) return;
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === "z" && !e.shiftKey) { e.preventDefault(); undo(); }
    else if (mod && (e.key.toLowerCase() === "y" || (e.key.toLowerCase() === "z" && e.shiftKey))) { e.preventDefault(); redo(); }
    else if (shapeMode && shape && (e.key === "Delete" || e.key === "Backspace")) { e.preventDefault(); editShape({ kind: shape.kind === "skip" ? "auto" : "skip" }); }
    else if (shapeMode && e.key === "Escape") { shape = null; fillShapeBar(); drawOverlay(); }
  });
  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
  // downloads
  const name = () => (image ? image.name.replace(/\.[^.]+$/, "") : (state.texts[0] ? state.texts[0].text.split(/\s/)[0] : "design")) || "design";
  const get = async (kind, params = {}) => {
    if (!result) return;
    busy(true, "Preparing the file…");
    try {
      const body = { kind, name: name(), image_id: image ? image.id : null, state, hoop: $("show-hoop").checked, ...params };
      const r = await fetch(API() + "/api/download", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const type = r.headers.get("content-type") || "";
      if (!r.ok || type.startsWith("application/json")) {
        const data = await r.json().catch(() => ({}));
        if (!r.ok || !data.url) throw new Error(data.error || "Couldn't make that file.");
        location.href = data.url;  // a big file: a short-lived download link
        return;
      }
      const fn = /filename="([^"]+)"/.exec(r.headers.get("content-disposition") || "");
      const a = document.createElement("a");
      a.href = URL.createObjectURL(await r.blob());
      a.download = fn ? fn[1] : "design";
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 30000);
    } catch (e) {
      toast(e.message, true, 8000);
    } finally { busy(false); }
  };
  on("dl-embroidery", "click", () => get("embroidery", { ext: OPT.formats[state.format] || "pes" }));
  on("dl-template", "click", () => get("template", { paper: $("paper").value }));
  on("dl-cut-svg", "click", () => get("cut", { ext: "svg" }));
  on("dl-cut-dxf", "click", () => get("cut", { ext: "dxf" }));
  on("dl-project", "click", () => get("project"));
  on("dl-preview", "click", () => get("preview"));
  // AI
  on("key-btn", "click", keyDialog);
  on("ai-settings", "click", aiSettings);
  on("clean-text", "click", aiText);
  on("shape-ai", "click", aiShapes);
  on("theme-btn", "click", () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    store.set("theme", document.documentElement.dataset.theme);
    $("theme-btn").textContent = dark ? "🌙" : "☀️";
  });
}

function fitHoop(s) {  // a design fills its hoop minus a 5 mm margin each side
  const h = OPT.hoops[s.hoop];
  if (h) { s.max_w = Math.round(h[0] - 10); s.max_h = Math.round(h[1] - 10); }
}

let currentTab = "stitches";
function setTab(t) {
  currentTab = t;
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
  $("view").hidden = t !== "stitches" || !result;
  $("original").hidden = t !== "original" || !image;
  $("empty").hidden = t === "stitches" ? !!result : !!image;
}

// ------------------------------------------------------------------ lettering dialog
function textDialog(i, kind) {
  const t = i >= 0 ? state.texts[i] : {
    text: "", font: "Arial", bold: true, italic: false, height_mm: kind === "monogram" ? 30 : 12,
    rgb: [20, 20, 20], position: kind === "monogram" && image ? "over" : "below", arc: 0, kind: kind || "text",
    frame: kind === "monogram" ? "circle" : "none",
  };
  const dlg = $("text-dlg");
  $("t-kind").value = t.kind || "text";
  $("t-text").value = t.text;
  const fonts = OPT.fonts.includes(t.font) ? OPT.fonts : [t.font, ...OPT.fonts];
  fillSelect($("t-font"), fonts.map((f) => [f, f]), t.font);
  $("t-bold").checked = t.bold; $("t-italic").checked = t.italic;
  $("t-arc").value = t.arc || 0;
  fillSelect($("t-frame"), Object.entries(OPT.frames), t.frame || "none");
  $("t-height").value = t.height_mm;
  $("t-rgb").value = hex(t.rgb);
  $("t-pos").value = t.position;
  const read = () => {
    const mono = $("t-kind").value === "monogram";
    let text = $("t-text").value.trim();
    if (mono) text = text.replace(/\s+/g, "").slice(0, 3);
    return { text, font: $("t-font").value, bold: $("t-bold").checked, italic: $("t-italic").checked,
      height_mm: Math.max(4, Math.min(150, +$("t-height").value || 12)), rgb: rgbOf($("t-rgb").value),
      position: $("t-pos").value, arc: mono ? 0 : +$("t-arc").value, kind: mono ? "monogram" : "text",
      frame: mono ? $("t-frame").value : "none" };
  };
  let ptimer = null, pid = 0;
  const refresh = () => {
    const mono = $("t-kind").value === "monogram";
    document.querySelectorAll(".t-arc").forEach((el) => (el.hidden = mono));
    document.querySelectorAll(".t-frame").forEach((el) => (el.hidden = !mono));
    $("t-height-label").textContent = mono ? "Big letter height" : "Letter height";
    $("t-text").placeholder = mono ? "Initials, e.g. JMP (the middle letter is the biggest)" : "Type a name, initials or a slogan…";
    const a = +$("t-arc").value;
    $("t-arc-out").textContent = Math.abs(a) < 5 ? "Straight" : `${a > 0 ? "arch" : "smile"} ${Math.abs(a)}°`;
    clearTimeout(ptimer);
    ptimer = setTimeout(async () => {
      const item = read(), my = ++pid;
      if (!item.text) { $("t-preview").removeAttribute("src"); return; }
      try {
        const r = await fetch(API() + "/api/text-preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ item }) });
        if (r.ok && my === pid) {
          const blob = await r.blob();
          if (blob.size) { URL.revokeObjectURL($("t-preview").src); $("t-preview").src = URL.createObjectURL(blob); }
        }
      } catch { /* preview is a nicety */ }
    }, 250);
  };
  for (const id of ["t-kind", "t-text", "t-font", "t-bold", "t-italic", "t-arc", "t-frame", "t-rgb"]) {
    $(id).oninput = refresh; $(id).onchange = refresh;
  }
  refresh();
  dlg.returnValue = "";
  dlg.onclose = () => {
    if (dlg.returnValue !== "ok") return;
    const item = read();
    change((s) => {
      if (i >= 0) { if (item.text) s.texts[i] = item; else s.texts.splice(i, 1); }
      else if (item.text) s.texts.push(item);
    });
  };
  dlg.showModal();
}

// ------------------------------------------------------------------ shape editing
const KIND_NAMES = { fill: "Fill", satin: "Satin", run: "Line (bean stitch)", outline: "Outline only", skip: "Removed" };

function setShapeMode(on) {
  shapeMode = on;
  $("shape-mode").setAttribute("aria-pressed", String(on));
  $("shape-bar").hidden = !on;
  $("view").classList.toggle("shapes", on);
  if (!on) shape = null;
  fillShapeBar(); drawOverlay();
}

function mmFromEvent(e) {
  const img = $("preview"), L = result.layout;
  const r = img.getBoundingClientRect();
  const px = (e.clientX - r.left) * img.naturalWidth / r.width, py = (e.clientY - r.top) * img.naturalHeight / r.height;
  return [px / L.ppm + L.ox, py / L.ppm + L.oy];
}

function inside(x, y, rings) {  // even-odd rule: holes count
  let c = false;
  for (const r of rings) {
    for (let i = 0, j = r.length - 1; i < r.length; j = i++) {
      const [xi, yi] = r[i], [xj, yj] = r[j];
      if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) c = !c;
    }
  }
  return c;
}
function edgeDist(x, y, rings) {
  let best = Infinity;
  for (const r of rings) {
    for (let i = 0, j = r.length - 1; i < r.length; j = i++) {
      const [ax, ay] = r[j], [bx, by] = r[i], dx = bx - ax, dy = by - ay;
      const t = Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy || 1)));
      best = Math.min(best, Math.hypot(ax + t * dx - x, ay + t * dy - y));
    }
  }
  return best;
}
function shapeAt(x, y, slack = 0.4) {  // where shapes overlap or nearly touch, the smallest wins (as on desktop)
  const hits = (result.shape_list || []).filter((s) => inside(x, y, s.rings) || edgeDist(x, y, s.rings) <= slack);
  return hits.length ? hits.reduce((a, b) => (b.area_mm2 < a.area_mm2 ? b : a)) : null;
}
function pickShape(x, y) {
  shape = shapeAt(x, y);
  fillShapeBar(); drawOverlay();
}
function reselectShape() {
  if (!shape) return;
  pickShape(shape.centre[0], shape.centre[1]);
}

function fillShapeBar() {
  const kind = $("shape-kind");
  if (!kind.options.length) {
    fillSelect(kind, [["auto", "Auto"], ["fill", "Fill"], ["satin", "Satin"], ["outline", "Outline only"],
      ...Object.entries(OPT.patterns).filter(([k]) => k !== "plain")
        .map(([k, v]) => [`fill:${k}`, k === "waves" || k === "cross" ? `${v} fill` : v])]);
  }
  const sh = shape, e = sh && sh.edit;
  for (const id of ["shape-kind", "shape-angle", "shape-density", "shape-order", "shape-remove", "shape-reset"]) $(id).disabled = !sh;
  $("shape-reset-all").disabled = !state.shape_edits.length;
  if (!sh) {
    $("shape-label").textContent = `Click a shape to change it · ${result ? result.shapes : 0} shapes · Esc deselects`;
    kind.value = "auto"; $("shape-angle").value = ""; $("shape-density").value = 100; $("shape-order").value = "0";
    return;
  }
  let used = KIND_NAMES[sh.kind] || sh.kind;
  if (sh.kind === "fill" && sh.pattern !== "plain") used = OPT.patterns[sh.pattern] + " fill";
  $("shape-label").innerHTML = `<b>${esc(used)}</b> · ${Math.round(sh.area_mm2)} mm² · about ${sh.width_mm} mm wide${e ? " · <i>changed by you</i>" : ""}`;
  kind.options[0].textContent = `Auto (${KIND_NAMES[sh.auto_kind] || sh.auto_kind})`;
  let k = e && e.kind !== "skip" ? e.kind : "auto";
  if (k === "fill" && e.pattern && e.pattern !== "plain") k = `fill:${e.pattern}`;
  kind.value = k;
  const removed = sh.kind === "skip";
  kind.disabled = removed;
  $("shape-angle").value = e && e.angle != null ? e.angle : "";
  $("shape-angle").placeholder = sh.angle != null ? `Auto (${Math.round(sh.angle)}°)` : "Auto";
  $("shape-angle").disabled = removed || sh.kind !== "fill" || sh.angle == null;
  $("shape-density").value = Math.round((e ? e.density : 1) * 100);
  $("shape-density").disabled = removed || !["fill", "satin"].includes(sh.kind);
  $("shape-order").value = String(e ? e.order : 0);
  $("shape-remove").textContent = removed ? "↩ Bring back" : "🗑 Remove";
  $("shape-reset").disabled = !e;
}

function editShape(changes, { reset = false } = {}) {
  const sh = shape;
  if (!sh) return;
  change((s) => {
    const i = sh.edit ? s.shape_edits.findIndex((x) => JSON.stringify(x) === JSON.stringify(sh.edit)) : -1;
    const old = i >= 0 ? s.shape_edits[i] : { rgb: sh.rgb, fx: sh.fx, fy: sh.fy, kind: "auto", angle: null, density: 1, order: 0, pattern: "plain" };
    const base = { rgb: old.rgb, fx: old.fx, fy: old.fy, kind: "auto", angle: null, density: 1, order: 0, pattern: "plain" };
    const nu = reset ? base : { ...base, ...old, ...changes };
    if (i >= 0) s.shape_edits.splice(i, 1);
    const neutral = nu.kind === "auto" && nu.angle == null && nu.density === 1 && nu.order === 0 && nu.pattern === "plain";
    if (!neutral) s.shape_edits.push(nu);
  });
}

function drawOverlay() {
  const svg = $("overlay");
  if (!result || !shape || !shapeMode) { svg.innerHTML = ""; return; }
  const L = result.layout;
  svg.setAttribute("viewBox", `0 0 ${L.w} ${L.h}`);
  svg.setAttribute("preserveAspectRatio", "none");
  const P = ([x, y]) => `${((x - L.ox) * L.ppm).toFixed(1)},${((y - L.oy) * L.ppm).toFixed(1)}`;
  const d = shape.rings.map((r) => "M" + r.map(P).join("L") + "Z").join("");
  let html = `<path d="${d}" fill="rgba(255,111,174,.18)" fill-rule="evenodd" stroke="#ff4f9b" stroke-width="2.5" stroke-dasharray="7 5" vector-effect="non-scaling-stroke"/>`;
  if (shape.kind === "fill" && shape.angle != null) {
    const a = (shape.edit && shape.edit.angle != null ? shape.edit.angle : shape.angle) * Math.PI / 180;
    const half = Math.max(3, Math.min(14, Math.sqrt(shape.area_mm2) * 0.35));
    const [cx, cy] = shape.centre, dx = Math.cos(a) * half, dy = Math.sin(a) * half;
    html += `<line x1="${P([cx - dx, cy - dy]).replace(",", '" y1="')}" x2="${P([cx + dx, cy + dy]).replace(",", '" y2="')}" stroke="#9b7dff" stroke-width="3" vector-effect="non-scaling-stroke"/>`;
    html += `<circle cx="${P([cx + dx, cy + dy]).split(",")[0]}" cy="${P([cx + dx, cy + dy]).split(",")[1]}" r="${(1.2 * L.ppm).toFixed(1)}" fill="#9b7dff"/>`;
  }
  svg.innerHTML = html;
}

function bindShapes() {
  $("shape-mode").addEventListener("click", () => setShapeMode(!shapeMode));
  $("preview").addEventListener("click", (e) => { if (shapeMode && result) pickShape(...mmFromEvent(e)); });
  $("shape-kind").addEventListener("change", (e) => {
    const [kind, pattern] = e.target.value.split(":");
    editShape({ kind, pattern: pattern || "plain" });
  });
  $("shape-angle").addEventListener("change", (e) => editShape({ angle: e.target.value === "" ? null : ((+e.target.value % 180) + 180) % 180 }));
  $("shape-density").addEventListener("change", (e) => editShape({ density: Math.max(70, Math.min(150, +e.target.value || 100)) / 100 }));
  $("shape-order").addEventListener("change", (e) => editShape({ order: +e.target.value }));
  $("shape-remove").addEventListener("click", () => editShape({ kind: shape.kind === "skip" ? "auto" : "skip" }));
  $("shape-reset").addEventListener("click", () => editShape({}, { reset: true }));
  $("shape-reset-all").addEventListener("click", () => change((s) => (s.shape_edits = [])));
}

// ------------------------------------------------------------------ AI
function keyDialog() {
  return new Promise((resolve) => {
    const dlg = $("key-dlg");
    $("key-input").value = apiKey;
    dlg.returnValue = "";
    dlg.onclose = () => {
      if (dlg.returnValue === "ok" && $("key-input").value.trim()) {
        apiKey = $("key-input").value.trim();
        if ($("key-remember").checked) store.set("claude_key", apiKey);
        else { store.set("claude_key", null); store.session("claude_key", apiKey); }
        toast("Key saved on this device ✨");
      } else if (dlg.returnValue === "clear") {
        apiKey = ""; store.set("claude_key", null); store.session("claude_key", null);
        toast("Key forgotten.");
      }
      resolve(!!apiKey);
    };
    dlg.showModal();
  });
}

async function aiCall(path, body, text) {
  if (!apiKey && !(await keyDialog())) return null;
  busy(true, text);
  try {
    return await api(path, body, { headers: { "X-Claude-Key": apiKey } });
  } catch (e) {
    if (e.status === 410 && image && image.file) {
      await upload(image.file, { keepState: true });
      return api(path, { ...body, image_id: image.id }, { headers: { "X-Claude-Key": apiKey } });
    }
    toast(e.message, true, 9000);
    return null;
  } finally { busy(false); }
}

async function aiSettings() {
  if (!image) return;
  const r = await aiCall("/api/ai/settings", { image_id: image.id, state }, "AI is looking at your picture…");
  if (!r) return;
  change((s) => Object.assign(s, r.changes));
  const fit = { great: "Great fit for embroidery", good: "Good fit for embroidery", fair: "Will work with some simplification", poor: "Hard to embroider well" }[r.suitability];
  const warn = Math.min(state.max_w, state.max_h) < r.min_width_mm ? `<br>⚠ Details may be lost below about ${r.min_width_mm} mm wide.` : "";
  $("ai-note").innerHTML = `<b>${esc(r.subject)}</b><br>${fit} · ${r.changes.ai_palette.length} thread colours${warn}<ul>${r.tips.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>`;
  $("ai-note").hidden = false;
  $("ai-settings").textContent = "✨ Ask AI again";
}

function pickDialog(title, intro, rows, okText) {  // rows: [{html, checked, disabled, read()}]
  return new Promise((resolve) => {
    $("pick-title").textContent = title;
    $("pick-intro").textContent = intro;
    $("pick-ok").textContent = okText;
    const box = $("pick-rows");
    box.innerHTML = "";
    const ticks = rows.map((r) => {
      const div = document.createElement("div");
      div.className = "pick";
      div.innerHTML = `<input type="checkbox" ${r.checked ? "checked" : ""} ${r.disabled ? "disabled" : ""}><div>${r.html}</div>`;
      box.appendChild(div);
      r.el = div;
      return div.querySelector("input");
    });
    const dlg = $("pick-dlg");
    dlg.returnValue = "";
    dlg.onclose = () => resolve(dlg.returnValue === "ok" ? rows.filter((r, i) => ticks[i].checked) : null);
    dlg.showModal();
  });
}

async function aiText() {
  if (!image) return;
  const r = await aiCall("/api/ai/text", { image_id: image.id }, "AI is reading the text…");
  if (!r) return;
  if (!r.rows.length) { toast("No text found in this picture."); return; }
  const rows = r.rows.map((row) => {
    if (!row.fix) return { html: `“${esc(row.text)}” <span class="muted">– ${esc(row.note)}</span>`, disabled: true };
    const fonts = OPT.fonts.includes(row.fix.font) ? OPT.fonts : [row.fix.font, ...OPT.fonts];
    return {
      checked: row.score >= 0.35,
      html: `<div class="fields"><input type="text" value="${esc(row.fix.text)}" aria-label="Text">
        <select aria-label="Font">${fonts.map((f) => `<option${f === row.fix.font ? " selected" : ""}>${esc(f)}</option>`).join("")}</select>
        <label class="check"><input type="checkbox" ${row.fix.bold ? "checked" : ""}> Bold</label>
        <span class="muted" title="How closely the font's letters overlap the picture's. Under about 50%, try another font.">${Math.round(row.score * 100)}% match</span></div>`,
      read(el) {
        const [text, font, bold] = [el.querySelector("input[type=text]"), el.querySelector("select"), el.querySelectorAll("input[type=checkbox]")[1]];
        return { ...row.fix, text: text.value.trim(), font: font.value, bold: bold.checked };
      },
    };
  });
  const picked = await pickDialog("Clean up text", "StitchPilot can redraw this text as clean lettering before stitching, so the letters come out crisp. Check the words and the font, then tick what to redraw.", rows, "Redraw ticked text");
  if (!picked || !picked.length) return;
  const fixes = picked.map((p) => p.read(p.el)).filter((f) => f.text);
  change((s) => {
    const boxes = fixes.map((f) => JSON.stringify(f.box));
    s.text_fixes = s.text_fixes.filter((f) => !boxes.includes(JSON.stringify(f.box))).concat(fixes);
  });
}

async function aiShapes() {
  if (!result) return;
  const r = await aiCall("/api/ai/shapes", { image_id: image ? image.id : null, state }, "AI is reviewing every shape…");
  if (!r) return;
  if (!r.suggestions.length) { toast((r.summary || "") + " No changes suggested: the automatic choices look right.", false, 8000); return; }
  const rows = r.suggestions.map((x) => {
    const what = { keep: KIND_NAMES[x.kind] || x.kind, fill: "Fill", satin: "Satin", outline: "Outline only" }[x.stitch]
      + (x.angle != null && (x.stitch === "fill" || (x.stitch === "keep" && x.kind === "fill")) ? ` at ${x.angle}°` : "");
    return { checked: true, x, html: `<b>${esc(KIND_NAMES[x.kind] || x.kind)} shape</b> (${x.area_mm2} mm²) → ${esc(what)}<br><span class="muted">${esc(x.reason)}</span>` };
  });
  const picked = await pickDialog("AI suggestions for your shapes", r.summary || "Here's what could stitch better.", rows, "Use ticked suggestions");
  if (!picked || !picked.length) return;
  change((s) => {  // one undo step for all of them
    for (const { x } of picked) {
      const i = s.shape_edits.findIndex((e) => same(e.rgb, x.rgb) && Math.abs(e.fx - x.fx) < 1e-6 && Math.abs(e.fy - x.fy) < 1e-6);
      const old = i >= 0 ? s.shape_edits.splice(i, 1)[0] : { rgb: x.rgb, fx: x.fx, fy: x.fy, kind: "auto", angle: null, density: 1, order: 0, pattern: "plain" };
      if (x.stitch !== "keep") { old.kind = x.stitch; old.pattern = "plain"; }
      if (x.angle != null) old.angle = x.angle;
      s.shape_edits.push(old);
    }
  });
}

// ------------------------------------------------------------------ start
async function start() {
  const t = store.get("theme");
  if (t) { document.documentElement.dataset.theme = t; $("theme-btn").textContent = t === "dark" ? "☀️" : "🌙"; }
  try {
    OPT = await api("/api/options");
  } catch (e) {
    toast("Couldn't reach the StitchPilot server. Reload the page to try again.", true, 60000);
    return;
  }
  state = { ...OPT.defaults };
  for (const k of ["machine", "hoop", "fabric", "format", "chart", "max_w", "max_h"]) {
    const v = store.get("pref_" + k);
    if (v != null) { try { state[k] = JSON.parse(v); } catch { /* ignore */ } }
  }
  fillSelect($("machine"), Object.keys(OPT.machines).map((m) => [m, m]));
  fillSelect($("fabric"), OPT.fabrics.map((f) => [f, f]));
  fillSelect($("format"), Object.keys(OPT.formats).map((f) => [f, f]));
  fillSelect($("chart"), [["", "None"], ...OPT.charts.map((c) => [c, c])]);
  bindControls();
  bindShapes();
  syncControls();
}
start();
