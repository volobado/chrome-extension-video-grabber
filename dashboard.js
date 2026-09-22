// Video Grabber — дашборд загрузок: всё, что скачано, качается и ждёт очереди.
// Данные берём у службы загрузок раз в секунду; строки обновляем на месте, а не
// перерисовываем, чтобы кнопки не прыгали под курсором и не сбрасывался открытый журнал.

let filter = "all";
let query = "";
let tasks = [];
const openLogs = new Set();   // id задач с раскрытым журналом
const rows = new Map();       // id -> элемент строки
const groups = new Map();     // ключ группы -> заголовок

// ---------- форматирование ----------

// yt-dlp пишет размеры как 1.23GiB и скорость как 5.20MiB/s.
const UNIT_POW = { B: 0, KiB: 1, MiB: 2, GiB: 3, TiB: 4, KB: 1, MB: 2, GB: 3 };

function parseBytes(s) {
  const m = /([\d.]+)\s*([KMGT]?i?B)/.exec(s || "");
  if (!m) return 0;
  return parseFloat(m[1]) * Math.pow(1024, UNIT_POW[m[2]] ?? 0);
}

function humanBytes(n) {
  if (!n) return "";
  const u = [t("unitB"), t("unitKB"), t("unitMB"), t("unitGB")];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}

function humanSize(s) {
  return humanBytes(parseBytes(s)) || s || "";
}

// «12 МБ/с» по-русски и «12 MB/s» по-английски.
const perSec = (size) => `${size}/${t("unitSec")}`;

function humanDuration(sec) {
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h) return `${h} ${t("unitHour")} ${m} ${t("unitMin")}`;
  if (m) return `${m} ${t("unitMin")} ${s} ${t("unitSec")}`;
  return `${s} ${t("unitSec")}`;
}

const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const dateFmt = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "long", year: "numeric" });

function dayKey(ts) {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

function dayLabel(ts) {
  const now = new Date();
  const today = dayKey(now.getTime() / 1000);
  const yest = dayKey(now.getTime() / 1000 - 86400);
  const k = dayKey(ts);
  if (k === today) return t("grpToday");
  if (k === yest) return t("grpYesterday");
  return dateFmt.format(new Date(ts * 1000));
}

function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return ""; }
}

function taskName(task) {
  const name = task.file || task.name || task.title || task.url;
  return task.index ? `${String(task.index).padStart(2, "0")}. ${name.replace(/^\d+ - /, "")}` : name;
}

// ---------- строка задачи ----------

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function makeRow(task) {
  const row = el("div", "row");
  row.dataset.id = task.id;
  row.innerHTML = `
    <div class="st"></div>
    <div class="body">
      <div class="name"></div>
      <div class="sub"></div>
      <div class="prog" hidden><div class="bar"><div class="fill"></div></div><div class="prog-text"></div></div>
      <div class="msg" hidden></div>
      <pre class="log" hidden></pre>
    </div>
    <div class="acts"></div>`;
  row.querySelector(".name").onclick = () => {
    const task = tasks.find((x) => x.id === Number(row.dataset.id));
    if (task && task.status === "done") host.send({ cmd: "reveal", id: task.id }).catch(() => {});
  };
  return row;
}

function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

function updateRow(row, task, queuePos) {
  row.className = `row ${task.status}`;

  // значок состояния
  const st = row.querySelector(".st");
  const icon = { queued: "…", paused: "❚❚", done: "✓", error: "!", canceled: "–" }[task.status];
  if (task.status === "running") {
    if (!st.querySelector(".spin")) { st.textContent = ""; st.append(el("div", "spin")); }
  } else {
    setText(st, icon || "");
  }

  const name = row.querySelector(".name");
  setText(name, (task.kind === "playlist" ? "🗂 " : "") + taskName(task));
  name.title = task.status === "done" ? t("actReveal") : task.url;

  // вторая строка: сайт · папка · время
  const parts = [hostOf(task.url)];
  if (task.playlist) parts.push(task.playlist);
  if (task.status === "queued") parts.push(t("queuePos", String(queuePos)));
  if (task.status === "done") {
    if (task.total) parts.push(humanSize(task.total));
    if (task.finished) parts.push(timeFmt.format(new Date(task.finished * 1000)));
    if (task.started && task.finished) parts.push(t("tookTime", humanDuration(task.finished - task.started)));
  }
  if (task.status === "error" || task.status === "canceled") {
    if (task.finished) parts.push(timeFmt.format(new Date(task.finished * 1000)));
  }
  parts.push(task.dir);
  setText(row.querySelector(".sub"), parts.filter(Boolean).join("  ·  "));
  row.querySelector(".sub").title = task.url;

  // прогресс
  const prog = row.querySelector(".prog");
  const paused = task.status === "paused";
  prog.hidden = task.status !== "running" && !(paused && task.progress);
  if (paused && task.progress) {
    row.querySelector(".fill").style.width = `${Math.min(100, task.progress)}%`;
    setText(row.querySelector(".prog-text"), [t("stPaused"), `${task.progress.toFixed(1)}%`,
      task.total ? humanSize(task.total) : ""].filter(Boolean).join("  ·  "));
  }
  if (task.status === "running") {
    const pct = Math.max(1, Math.min(100, task.progress || 0));
    row.querySelector(".fill").style.width = `${pct}%`;
    let text;
    if (!task.progress && !task.total) {
      text = task.line && !task.line.startsWith("запуск") ? task.line : t("startingNow");
    } else if (task.progress >= 99 && task.line && !task.line.includes("%")) {
      text = task.line;          // склейка видео и звука, извлечение звука
    } else {
      text = [`${(task.progress || 0).toFixed(1)}%`,
              task.total ? humanSize(task.total) : "",
              task.speed ? perSec(humanSize(task.speed.replace("/s", ""))) : "",
              task.eta ? t("etaLeft", task.eta) : ""].filter(Boolean).join("  ·  ");
    }
    setText(row.querySelector(".prog-text"), text);
  }

  // ошибка
  const msg = row.querySelector(".msg");
  msg.hidden = task.status !== "error";
  if (task.status === "error") setText(msg, task.message || t("stError"));

  // журнал yt-dlp
  const log = row.querySelector(".log");
  const logText = (task.log || []).join("\n");
  log.hidden = !openLogs.has(task.id) || !logText;
  if (!log.hidden) setText(log, logText);

  renderActions(row, task, !!logText);
}

// Эмодзи корзины в тёмной теме почти не видно — рисуем свою.
function trashIcon() {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "14");
  svg.setAttribute("height", "14");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  for (const d of ["M3 6h18", "M8 6V4h8v2", "M19 6l-1 14H6L5 6", "M10 11v6", "M14 11v6"]) {
    const p = document.createElementNS(ns, "path");
    p.setAttribute("d", d);
    svg.append(p);
  }
  return svg;
}

function renderActions(row, task, hasLog) {
  const acts = row.querySelector(".acts");
  // Набор кнопок зависит только от состояния: пересобираем, лишь когда оно поменялось.
  const sig = `${task.status}|${hasLog}|${openLogs.has(task.id)}`;
  if (acts.dataset.sig === sig) return;
  acts.dataset.sig = sig;
  acts.innerHTML = "";

  const add = (label, title, cls, fn) => {
    const b = el("button", `act ${cls || ""}`);
    if (label instanceof Node) b.append(label); else b.textContent = label;
    if (title) b.title = title;
    b.onclick = async (e) => {
      e.stopPropagation();
      b.disabled = true;
      try { await fn(b); } finally { b.disabled = false; }
      refresh();
    };
    acts.append(b);
  };

  if (task.status === "done") {
    add(t("actReveal"), "", "primary", () => host.send({ cmd: "reveal", id: task.id }));
  }
  if (task.status === "error" || task.status === "canceled") {
    add("↻ " + t("retryTask"), "", "primary", () => retryTask(task));
  }
  if (task.status === "paused") {
    add("▶ " + t("resumeTask"), "", "primary", () => host.send({ cmd: "resume", id: task.id }));
  }
  if (task.status === "queued" || task.status === "running") {
    add("⏸", t("pauseTask"), "icon", () => host.send({ cmd: "pause", id: task.id }));
  }
  if (hasLog && task.status !== "queued") {
    add(t("actLog"), "", "", () => {
      if (openLogs.has(task.id)) openLogs.delete(task.id); else openLogs.add(task.id);
    });
  }
  add("⧉", t("actCopy"), "icon", async (b) => {
    await navigator.clipboard.writeText(task.url);
    b.title = t("actCopied");
  });
  if (["queued", "running", "paused"].includes(task.status)) {
    add("✕", t("cancelTask"), "icon danger", () => host.send({ cmd: "cancel", id: task.id }));
  } else {
    add(trashIcon(), t("actRemove"), "icon danger", () => host.send({ cmd: "remove", id: task.id }));
  }
}

// ---------- список ----------

function matches(task) {
  if (filter !== "all" && task.status !== filter) return false;
  if (!query) return true;
  const hay = `${taskName(task)} ${task.title} ${task.url} ${task.dir} ${task.playlist}`.toLowerCase();
  return hay.includes(query);
}

function groupHeader(key, label) {
  let h = groups.get(key);
  if (!h) {
    h = el("div", "group");
    groups.set(key, h);
  }
  setText(h, label);
  return h;
}

function renderList() {
  const list = document.getElementById("list");

  const running = tasks.filter((x) => x.status === "running").sort((a, b) => a.id - b.id);
  const queued = tasks.filter((x) => x.status === "queued").sort((a, b) => a.id - b.id);
  const paused = tasks.filter((x) => x.status === "paused").sort((a, b) => a.id - b.id);
  const finished = tasks
    .filter((x) => !["running", "queued", "paused"].includes(x.status))
    .sort((a, b) => (b.finished || b.added) - (a.finished || a.added) || b.id - a.id);

  const queuePos = new Map(queued.map((x, i) => [x.id, i + 1]));

  // Порядок на странице: идущие, очередь, завершённые по дням (свежие сверху).
  const order = [];
  const push = (key, label, items) => {
    const shown = items.filter(matches);
    if (!shown.length) return;
    order.push(groupHeader(key, `${label}  ·  ${shown.length}`));
    for (const task of shown) {
      let row = rows.get(task.id);
      if (!row) { row = makeRow(task); rows.set(task.id, row); }
      updateRow(row, task, queuePos.get(task.id));
      order.push(row);
    }
  };
  push("running", t("grpRunning"), running);
  push("queued", t("grpQueued"), queued);
  push("paused", t("grpPaused"), paused);
  const byDay = new Map();
  for (const task of finished) {
    const k = dayKey(task.finished || task.added);
    if (!byDay.has(k)) byDay.set(k, { label: dayLabel(task.finished || task.added), items: [] });
    byDay.get(k).items.push(task);
  }
  for (const [k, g] of byDay) push(`day-${k}`, g.label, g.items);

  // Сверяем DOM с нужным порядком: лишнее убираем, недостающее ставим на место.
  const keep = new Set(order);
  for (const child of [...list.children]) if (!keep.has(child)) child.remove();
  order.forEach((node, i) => {
    if (list.children[i] !== node) list.insertBefore(node, list.children[i] || null);
  });

  // Удалённые задачи больше не держим в памяти.
  const ids = new Set(tasks.map((x) => x.id));
  for (const id of rows.keys()) if (!ids.has(id)) { rows.delete(id); openLogs.delete(id); }

  const empty = document.getElementById("empty");
  empty.hidden = order.length > 0;
  empty.textContent = tasks.length ? t("dashNothing") : t("dashEmpty");
}

// ---------- сводка ----------

function renderStats(st) {
  const count = (s) => tasks.filter((x) => x.status === s).length;
  const n = {
    running: count("running"), queued: count("queued"), paused: count("paused"), done: count("done"),
    error: count("error"), canceled: count("canceled"),
  };
  for (const [k, id] of [["running", "nRunning"], ["queued", "nQueued"], ["paused", "nPaused"], ["done", "nDone"],
                         ["error", "nError"], ["canceled", "nCanceled"]]) {
    const node = document.getElementById(id);
    setText(node, String(n[k]));
    node.parentElement.classList.toggle("zero", n[k] === 0);
    node.parentElement.classList.toggle("active", filter === k);
  }
  setText(document.getElementById("parallelInfo"), t("dashParallel", String(st.parallel || 5)));

  for (const chip of document.querySelectorAll(".chip")) {
    chip.classList.toggle("active", chip.dataset.filter === filter);
  }

  // Общий прогресс: плейлисты-обёртки и отменённые не считаем.
  const real = tasks.filter((x) => x.kind !== "playlist" && x.status !== "canceled");
  const doneN = real.filter((x) => x.status === "done").length;
  const partial = real.filter((x) => x.status === "running" || x.status === "paused")
    .reduce((s, x) => s + (x.progress || 0) / 100, 0);
  const pct = real.length ? ((doneN + partial) / real.length) * 100 : 0;
  document.getElementById("overallFill").style.width = `${pct}%`;
  setText(document.getElementById("overallText"), t("dashOverall", String(doneN), String(real.length)));

  const speed = tasks.filter((x) => x.status === "running")
    .reduce((s, x) => s + parseBytes((x.speed || "").replace("/s", "")), 0);
  setText(document.getElementById("speedText"), speed ? t("dashSpeed", perSec(humanBytes(speed))) : "");

  const banner = document.getElementById("errBanner");
  banner.hidden = n.error === 0;
  if (n.error) setText(document.getElementById("errBannerText"), t("errBanner", String(n.error)));

  document.title = (n.running ? `(${n.running}) ` : "") + `${t("dashTitle")} · Video Grabber`;

  const dir = (st.settings && st.settings.dir) || "";
  setText(document.getElementById("dirPath"), dir);

  const dot = document.getElementById("hostDot");
  const state = document.getElementById("hostState");
  const tools = st.tools || {};
  if (!tools.ytdlp) { dot.className = "dot err"; setText(state, t("hostNoYtdlp")); }
  else if (!tools.ffmpeg) { dot.className = "dot err"; setText(state, t("hostNoFfmpeg")); }
  else if (n.running || n.queued) { dot.className = "dot busy"; setText(state, t("hostBusy", String(n.running + n.queued))); }
  else if (n.paused) { dot.className = "dot ok"; setText(state, t("hostPaused", String(n.paused))); }
  else { dot.className = "dot ok"; setText(state, t("hostReady")); }

  document.getElementById("pauseAll").disabled = !(n.running || n.queued);
  document.getElementById("resumeAll").disabled = !n.paused;
}

// ---------- опрос службы ----------

let busy = false;
async function refresh() {
  if (busy) return;
  busy = true;
  try {
    const st = await host.send({ cmd: "status" });
    if (st && st.ok) {
      tasks = st.tasks || [];
      renderStats(st);
      renderList();
    }
  } catch (e) {
    document.getElementById("hostDot").className = "dot err";
    setText(document.getElementById("hostState"), t("dashHostDown", e.message));
  } finally {
    busy = false;
  }
}

async function retryAllFailed() {
  const failed = tasks.filter((x) => x.status === "error").sort((a, b) => a.id - b.id);
  for (const task of failed) await retryTask(task).catch(() => {});
  refresh();
}

function setFilter(f) {
  filter = filter === f && f !== "all" ? "all" : f;
  refresh();
}

function init() {
  applyI18n();
  for (const b of document.querySelectorAll("[data-filter]")) {
    b.onclick = () => setFilter(b.dataset.filter);
  }
  const search = document.getElementById("search");
  search.oninput = () => { query = search.value.trim().toLowerCase(); renderList(); };

  document.getElementById("openDir").onclick = () => host.send({ cmd: "open_dir" }).catch(() => {});
  document.getElementById("retryFailed").onclick = retryAllFailed;
  document.getElementById("retryFailed2").onclick = retryAllFailed;
  document.getElementById("cancelAll").onclick = async () => {
    await host.send({ cmd: "cancel_all" }).catch(() => {});
    refresh();
  };
  document.getElementById("clearDone").onclick = async () => {
    await host.send({ cmd: "clear_done" }).catch(() => {});
    refresh();
  };
  document.getElementById("pauseAll").onclick = async () => {
    await host.send({ cmd: "pause_all" }).catch(() => {});
    refresh();
  };
  document.getElementById("resumeAll").onclick = async () => {
    await host.send({ cmd: "resume_all" }).catch(() => {});
    refresh();
  };

  refresh();
  setInterval(refresh, 1000);
}

init();
