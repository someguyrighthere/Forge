"use strict";
const T = new URLSearchParams(location.search).get("t") || "";
const $ = (s) => document.querySelector(s);
const chat = $("#chat"), scroller = $("#scroll"), input = $("#input"), menu = $("#menu");
const S = { model: "", mode: "auto", cwd: "", used: 0, ctx: 16384, busy: false, ollama: false, models: [], recent: [], pulling: null, sessionId: "" };
let working = null, welcomeEl = null, lastSession = null, menuItems = [], menuSel = 0;
const streams = {}, history = [];

async function api(path, body) {
  const url = path + (path.includes("?") ? "&" : "?") + "t=" + encodeURIComponent(T);
  const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  try { return await (await fetch(url, opts)).json(); } catch (e) { return { error: String(e) }; }
}

/* ---------------------------------------------------------- markdown */
const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const KW = new Set("def class return if elif else for while import from as with try except finally raise pass break continue lambda yield async await in is not and or None True False function const let var new this typeof export default static public private void int string bool true false null undefined switch case do throw catch struct enum interface type fn pub use mut impl self echo".split(" "));
const HASH = new Set(["", "python", "py", "bash", "sh", "shell", "powershell", "ps1", "yaml", "yml", "toml", "ini", "dockerfile"]);

function hl(code, lang) {
  lang = (lang || "").toLowerCase();
  if (lang === "diff") return diffHtml(code);
  const re = /(#[^\n]*|\/\/[^\n]*|\/\*[\s\S]*?\*\/)|("(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`[^`]*`)|(\b\d+(?:\.\d+)?\b)|(\b[A-Za-z_]\w*\b)/g;
  let out = "", last = 0, m;
  while ((m = re.exec(code))) {
    out += esc(code.slice(last, m.index));
    if (m[1] !== undefined) out += m[1][0] === "#" && !HASH.has(lang) ? esc(m[1]) : `<span class="tk-c">${esc(m[1])}</span>`;
    else if (m[2] !== undefined) out += `<span class="tk-s">${esc(m[2])}</span>`;
    else if (m[3] !== undefined) out += `<span class="tk-n">${m[3]}</span>`;
    else out += KW.has(m[4]) ? `<span class="tk-k">${m[4]}</span>` : esc(m[4]);
    last = re.lastIndex;
  }
  return out + esc(code.slice(last));
}

function diffHtml(text) {
  return text.split("\n").map((l) => {
    const c = l.startsWith("+") && !l.startsWith("+++") ? "a" : l.startsWith("-") && !l.startsWith("---") ? "d" : l.startsWith("@@") || l.startsWith("---") || l.startsWith("+++") ? "h" : "";
    return `<span class="dl ${c}">${esc(l) || " "}</span>`;
  }).join("");
}

function inline(t) {
  const codes = [];
  t = esc(t).replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return `\u0001${codes.length - 1}\u0001`; });
  t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\s][^*]*)\*(?=[\s).,!?:;]|$)/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return t.replace(/\u0001(\d+)\u0001/g, (_, i) => `<code>${codes[i]}</code>`);
}

function codeBlock(lang, code) {
  return `<div class="code"><div class="codehead"><span>${esc(lang || "text")}</span><button class="copy">Copy</button></div><pre><code>${hl(code, lang)}</code></pre></div>`;
}

function md(src) {
  const blocks = [];
  src = src.replace(/```([\w+-]*)[^\n]*\n([\s\S]*?)(?:```|$)/g, (_, lang, code) => { blocks.push([lang, code.replace(/\n$/, "")]); return `\n@@BLOCK${blocks.length - 1}@@\n`; });
  const lines = src.split("\n");
  let html = "", para = [], list = null, m;
  const flushP = () => { if (para.length) { html += `<p>${inline(para.join(" "))}</p>`; para = []; } };
  const flushL = () => { if (list) { html += `</${list}>`; list = null; } };
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    if ((m = /^@@BLOCK(\d+)@@$/.exec(l))) { flushP(); flushL(); html += codeBlock(...blocks[+m[1]]); continue; }
    if (!l.trim()) { flushP(); flushL(); continue; }
    if ((m = /^(#{1,4})\s+(.*)$/.exec(l))) { flushP(); flushL(); html += `<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`; continue; }
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(l.trim())) { flushP(); flushL(); html += "<hr>"; continue; }
    if ((m = /^\s*[-*+]\s+(.*)$/.exec(l))) { flushP(); if (list !== "ul") { flushL(); html += "<ul>"; list = "ul"; } html += `<li>${inline(m[1])}</li>`; continue; }
    if ((m = /^\s*\d+[.)]\s+(.*)$/.exec(l))) { flushP(); if (list !== "ol") { flushL(); html += "<ol>"; list = "ol"; } html += `<li>${inline(m[1])}</li>`; continue; }
    if ((m = /^>\s?(.*)$/.exec(l))) { flushP(); flushL(); html += `<blockquote>${inline(m[1])}</blockquote>`; continue; }
    if (l.includes("|") && lines[i + 1] && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      flushP(); flushL();
      const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
      html += "<table><tr>" + cells(l).map((c) => `<th>${c}</th>`).join("") + "</tr>";
      i += 2;
      for (; i < lines.length && lines[i].includes("|"); i++) html += "<tr>" + cells(lines[i]).map((c) => `<td>${c}</td>`).join("") + "</tr>";
      html += "</table>"; i--; continue;
    }
    flushL(); para.push(l.trim());
  }
  flushP(); flushL();
  return html;
}

document.addEventListener("click", (e) => {
  if (e.target.classList.contains("copy")) {
    const pre = e.target.closest(".code").querySelector("pre");
    navigator.clipboard.writeText(pre.textContent);
    e.target.textContent = "Copied"; setTimeout(() => (e.target.textContent = "Copy"), 1200);
  }
});

/* -------------------------------------------------------------- chat */
function keepBottom(fn) {
  const near = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 90;
  fn();
  if (near) scroller.scrollTop = scroller.scrollHeight;
}

function add(el) {
  if (welcomeEl) { welcomeEl.remove(); welcomeEl = null; }
  working ? chat.insertBefore(el, working) : chat.appendChild(el);
  return el;
}

function addUser(text) { const el = document.createElement("div"); el.className = "msg user"; el.textContent = text; add(el); scroller.scrollTop = scroller.scrollHeight; }
function botEl(id) { const el = document.createElement("div"); el.className = "msg bot"; if (id) streams[id] = el; return add(el); }
function setMd(el, text, cursor) { el.innerHTML = md(text); el.classList.toggle("cursor", !!cursor); }
function addNotice(text, level) { const el = document.createElement("div"); el.className = "note " + (level || ""); el.textContent = text; add(el); }

const LABEL = {
  read_file: ["Reading", "Read"], write_file: ["Writing", "Wrote"], edit_file: ["Editing", "Edited"], list_dir: ["Listing", "Listed"],
  glob_files: ["Finding files", "Found files"], grep: ["Searching", "Searched"], run_command: ["Running", "Ran"],
  python_debug: ["Debugging", "Debugged"],
  github_prs: ["Checking pull requests", "Checked pull requests"], github_issues: ["Checking issues", "Checked issues"],
  github_actions: ["Checking Actions runs", "Checked Actions runs"],
  notebook_read: ["Reading notebook", "Read notebook"], notebook_edit: ["Editing notebook", "Edited notebook"],
  browser_read: ["Opening in browser", "Read in browser"],
  web_search: ["Searching the web", "Searched the web"], web_fetch: ["Fetching", "Fetched"], task: ["Researching", "Researched"],
};

function targetOf(name, a) {
  switch (name) {
    case "read_file": case "write_file": case "edit_file": case "list_dir": case "notebook_read": case "notebook_edit":
      return a.path || ".";
    case "run_command": return a.command || "";
    case "python_debug": return a.script || "";
    case "github_prs": case "github_issues": case "github_actions": {
      const id = a.number || a.run_id;
      return `${a.repo || ""}${id ? " #" + id : a.query ? " " + a.query : ""}`.trim();
    }
    case "python_hover": case "python_definition": case "python_references": case "python_rename_impact":
    case "python_call_hierarchy": case "python_symbols": case "python_diagnostics":
      return `${a.path || ""}${a.symbol ? " · " + a.symbol : ""}`;
    case "glob_files": case "grep": return `${a.pattern || ""}${a.path && a.path !== "." ? " in " + a.path : ""}`;
    case "web_search": return a.query || "";
    case "web_fetch": case "browser_read": return a.url || "";
    case "task": return (a.prompt || "").slice(0, 90);
  }
  return "";
}

function addCall(ev) {
  if (ev.name === "todo") return;
  const el = document.createElement("div");
  el.className = "call" + (ev.depth ? " d1" : "");
  el.id = "c" + ev.id;
  el.innerHTML = '<div class="callhead"><span class="ico spin"></span><span class="lbl"></span><code class="tgt"></code><span class="chev">›</span></div>';
  el.querySelector(".lbl").textContent = (LABEL[ev.name] || [ev.name, ev.name])[0];
  const tgt = el.querySelector(".tgt"), t = targetOf(ev.name, ev.args || {});
  t ? (tgt.textContent = t) : tgt.remove();
  el.querySelector(".callhead").onclick = () => {
    const body = el.querySelector(".callbody");
    if (body) { body.hidden = !body.hidden; el.classList.toggle("open", !body.hidden); }
  };
  el._ev = ev;
  add(el);
}

function finishCall(ev) {
  const el = $("#c" + ev.id);
  if (!el) return;
  const a = (el._ev && el._ev.args) || {}, bad = /^Error|declined/.test(ev.result);
  const ico = el.querySelector(".ico");
  ico.className = "ico " + (bad ? "err" : "ok"); ico.textContent = bad ? "✗" : "✓";
  el.querySelector(".lbl").textContent = (LABEL[ev.name] || [ev.name, ev.name])[1];
  let html, open = false;
  if (ev.name === "edit_file" && !bad) {
    const o = (a.old_string || "").split("\n").map((l) => "- " + l), n = (a.new_string || "").split("\n").map((l) => "+ " + l);
    html = diffHtml(o.concat(n).join("\n")); open = true;
  } else if (ev.name === "write_file" && !bad) {
    html = diffHtml((a.content || "").split("\n").slice(0, 60).map((l) => "+ " + l).join("\n")); open = true;
  } else if (ev.name === "run_command") {
    html = esc("$ " + (a.command || "") + "\n" + ev.result); open = !bad;
  } else html = esc(ev.result);
  const body = document.createElement("div");
  body.className = "callbody"; body.hidden = !open;
  body.innerHTML = `<pre>${html}</pre>`;
  el.appendChild(body);
  el.classList.toggle("open", open);
}

const VERB = { run_command: "Run this command?", python_debug: "Run this script under the debugger?", edit_file: "Edit this file?", write_file: "Write this file?", notebook_edit: "Edit this notebook?" };
function addApproval(ev) {
  const el = document.createElement("div");
  el.className = "approval"; el.id = "a" + ev.id;
  const isCmd = ev.name === "run_command" || ev.name === "python_debug";
  el.innerHTML = `<div class="ah"><span class="st">⚠</span><span>${VERB[ev.name] || "Allow this action?"}</span><code></code></div><pre>${isCmd ? esc(ev.diff) : diffHtml(ev.diff)}</pre><div class="btns"><button class="btn go" data-a="allow">Allow</button><button class="btn" data-a="always">Always allow this session</button><button class="btn" data-a="deny">Deny</button></div>`;
  if (!isCmd) el.querySelector("code").textContent = ev.target;
  el.querySelector(".btns").onclick = (e) => {
    const a = e.target.dataset.a;
    if (!a) return;
    el.querySelectorAll("button").forEach((b) => (b.disabled = true));
    api("/api/approve", { id: ev.id, answer: a });
  };
  add(el);
}

function renderTodos(items) {
  const box = $("#todo");
  if (!items || !items.length) { box.hidden = true; return; }
  const done = items.filter((t) => t.done).length, collapsed = box.dataset.collapsed === "1";
  box.hidden = false;
  box.innerHTML = `<div class="th"><span>Tasks · ${done}/${items.length}</span><span>${collapsed ? "▸" : "▾"}</span></div>` +
    (collapsed ? "" : items.map((t) => `<div class="ti ${t.done ? "done" : ""}"><span>${t.done ? "✓" : "○"}</span><span>${esc(t.task)}</span></div>`).join(""));
  box.querySelector(".th").onclick = () => { box.dataset.collapsed = collapsed ? "0" : "1"; renderTodos(items); };
  box._items = items;
}

function setBusy(v) {
  S.busy = v;
  const b = $("#send");
  b.textContent = v ? "■" : "↑"; b.classList.toggle("stop", v); b.title = v ? "Stop (Esc)" : "Send (Enter)";
  if (v && !working) {
    working = document.createElement("div"); working.className = "working";
    working.innerHTML = '<span class="ico spin"></span><span class="shim">Thinking…</span>';
    if (welcomeEl) { welcomeEl.remove(); welcomeEl = null; }
    chat.appendChild(working);
  } else if (!v && working) { working.remove(); working = null; }
  keepBottom(() => {});
}

function handle(ev) {
  keepBottom(() => {
    switch (ev.type) {
      case "reset": chat.innerHTML = ""; Object.keys(streams).forEach((k) => delete streams[k]); working = null; welcomeEl = null; renderTodos([]); showWelcome(); if (S.busy) setBusy(true); break;
      case "user": addUser(ev.text); break;
      case "assistant": setMd(botEl(), ev.text, false); break;
      case "stream_start": botEl(ev.id); break;
      case "stream": if (!streams[ev.id]) botEl(ev.id); setMd(streams[ev.id], ev.text, true); break;
      case "stream_end": { const el = streams[ev.id]; if (el) { ev.text ? setMd(el, ev.text, false) : el.remove(); delete streams[ev.id]; } break; }
      case "call": addCall(ev); break;
      case "result": finishCall(ev); break;
      case "approval": addApproval(ev); break;
      case "approval_done": { const el = $("#a" + ev.id); if (el) { el.classList.add("done"); el.querySelector(".st").textContent = ev.answer === "deny" ? "✗" : "✓"; el.querySelector(".ah span:nth-child(2)").textContent = ev.answer === "deny" ? "Denied" : ev.answer === "always" ? "Allowed for this session" : "Allowed"; } break; }
      case "todos": renderTodos(ev.items); break;
      case "notice": addNotice(ev.text, ev.level); break;
      case "status": if (working) working.querySelector(".shim").textContent = ev.text || "Thinking…"; break;
      case "busy": setBusy(ev.value); if (!ev.value) refreshSessions(); break;
      case "state": Object.assign(S, ev.state); renderState(); break;
      case "pull": renderPull(ev); break;
    }
  });
}

/* ---------------------------------------------------------- welcome */
const SUGGEST = [
  ["Explain this project", "Look through the files and tell me what this project does."],
  ["Find and fix a bug", "Run the tests and fix anything that fails."],
  ["Write a feature", "Add a new feature to this project. Ask me what it should do first."],
  ["Review my code", "Review the code in this folder and list the top 5 improvements."],
];

function showWelcome() {
  if (chat.children.length) return;
  const el = document.createElement("div");
  el.className = "welcome";
  let banner = "";
  const installed = S.models.map((m) => m.name);
  if (!S.ollama) banner = `<div class="banner"><b>Ollama isn't running.</b><br>Forge runs models locally through Ollama. If it's installed, it should start in a few seconds; otherwise get it from <a href="https://ollama.com/download" target="_blank" rel="noopener">ollama.com</a>.</div>`;
  else if (!installed.length) banner = `<div class="banner"><b>No models installed yet.</b><br>Download a model to get started (about 5 GB).<br><button class="btn go" id="dlDefault">Download qwen3:8b</button><div class="bar-prog" id="pullProg" ${S.pulling ? "" : "hidden"}><i></i></div></div>`;
  else if (!installed.includes(S.model)) banner = `<div class="banner"><b>${esc(S.model)} isn't installed.</b><br>Pick one of your installed models below the chat box, or download it.<br><button class="btn go" id="dlDefault">Download ${esc(S.model)}</button></div>`;
  el.innerHTML = `<div class="logo big">F</div><h1>What should we build?</h1><p>Your local coding agent. It can read and edit files, run commands and search the web, all on this PC.</p>${banner}<div class="sugg">${SUGGEST.map((s, i) => `<button data-i="${i}"><b>${s[0]}</b><span>${s[1]}</span></button>`).join("")}</div>`;
  el.querySelector(".sugg").onclick = (e) => { const b = e.target.closest("button"); if (b) { input.value = SUGGEST[+b.dataset.i][1]; autosize(); input.focus(); } };
  const dl = el.querySelector("#dlDefault");
  if (dl) dl.onclick = () => api("/api/command", { cmd: "pull", arg: S.model || "qwen3:8b" });
  chat.appendChild(el); welcomeEl = el;
}

function renderPull(ev) {
  const chip = $("#pullChip");
  if (ev.status === "done") { if (chip) chip.remove(); return; }
  let c = chip;
  if (!c) { c = document.createElement("span"); c.id = "pullChip"; c.className = "chip"; $("header .grow").after(c); }
  c.textContent = `⬇ ${ev.name} ${ev.pct == null ? ev.status : ev.pct + "%"}`;
  const prog = $("#pullProg");
  if (prog) { prog.hidden = false; prog.firstElementChild.style.width = (ev.pct || 0) + "%"; }
}

/* ------------------------------------------------------------ state */
const fmt = (n) => (n >= 1000 ? (n / 1000).toFixed(1).replace(".0", "") + "k" : String(n));
const MODE_LABEL = { auto: "⚡ Auto", ask: "✋ Ask", plan: "🗺 Plan" };
const MODE_DESC = { auto: "Edits and runs commands without asking", ask: "Confirms each edit and command", plan: "Read-only: research and propose a plan" };

function renderState() {
  $("#ver").textContent = "v" + (S.version || "");
  $("#folderName").textContent = S.cwd ? S.cwd.split(/[\\/]/).filter(Boolean).pop() || S.cwd : "Choose folder";
  $("#folderBtn").title = S.cwd;
  $("#dot").className = "dot" + (S.ollama ? " ok" : ""); $("#dot").title = S.ollama ? "Ollama connected" : "Ollama not reachable";
  $("#modeBtn").textContent = MODE_LABEL[S.mode] || S.mode;
  $("#modelBtn").textContent = S.model || "Choose model";
  const pct = Math.min(100, Math.round((S.used / S.ctx) * 100)) || 0;
  $("#ctxFill").style.width = pct + "%"; $("#ctxFill").style.background = pct > 90 ? "var(--red)" : pct > 75 ? "var(--yellow)" : "var(--green)";
  $("#ctxText").textContent = `${fmt(S.used)} / ${fmt(S.ctx)}`;
  renderUpdate();
  showWhatsNew();
  if (S.busy !== !!working) setBusy(S.busy);
  if (welcomeEl) { welcomeEl.remove(); welcomeEl = null; showWelcome(); }
  if (lastSession !== S.sessionId) { lastSession = S.sessionId; refreshSessions(); }
}

function renderUpdate() {
  let b = $("#updBtn");
  if (!S.update) { if (b) b.remove(); return; }
  if (!b) { b = document.createElement("button"); b.id = "updBtn"; b.className = "btn go"; $("header .grow").after(b); b.onclick = showUpdateDialog; }
  b.textContent = "\u2b06 Update to v" + S.update.version;
  b.title = "See what's new in this version";
}

// Release notes are GitHub markdown; this renders the small subset they use. Text is escaped first.
function notesHtml(text) {
  let html = "", list = false;
  for (const raw of esc(text || "No release notes were provided for this version.").split(/\r?\n/)) {
    const line = raw.trim().replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
    const item = line.match(/^[-*]\s+(.*)/), head = line.match(/^#{1,6}\s+(.*)/);
    if (item) { if (!list) { html += "<ul>"; list = true; } html += `<li>${item[1]}</li>`; continue; }
    if (list) { html += "</ul>"; list = false; }
    if (head) html += `<h4>${head[1]}</h4>`; else if (line) html += `<p>${line}</p>`;
  }
  return html + (list ? "</ul>" : "");
}

function showUpdateDialog() {
  if (!S.update) return;
  const m = modal(`<h3>Forge v${esc(S.update.version)} is available</h3><small>You have v${esc(S.version || "")}. Your chats and settings are kept; Forge restarts to finish.</small><div class="notes">${notesHtml(S.update.notes)}</div><div class="row"><button class="btn" id="mLater">Later</button><button class="btn go" id="mUpdate">Update now</button></div>`);
  m.querySelector("#mLater").onclick = closeModal;
  m.querySelector("#mUpdate").onclick = async () => {
    closeModal();
    const b = $("#updBtn");
    if (b) { b.disabled = true; b.textContent = "Updating\u2026"; }
    const r = await command("update");
    if (r.error && b) { b.disabled = false; renderUpdate(); }
  };
}

let whatsNewShown = false;
function showWhatsNew() {
  if (whatsNewShown || !S.whatsNew) return;
  whatsNewShown = true;
  const m = modal(`<h3>Forge updated to v${esc(S.whatsNew.version)}</h3><small>Here is what's new:</small><div class="notes">${notesHtml(S.whatsNew.notes)}</div><div class="row"><button class="btn go" id="mGotIt">Got it</button></div>`);
  const done = () => { closeModal(); command("whatsnew_seen"); };
  m.querySelector("#mGotIt").onclick = done;
  m.onmousedown = (e) => { if (e.target === m) done(); };
}

async function refreshSessions() {
  const list = await api("/api/sessions"), box = $("#sessions");
  box.innerHTML = "";
  if (!Array.isArray(list) || !list.length) { box.innerHTML = '<div class="empty">No saved chats yet</div>'; return; }
  for (const s of list) {
    const row = document.createElement("div");
    row.className = "sess" + (s.id === S.sessionId ? " active" : ""); row.title = s.cwd;
    row.innerHTML = '<span class="t"></span><button class="x" title="Delete">✕</button>';
    row.querySelector(".t").textContent = s.title;
    row.onclick = (e) => { if (e.target.classList.contains("x")) { api("/api/command", { cmd: "delete", arg: s.id }).then(refreshSessions); } else command("resume", s.id); };
    box.appendChild(row);
  }
}

async function command(cmd, arg) {
  const r = await api("/api/command", { cmd, arg: arg || "" });
  if (r.error) keepBottom(() => addNotice(r.error, "error"));
  return r;
}

/* ----------------------------------------------------------- popups */
function closePopup() { document.querySelectorAll(".popup").forEach((p) => p.remove()); }
document.addEventListener("mousedown", (e) => { if (!e.target.closest(".popup")) closePopup(); });

function popup(anchor, items, below) {
  closePopup();
  const p = document.createElement("div"); p.className = "popup";
  for (const it of items) {
    if (it.sep) { p.insertAdjacentHTML("beforeend", '<div class="msep"></div>'); continue; }
    const row = document.createElement("div"); row.className = "mi";
    row.innerHTML = `<span class="c">${it.check ? "✓" : ""}</span><span class="l"></span><span class="d"></span>`;
    row.querySelector(".l").textContent = it.label; row.querySelector(".d").textContent = it.desc || "";
    row.onclick = () => { closePopup(); it.run(); };
    p.appendChild(row);
  }
  document.body.appendChild(p);
  const r = anchor.getBoundingClientRect();
  p.style.left = Math.max(8, Math.min(r.left, innerWidth - p.offsetWidth - 8)) + "px";
  if (below) p.style.top = r.bottom + 6 + "px"; else p.style.bottom = innerHeight - r.top + 6 + "px";
}

function modeMenu() { popup($("#modeBtn"), Object.keys(MODE_LABEL).map((m) => ({ label: MODE_LABEL[m], desc: MODE_DESC[m], check: S.mode === m, run: () => command("mode", m) }))); }
function modelMenu() {
  const items = S.models.map((m) => ({ label: m.name, desc: (m.size / 1e9).toFixed(1) + " GB", check: m.name === S.model, run: () => command("model", m.name) }));
  if (items.length) items.push({ sep: true });
  items.push({ label: "Download a model…", run: pullDialog });
  popup($("#modelBtn"), items);
}
async function folderMenu() {
  const items = S.recent.map((p) => ({ label: p.split(/[\\/]/).filter(Boolean).pop() || p, desc: p, check: p === S.cwd, run: () => setFolder(p) }));
  if (items.length) items.push({ sep: true });
  items.push({ label: "Open folder…", run: () => setFolder("") });
  popup($("#folderBtn"), items, true);
}
async function setFolder(path) {
  const r = await api("/api/folder", { path });
  if (r.error) keepBottom(() => addNotice(r.error, "error"));
}

function modal(html) { const m = $("#modal"); m.innerHTML = `<div class="dlg">${html}</div>`; m.hidden = false; m.onmousedown = (e) => { if (e.target === m) closeModal(); }; return m; }
function closeModal() { $("#modal").hidden = true; $("#modal").innerHTML = ""; }

async function memoryDialog() {
  const mem = await api("/api/memory");
  const m = modal(`<h3>Memory</h3><small>Notes Forge sees at the start of every chat. Use /remember &lt;note&gt; to add one quickly.</small><textarea id="memText"></textarea><small></small><div class="row"><button class="btn" id="mCancel">Cancel</button><button class="btn go" id="mSave">Save</button></div>`);
  m.querySelector("#memText").value = mem.text || ""; m.querySelector("small:nth-of-type(2)").textContent = mem.path || "";
  m.querySelector("#mCancel").onclick = closeModal;
  m.querySelector("#mSave").onclick = async () => { await api("/api/memory", { text: m.querySelector("#memText").value }); closeModal(); };
}

function pullDialog() {
  const m = modal(`<h3>Download a model</h3><small>Models run locally through Ollama. Smaller models are faster; bigger ones are smarter. 8 GB of GPU memory handles 7–8B models well.</small><input id="pullName" style="background:var(--code);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px;font:inherit;outline:0" placeholder="e.g. qwen3:8b"><div class="sugg" style="grid-template-columns:repeat(3,1fr)">${["qwen3:8b", "qwen2.5-coder:7b", "qwen3:4b", "llama3.1:8b", "deepseek-r1:8b", "gemma3:4b"].map((n) => `<button class="btn" data-n="${n}">${n}</button>`).join("")}</div><div class="row"><button class="btn" id="mCancel">Cancel</button><button class="btn go" id="mGo">Download</button></div>`);
  const field = m.querySelector("#pullName");
  m.querySelector(".sugg").onclick = (e) => { if (e.target.dataset.n) field.value = e.target.dataset.n; };
  m.querySelector("#mCancel").onclick = closeModal;
  m.querySelector("#mGo").onclick = async () => { const n = field.value.trim(); if (n) { closeModal(); const r = await command("pull", n); if (!r.error) keepBottom(() => addNotice("Downloading " + n + "…")); } };
  field.focus();
}

/* ------------------------------------------------------------ input */
const SLASH = [
  { name: "/new", desc: "Start a new chat", run: () => command("new") },
  { name: "/model", desc: "Switch model", run: modelMenu },
  { name: "/mode", desc: "Auto, Ask or Plan", run: modeMenu },
  { name: "/undo", desc: "Undo the last file edit", run: () => command("undo") },
  { name: "/compact", desc: "Summarise older messages", run: () => command("compact") },
  { name: "/init", desc: "Write an AGENT.md for this project", run: () => command("init") },
  { name: "/remember", desc: "Save a note to memory", arg: true, run: (a) => command("remember", a) },
  { name: "/memory", desc: "View and edit memory", run: memoryDialog },
  { name: "/folder", desc: "Open another project folder", run: () => setFolder("") },
];

function autosize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 220) + "px"; }

function hideMenu() { menu.hidden = true; menuItems = []; }
function showMenu(items) {
  menuItems = items; menuSel = 0;
  if (!items.length) return hideMenu();
  menu.innerHTML = "";
  items.forEach((it, i) => {
    const row = document.createElement("div"); row.className = "mi" + (i === 0 ? " sel" : "");
    row.innerHTML = '<span class="l"></span><span class="d"></span>'; row.querySelector(".l").textContent = it.label; row.querySelector(".d").textContent = it.desc || "";
    row.onmousedown = (e) => { e.preventDefault(); pick(i); };
    menu.appendChild(row);
  });
  menu.hidden = false;
}
function moveMenu(d) { menuSel = (menuSel + d + menuItems.length) % menuItems.length; [...menu.children].forEach((c, i) => c.classList.toggle("sel", i === menuSel)); menu.children[menuSel].scrollIntoView({ block: "nearest" }); }
function pick(i) { const it = menuItems[i]; hideMenu(); it.apply(); }

let fileTimer = 0;
function updateMenu() {
  const before = input.value.slice(0, input.selectionStart);
  let m;
  if (/^\/\S*$/.test(before)) {
    showMenu(SLASH.filter((c) => c.name.startsWith(before)).map((c) => ({ label: c.name, desc: c.desc, apply: () => { if (c.arg) { input.value = c.name + " "; autosize(); } else { input.value = ""; autosize(); c.run(); } } })));
  } else if ((m = /(?:^|\s)@(\S*)$/.exec(before))) {
    clearTimeout(fileTimer);
    const q = m[1], start = input.selectionStart - q.length - 1;
    fileTimer = setTimeout(async () => {
      const files = await api("/api/files?q=" + encodeURIComponent(q));
      if (!Array.isArray(files)) return;
      showMenu(files.map((f) => ({ label: f, apply: () => { input.value = input.value.slice(0, start) + "@" + f + " " + input.value.slice(input.selectionStart); input.focus(); autosize(); } })));
    }, 120);
  } else hideMenu();
}

async function submit() {
  const text = input.value.trim();
  if (!text) return;
  if (S.busy) return;
  if (text.startsWith("/")) {
    const [name, ...rest] = text.split(/\s+/), cmd = SLASH.find((c) => c.name === name);
    if (cmd) { input.value = ""; autosize(); history.push(text); cmd.run(rest.join(" ")); return; }
  }
  history.push(text); histPos = history.length;
  input.value = ""; autosize(); hideMenu();
  const r = await api("/api/send", { text });
  if (r.error) keepBottom(() => addNotice(r.error === "busy" ? "Forge is still working. Press Stop first." : r.error, "error"));
}

let histPos = 0;
input.addEventListener("input", () => { autosize(); updateMenu(); });
input.addEventListener("keydown", (e) => {
  if (!menu.hidden && menuItems.length) {
    if (e.key === "ArrowDown") { e.preventDefault(); return moveMenu(1); }
    if (e.key === "ArrowUp") { e.preventDefault(); return moveMenu(-1); }
    if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) { e.preventDefault(); return pick(menuSel); }
    if (e.key === "Escape") { e.preventDefault(); return hideMenu(); }
  }
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); return submit(); }
  if (e.key === "Tab" && e.shiftKey) { e.preventDefault(); const modes = ["auto", "ask", "plan"]; return void command("mode", modes[(modes.indexOf(S.mode) + 1) % 3]); }
  if (e.key === "ArrowUp" && input.selectionStart === 0 && !input.value.includes("\n") && history.length) { e.preventDefault(); histPos = Math.max(0, histPos - 1); input.value = history[histPos]; autosize(); }
  if (e.key === "ArrowDown" && histPos < history.length && !input.value.includes("\n")) { e.preventDefault(); histPos++; input.value = history[histPos] || ""; autosize(); }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && S.busy && menu.hidden) api("/api/stop", {});
  if (e.ctrlKey && e.key.toLowerCase() === "n") { e.preventDefault(); command("new"); }
});

$("#send").onclick = () => (S.busy ? api("/api/stop", {}) : submit());
$("#newChat").onclick = () => command("new");
$("#modeBtn").onclick = modeMenu;
$("#modelBtn").onclick = modelMenu;
$("#folderBtn").onclick = folderMenu;
$("#memBtn").onclick = memoryDialog;
$("#attach").onclick = () => { input.focus(); input.value += (input.value && !input.value.endsWith(" ") ? " " : "") + "@"; autosize(); updateMenu(); };
$("#toggleSide").onclick = () => $("#side").classList.toggle("collapsed");

/* ------------------------------------------------------------- boot */
(async () => {
  Object.assign(S, await api("/api/state"));
  renderState();
  showWelcome();
  const es = new EventSource("/api/events?t=" + encodeURIComponent(T));
  es.onmessage = (m) => handle(JSON.parse(m.data));
  es.onerror = () => { S.ollama = false; $("#dot").className = "dot"; $("#dot").title = "Connection to Forge lost"; };
  es.onopen = () => api("/api/state").then((s) => { Object.assign(S, s); renderState(); });
  input.focus();
})();
