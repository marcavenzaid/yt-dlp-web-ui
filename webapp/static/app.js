"use strict";

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- helpers

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok && data.error) throw new Error(data.error);
  return { ok: res.ok, data };
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node[k] = v;
  }
  for (const c of children) if (c != null) node.append(c);
  return node;
}

function bytes(n) {
  if (n == null || isNaN(n)) return "";
  const u = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}

function duration(s) {
  if (s == null) return "";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const pad = (x) => String(x).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

const checked = (name) => document.querySelector(`input[name=${name}]:checked`).value;

// "audio" | "video" (≤ quality) | "best" (unlimited video), as the server expects.
function selectedPreset() {
  return checked("kind") === "audio" ? checked("audioPreset") : checked("videoPreset");
}

function options() {
  return {
    preset: selectedPreset(),
    quality: $("quality").value,
    start: $("start").value.trim(),
    end: $("end").value.trim(),
    cookies: $("cookies").checked,
    browser: $("browser").value,
  };
}

function urls() {
  return $("urls").value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
}

function showError(msg) {
  $("formError").textContent = msg || "";
  $("formError").hidden = !msg;
}

// ------------------------------------------------- remembered form settings

const SETTINGS_KEY = "ytdlp-web-settings";
function saveSettings() {
  const o = options();
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify({
      preset: o.preset, quality: o.quality, cookies: o.cookies, browser: o.browser,
    }));
  } catch { /* storage unavailable */ }
}
function loadSettings() {
  let s;
  try { s = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "null"); } catch { s = null; }
  if (!s) return;
  const check = (name, value) => {
    const radio = document.querySelector(`input[name=${name}][value="${value}"]`);
    if (radio) radio.checked = true;
  };
  if (s.preset === "audio") check("kind", "audio");
  else if (s.preset === "video" || s.preset === "best") {
    check("kind", "video");
    check("videoPreset", s.preset);
  }
  if (s.quality) $("quality").value = s.quality;
  $("cookies").checked = !!s.cookies;
  if (s.browser) $("browser").value = s.browser;
}

// ---------------------------------------------------------------- options

function showKind() {
  const audio = checked("kind") === "audio";
  $("audioOpts").hidden = !audio;
  $("videoOpts").hidden = audio;
}

// Grow a read-only box to show all its lines. scrollHeight is the content
// height; offsetHeight - clientHeight adds the borders and, when long lines
// overflow sideways, the horizontal scrollbar, so no vertical one is needed.
function fitHeight(box) {
  box.style.height = "auto";
  box.style.height = `${box.scrollHeight + box.offsetHeight - box.clientHeight}px`;
}

// commands -> lines -> [text, extra] segments. Extra segments (launcher, config,
// output flags) are dimmed; the download options from the form stay prominent.
// Lines end with PowerShell's ` continuation; commands are separated by a blank line.
function renderCommand(commands) {
  const nodes = [];
  commands.forEach((lines, c) => {
    if (c) nodes.push("\n\n");
    lines.forEach((segs, l) => {
      if (l) nodes.push("\n  ");
      segs.forEach(([text, extra], s) => {
        if (s) nodes.push(" ");
        nodes.push(extra ? el("span", { className: "extra" }, text) : text);
      });
      if (l < lines.length - 1) {
        const lineExtra = segs[segs.length - 1][1];  // match the line's colour
        nodes.push(lineExtra ? el("span", { className: "extra" }, " `") : " `");
      }
    });
  });
  $("command").replaceChildren(...nodes);
}

// The command box is built by the server from the same code that runs downloads.
let commandSeq = 0;
let commandTimer = 0;
function scheduleCommand() {
  clearTimeout(commandTimer);
  commandTimer = setTimeout(updateCommand, 200);
}
async function updateCommand() {
  const seq = ++commandSeq;
  try {
    const { ok, data } = await api("POST", "/api/command", { urls: urls(), ...options() });
    if (seq !== commandSeq) return;           // a newer request is in flight
    if (!ok) throw new Error("Could not build the command. The server may be outdated, so restart it (start.bat).");
    renderCommand(data.commands);
    $("commandError").hidden = true;
  } catch (e) {
    if (seq !== commandSeq) return;
    // Keep the last valid command visible; explain what's wrong under it.
    $("commandError").textContent = e.message;
    $("commandError").hidden = false;
  }
}

// yt-dlp.conf is read by yt-dlp on every run; show its current contents.
async function loadConf() {
  try {
    const { ok, data } = await api("GET", "/api/config");
    if (!ok) throw new Error("Could not load yt-dlp.conf. The server may be outdated, so restart it (start.bat).");
    if (data.error) throw new Error(data.error);
    $("conf").title = data.path;
    $("conf").value = data.text;
    fitHeight($("conf"));
    $("confError").hidden = true;
  } catch (e) {
    $("confError").textContent = e.message;
    $("confError").hidden = false;
  }
}

// ---------------------------------------------------------------- preview

async function preview() {
  const list = urls();
  if (!list.length) return showError("Paste a URL first.");
  showError("");
  const btn = $("previewBtn");
  btn.disabled = true;
  btn.textContent = "Loading…";
  try {
    const { data } = await api("POST", "/api/info", { url: list[0], ...options() });
    renderPreview(data);
  } catch (e) {
    showError(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Show details";
  }
}

function renderPreview(info) {
  $("preview").hidden = false;
  $("pvThumb").src = info.thumbnail || "";
  $("pvThumb").hidden = !info.thumbnail;
  $("pvTitle").textContent = info.title || "(untitled)";
  const meta = [info.uploader, info.duration ? duration(info.duration) : null,
    info.entries != null ? `Playlist · ${info.entries} items` : null].filter(Boolean);
  $("pvMeta").textContent = meta.join(" · ");
  $("pvLink").href = info.webpage_url;

  const body = $("pvFormats");
  body.replaceChildren();
  for (const f of info.formats) {
    const size = f.size ? bytes(f.size) : f.size_approx ? `≈${bytes(f.size_approx)}` : "";
    const codec = (c) => (!c || c === "none" ? "—" : c);
    body.append(el("tr", {},
      el("td", { textContent: f.id ?? "" }),
      el("td", { textContent: f.ext ?? "" }),
      el("td", { textContent: f.resolution ?? "" }),
      el("td", { textContent: f.fps ?? "" }),
      el("td", { textContent: codec(f.vcodec) }),
      el("td", { textContent: codec(f.acodec) }),
      el("td", { textContent: size }),
      el("td", { textContent: f.tbr ? `${Math.round(f.tbr)}k` : "" }),
      el("td", { textContent: [f.note, f.language].filter(Boolean).join(", ") }),
    ));
  }
  $("pvCount").textContent = info.formats.length;
  $("pvFormatsWrap").hidden = !info.formats.length;
}

// --------------------------------------------------------------- download

async function submit(ev) {
  ev.preventDefault();
  const list = urls();
  if (!list.length) return showError("Paste a URL first.");
  showError("");
  saveSettings();
  const btn = $("downloadBtn");
  btn.disabled = true;
  try {
    await api("POST", "/api/jobs", { urls: list, ...options() });
    $("urls").value = "";
    $("start").value = "";
    $("end").value = "";
    $("preview").hidden = true;
    updateCommand();
    refresh();
  } catch (e) {
    showError(e.message);
  } finally {
    btn.disabled = false;
  }
}

// ---------------------------------------------------------- download folder

// The folder is stored by the server (settings.json), not the browser, because
// the server is what saves the files.
function showFolder(info) {
  $("saveToPath").textContent = info.path;
  $("saveToPath").title = info.path;
  $("resetFolderBtn").hidden = !info.custom;
}

async function loadFolder() {
  try {
    const { data } = await api("GET", "/api/download-dir");
    showFolder(data);
  } catch { $("saveToPath").textContent = "(server offline)"; }
}

async function chooseFolder() {
  const btn = $("folderBtn");
  btn.disabled = true;
  showError("");
  try {
    // Blocks until the folder dialog (opened on this PC by the server) is closed.
    const { data } = await api("POST", "/api/download-dir/choose");
    showFolder(data);
    updateCommand();
  } catch (e) {
    showError(e.message);
  } finally {
    btn.disabled = false;
  }
}

async function resetFolder() {
  try {
    const { data } = await api("DELETE", "/api/download-dir");
    showFolder(data);
    updateCommand();
  } catch (e) {
    showError(e.message);
  }
}

// ------------------------------------------------------------------ queue

const STATUS_LABEL = { queued: "Queued", running: "Downloading", done: "Done", error: "Failed", cancelled: "Cancelled" };
const nodes = new Map(); // job id -> { root, parts }

function presetLabel(o) {
  let s = o.preset === "video" ? `≤${o.quality}p` : o.preset === "best" ? "Best video" : "Audio";
  if (o.start || o.end) s += ` · ${o.start || "0"}–${o.end || "end"}`;
  if (o.cookies) s += ` · cookies: ${o.browser}`;
  return s;
}

function createJobNode(job) {
  const p = {
    title: el("div", { class: "job-title" }),
    url: el("div", { class: "job-url" }),
    badge: el("span", { class: "badge" }),
    cancel: el("button", { type: "button", class: "link", textContent: "Cancel",
      onclick: () => api("POST", `/api/jobs/${job.id}/cancel`).then(refresh).catch((e) => alert(e.message)) }),
    fill: el("div"),
    stats: el("div", { class: "stats" }),
    files: el("ul", { class: "files" }),
    error: el("p", { class: "error" }),
    log: el("pre"),
  };
  p.bar = el("div", { class: "bar" }, p.fill);
  p.logWrap = el("details", {}, el("summary", { textContent: "Log" }), p.log);
  const root = el("li", { class: "job" },
    el("div", { class: "job-head" },
      el("div", {}, p.title, p.url),
      el("div", { class: "job-actions" }, p.cancel, p.badge)),
    p.bar, p.stats, p.files, p.error, p.logWrap);
  return { root, p };
}

function updateJobNode({ root, p }, job) {
  root.className = `job ${job.status}`;
  p.title.textContent = job.title || job.url;
  p.url.textContent = `${job.title ? job.url + " · " : ""}${presetLabel(job.opts)}`;
  p.badge.className = `badge ${job.status}`;
  p.badge.textContent = STATUS_LABEL[job.status] || job.status;
  p.cancel.hidden = !(job.status === "queued" || job.status === "running");

  const running = job.status === "running";
  const pct = job.status === "done" ? 100 : job.percent ?? 0;
  const indeterminate = running && (job.percent == null || /^[A-Z]/.test(job.stage) || job.stage === "starting");
  p.bar.classList.toggle("indeterminate", indeterminate);
  p.fill.style.width = indeterminate ? "" : `${pct}%`;
  p.bar.hidden = job.status === "queued" || job.status === "cancelled";

  const stats = [];
  if (running) {
    if (job.item) stats.push(`Item ${job.item}`);
    if (job.stage) stats.push(/^[A-Z]/.test(job.stage) ? `Processing: ${job.stage}` : job.stage);
    if (job.percent != null && !indeterminate) stats.push(`${job.percent.toFixed(1)}%`);
    if (job.total) stats.push(`${bytes(job.downloaded)} / ${bytes(job.total)}`);
    if (job.speed) stats.push(`${bytes(job.speed)}/s`);
    if (job.eta != null) stats.push(`ETA ${duration(job.eta)}`);
  } else if (job.finished && job.started) {
    stats.push(`Took ${duration(job.finished - job.started)}`);
  }
  // One element per stat; spacing between them comes from the .stats flex gap.
  p.stats.replaceChildren(...stats.map((s) => el("span", { textContent: s })));

  if (p.files.childElementCount !== job.files.length) {
    p.files.replaceChildren(...job.files.map((f) => el("li", { textContent: f })));
  }
  p.error.textContent = job.status === "error" ? job.error || "Failed" : "";
  p.error.hidden = job.status !== "error";

  const logText = job.log.join("\n");
  if (p.log.textContent !== logText) {
    const atBottom = p.log.scrollTop + p.log.clientHeight >= p.log.scrollHeight - 4;
    p.log.textContent = logText;
    if (atBottom) p.log.scrollTop = p.log.scrollHeight;
  }
  p.logWrap.hidden = !job.log.length;
}

function renderJobs(jobs) {
  const list = $("jobs");
  const seen = new Set();
  // newest first
  jobs.slice().reverse().forEach((job, i) => {
    seen.add(job.id);
    let n = nodes.get(job.id);
    if (!n) { n = createJobNode(job); nodes.set(job.id, n); }
    updateJobNode(n, job);
    if (list.children[i] !== n.root) list.insertBefore(n.root, list.children[i] || null);
  });
  for (const [id, n] of nodes) if (!seen.has(id)) { n.root.remove(); nodes.delete(id); }
  $("empty").hidden = jobs.length > 0;
}

let polling = false;
async function refresh() {
  if (polling) return;
  polling = true;
  try {
    const { data } = await api("GET", "/api/jobs");
    renderJobs(data.jobs);
  } catch { /* server stopped; keep last state */ }
  finally { polling = false; }
}

// ----------------------------------------------------------------- update

async function loadVersion() {
  try {
    const { data } = await api("GET", "/api/version");
    $("version").textContent = `yt-dlp ${data.version}`;
  } catch { $("version").textContent = "yt-dlp (server offline)"; }
}

async function updateYtdlp() {
  const btn = $("updateBtn"), out = $("updateOut");
  btn.disabled = true;
  btn.textContent = "Updating…";
  out.hidden = false;
  out.textContent = "Running pip install -U \"yt-dlp[default]\" …";
  try {
    const res = await fetch("/api/update", { method: "POST" });
    const data = await res.json();
    const head = data.version
      ? (data.before === data.version ? `Already up to date (${data.version}).` : `Updated ${data.before} → ${data.version}.`)
      : "";
    out.textContent = [head, data.output].filter(Boolean).join("\n\n");
    loadVersion();
  } catch (e) {
    out.textContent = `Update failed: ${e.message}`;
  } finally {
    btn.disabled = false;
    btn.textContent = "Update yt-dlp";
  }
}

// ------------------------------------------------------------------ theme

const darkQuery = matchMedia("(prefers-color-scheme: dark)");

function storedTheme() {
  try { return localStorage.getItem("theme"); } catch { return null; }
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const label = theme === "dark" ? "Switch to light theme" : "Switch to dark theme";
  $("themeBtn").setAttribute("aria-label", label);
  $("themeBtn").title = label;
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  try { localStorage.setItem("theme", next); } catch { /* storage blocked */ }
  applyTheme(next);
}

// Follow OS changes until the user picks a theme explicitly.
darkQuery.addEventListener("change", (e) => {
  if (!storedTheme()) applyTheme(e.matches ? "dark" : "light");
});

// ------------------------------------------------------------------- init

$("form").addEventListener("submit", submit);
$("previewBtn").addEventListener("click", preview);
$("clearBtn").addEventListener("click", () => api("DELETE", "/api/jobs/finished").then(refresh));
$("openDownloadsBtn").addEventListener("click", () =>
  api("POST", "/api/open-downloads").catch((e) => alert(e.message)));
$("folderBtn").addEventListener("click", chooseFolder);
$("resetFolderBtn").addEventListener("click", resetFolder);
$("updateBtn").addEventListener("click", updateYtdlp);
$("themeBtn").addEventListener("click", toggleTheme);
$("settingsBtn").addEventListener("click", () => $("settings").showModal());
$("settingsClose").addEventListener("click", () => $("settings").close());
// Close when clicking the backdrop (clicks on the dialog box itself target its children).
$("settings").addEventListener("click", (e) => { if (e.target === $("settings")) $("settings").close(); });
$("urls").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("form").requestSubmit();
});
$("quality").addEventListener("change", () => {
  document.querySelector("input[name=videoPreset][value=video]").checked = true;
  scheduleCommand();
});
for (const r of document.querySelectorAll("input[name=kind]")) r.addEventListener("change", showKind);
$("form").addEventListener("input", scheduleCommand);
$("form").addEventListener("change", scheduleCommand);

applyTheme(document.documentElement.dataset.theme || (darkQuery.matches ? "dark" : "light"));
loadSettings();
showKind();
updateCommand();
loadConf();
window.addEventListener("focus", loadConf);  // pick up edits made to the file meanwhile
// A width change can add or remove the horizontal scrollbar, so re-fit.
window.addEventListener("resize", () => fitHeight($("conf")));
// Collapsible Command section: remember open/closed per browser. A collapsed box
// can't be measured, so re-fit the conf box when the section opens.
try { $("commandSection").open = localStorage.getItem("commandOpen") === "1"; } catch { /* storage blocked */ }
$("commandSection").addEventListener("toggle", () => {
  try { localStorage.setItem("commandOpen", $("commandSection").open ? "1" : "0"); } catch { /* storage blocked */ }
  if ($("commandSection").open) fitHeight($("conf"));
});
loadFolder();
loadVersion();
refresh();
setInterval(refresh, 1000);
