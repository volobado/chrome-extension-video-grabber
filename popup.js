// Video Grabber — логика попапа.
// Качает не попап, а служба загрузок: окно можно закрывать, загрузка не прервётся.

const HOST_NAME = "com.videograbber.ytdlp";
const params = new URLSearchParams(location.search);
const DETACHED = params.get("detached") === "1";

let currentTab = null;
let lastSettings = { dir: "" };
let pollTimer = null;

// Короткий доступ к переводам.
const t = (key, ...args) => chrome.i18n.getMessage(key, args.length ? args : undefined) || key;

function applyI18n() {
  for (const el of document.querySelectorAll("[data-i18n]")) {
    el.textContent = t(el.dataset.i18n);
  }
  for (const el of document.querySelectorAll("[data-i18n-title]")) {
    el.title = t(el.dataset.i18nTitle);
  }
}

// ---------- связь с нативным хостом ----------
// Один порт на всё время жизни окна: каждое подключение поднимает python,
// поэтому опрос статуса по новому порту был бы расточительным.

const host = {
  port: null,
  waiting: [],

  connect() {
    if (this.port) return this.port;
    this.port = chrome.runtime.connectNative(HOST_NAME);
    this.port.onMessage.addListener((msg) => {
      const w = this.waiting.shift();
      if (w) w.resolve(msg);
    });
    this.port.onDisconnect.addListener(() => {
      const err = chrome.runtime.lastError;
      this.port = null;
      const pending = this.waiting.splice(0);
      // Хост уходит сам после каждого ответа только в старых версиях; здесь обрыв
      // с незакрытыми запросами — настоящая ошибка, без запросов — просто закрытие.
      for (const w of pending) w.reject(new Error(err ? err.message : "disconnected"));
    });
    return this.port;
  },

  send(msg) {
    return new Promise((resolve, reject) => {
      let port;
      try {
        port = this.connect();
      } catch (e) {
        reject(new Error(t("hostNotInstalled")));
        return;
      }
      this.waiting.push({ resolve, reject });
      try {
        port.postMessage(msg);
      } catch (e) {
        this.waiting.pop();
        this.port = null;
        reject(e);
      }
    });
  },
};

// ---------- вспомогательное ----------

function humanSize(bytes) {
  if (!bytes) return "";
  const u = [t("unitB"), t("unitKB"), t("unitMB"), t("unitGB")];
  let i = 0, n = bytes;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}

function shortName(item) {
  let name = item.filename || "video";
  if (name.length > 42) name = name.slice(0, 20) + "…" + name.slice(-18);
  return name;
}

function setStatus(text, cls) {
  const el = document.getElementById("ytdlpStatus");
  el.textContent = text || "";
  el.className = "status" + (cls ? " " + cls : "");
}

function playlistId(url) {
  try {
    return new URL(url).searchParams.get("list") || "";
  } catch {
    return "";
  }
}

// Активная вкладка: в откреплённом окне — та, что открыта в обычном окне браузера.
async function resolveTab() {
  if (!DETACHED) {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    return tab;
  }
  const tabs = await chrome.tabs.query({ active: true, windowType: "normal" });
  const wanted = Number(params.get("tabId"));
  return tabs.find((x) => x.id === wanted) || tabs[0] || null;
}

// ---------- прямые медиа ----------

async function loadMedia() {
  const list = document.getElementById("mediaList");
  const empty = document.getElementById("empty");
  list.innerHTML = "";

  if (!currentTab) { empty.style.display = "block"; return; }

  // Просим страницу дослать ссылки плеера: воркер мог уснуть и забыть их.
  // На страницах без content-скрипта (chrome://) запрос падает — это нормально.
  await chrome.tabs.sendMessage(currentTab.id, { type: "VG_RESEND" }, { frameId: 0 }).catch(() => {});

  const resp = await chrome.runtime.sendMessage({ type: "GET_MEDIA", tabId: currentTab.id });
  const media = (resp && resp.media) || [];
  media.sort((a, b) => (b.size || 0) - (a.size || 0));

  if (!media.length) {
    empty.style.display = "block";
    return;
  }
  empty.style.display = "none";

  for (const item of media) {
    const li = document.createElement("li");
    li.className = "item";

    const badge = document.createElement("span");
    badge.className = `badge ${item.kind}`;
    badge.textContent = item.kind;

    const meta = document.createElement("div");
    meta.className = "meta";
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = shortName(item);
    name.title = item.url;
    const info = document.createElement("div");
    info.className = "info";
    info.textContent = [humanSize(item.size), t(item.source === "network" ? "fromNetwork" : "fromPlayer")]
      .filter(Boolean).join(" · ");
    meta.append(name, info);

    const btn = document.createElement("button");
    btn.className = "btn small";

    if (item.kind === "hls" || item.kind === "dash") {
      btn.textContent = "yt-dlp";
      btn.title = t("downloadStream");
      btn.onclick = () => enqueue(
        item.url, document.getElementById("ytdlpFormat").value, false, item.filename,
        item.source === "extractor" ? item.filename : ""
      );
    } else {
      btn.textContent = "⬇";
      btn.title = t("downloadFile");
      btn.onclick = () => {
        chrome.runtime.sendMessage({ type: "DOWNLOAD", url: item.url, filename: item.filename });
        btn.textContent = "✓";
      };
    }

    li.append(badge, meta, btn);
    list.append(li);
  }
}

// Лучшая ссылка из конфига плеера с учётом выбранного потолка качества.
// Прямой mp4 надёжнее потока из кусочков, поэтому при равном качестве берём его.
async function bestExtracted(format) {
  await chrome.tabs.sendMessage(currentTab.id, { type: "VG_RESEND" }, { frameId: 0 }).catch(() => {});
  const resp = await chrome.runtime.sendMessage({ type: "GET_MEDIA", tabId: currentTab.id });
  const found = ((resp && resp.media) || []).filter((m) => m.source === "extractor");
  if (!found.length) return null;
  const cap = { 1080: 10800, 720: 7200, 480: 4800 }[format];
  const fits = cap ? found.filter((m) => m.quality <= cap + 5) : [];
  const pool = fits.length ? fits : found;
  pool.sort((a, b) => (b.quality - a.quality) || ((a.kind === "video" ? 0 : 1) - (b.kind === "video" ? 0 : 1)));
  return pool[0];
}

// ---------- очередь загрузок ----------

// filename — готовое имя файла; без него служба берёт название, которое найдёт yt-dlp.
async function enqueue(url, format, asPlaylist, title, filename) {
  setStatus(t("statusStarting"), "work");

  // Куки нужны для роликов с ограничением по возрасту, приватных и по подписке.
  let cookies = "";
  try {
    const r = await chrome.runtime.sendMessage({ type: "GET_COOKIES", url });
    cookies = (r && r.cookies) || "";
  } catch {
    /* без кук тоже попробуем */
  }

  try {
    const resp = await host.send({
      cmd: "enqueue",
      url,
      format,
      cookies,
      playlist: !!asPlaylist,
      title: title || (currentTab && currentTab.title) || url,
      filename: filename || "",
    });
    if (resp && resp.ok) {
      setStatus(asPlaylist ? t("statusPlaylistQueued") : t("statusQueued"), "ok");
      refreshQueue();
    } else {
      setStatus(t("statusError", (resp && resp.message) || "?"), "err");
    }
  } catch (e) {
    setStatus(t("statusError", e.message), "err");
  }
}

function taskLabel(task) {
  // У файла из плейлиста номер уже в имени — второй раз его добавлять не надо.
  if (task.file) return task.file;
  const name = task.title || task.url;
  return task.index ? `${String(task.index).padStart(2, "0")}. ${name}` : name;
}

function renderQueue(tasks) {
  const list = document.getElementById("queueList");
  const empty = document.getElementById("queueEmpty");
  list.innerHTML = "";

  const shown = tasks.slice().sort((a, b) => b.id - a.id).slice(0, 40);
  empty.style.display = shown.length ? "none" : "block";

  for (const task of shown) {
    const li = document.createElement("li");
    li.className = "item task " + task.status;

    const meta = document.createElement("div");
    meta.className = "meta";

    const name = document.createElement("div");
    name.className = "name";
    name.textContent = (task.kind === "playlist" ? "🗂 " : "") + taskLabel(task);
    name.title = task.url + (task.playlist ? `\n${task.playlist}` : "");

    const info = document.createElement("div");
    info.className = "info";
    if (task.status === "running") info.textContent = task.line || t("statusDownloading");
    else if (task.status === "queued") info.textContent = t("stQueued");
    else if (task.status === "done") {
      // У плейлиста в message лежит число роликов — оно полезнее слова «готово».
      info.textContent = task.kind === "playlist" && task.message ? task.message : t("stDone");
    }
    else if (task.status === "canceled") info.textContent = t("stCanceled");
    else info.textContent = task.message || t("stError");

    meta.append(name, info);

    if (task.status === "running") {
      const bar = document.createElement("div");
      bar.className = "bar";
      const fill = document.createElement("div");
      fill.className = "fill";
      fill.style.width = `${Math.max(2, Math.min(100, task.progress || 0))}%`;
      bar.append(fill);
      meta.append(bar);
    }

    const btn = document.createElement("button");
    btn.className = "link-btn task-btn";
    if (task.status === "queued" || task.status === "running") {
      btn.textContent = "✕";
      btn.title = t("cancelTask");
      btn.onclick = async () => {
        btn.disabled = true;
        await host.send({ cmd: "cancel", id: task.id }).catch(() => {});
        refreshQueue();
      };
    } else if (task.status === "error" || task.status === "canceled") {
      btn.textContent = "↻";
      btn.title = t("retryTask");
      btn.onclick = async () => {
        btn.disabled = true;
        let cookies = "";
        try {
          const r = await chrome.runtime.sendMessage({ type: "GET_COOKIES", url: task.url });
          cookies = (r && r.cookies) || "";
        } catch { /* без кук */ }
        await host.send({
          cmd: "enqueue",
          url: task.url,
          format: task.format,
          cookies,
          title: task.title,
          dir: task.dir,
          index: task.index,
          playlist_title: task.playlist,
          filename: task.name || "",
        }).catch(() => {});
        refreshQueue();
      };
    } else {
      btn.textContent = "✓";
      btn.disabled = true;
    }

    li.append(meta, btn);
    list.append(li);
  }
}

function applySettings(settings) {
  if (!settings) return;
  lastSettings = settings;
  const el = document.getElementById("dirPath");
  el.textContent = settings.dir || "";
  el.title = settings.dir || "";
}

async function refreshQueue() {
  try {
    const st = await host.send({ cmd: "status" });
    if (!st || !st.ok) return;
    renderQueue(st.tasks || []);
    applySettings(st.settings);

    const state = document.getElementById("hostState");
    const tools = st.tools || {};
    if (!tools.ytdlp) {
      state.textContent = t("hostNoYtdlp");
      state.className = "host-state err";
    } else if (!tools.ffmpeg) {
      state.textContent = t("hostNoFfmpeg");
      state.className = "host-state err";
    } else {
      const active = (st.tasks || []).filter((x) => x.status === "running" || x.status === "queued").length;
      state.textContent = active ? t("hostBusy", String(active)) : t("hostReady");
      state.className = "host-state ok";
    }
  } catch (e) {
    const state = document.getElementById("hostState");
    state.textContent = t("hostUnavailable", e.message);
    state.className = "host-state err";
  }
}

// ---------- папка сохранения ----------

async function pickFolder() {
  setStatus(t("pickFolderHint"), "work");
  try {
    await host.send({ cmd: "pick_folder" });
  } catch (e) {
    setStatus(t("statusError", e.message), "err");
  }
}

function toggleDirInput(show) {
  const row = document.getElementById("dirEditRow");
  row.hidden = !show;
  if (show) {
    const input = document.getElementById("dirInput");
    input.value = lastSettings.dir || "";
    input.focus();
    input.select();
  }
}

async function saveTypedDir() {
  const value = document.getElementById("dirInput").value.trim();
  if (!value) return;
  try {
    const resp = await host.send({ cmd: "set_dir", dir: value });
    if (resp && resp.ok) {
      toggleDirInput(false);
      setStatus(t("dirSaved"), "ok");
      refreshQueue();
    } else {
      setStatus(t("statusError", (resp && resp.message) || "?"), "err");
    }
  } catch (e) {
    setStatus(t("statusError", e.message), "err");
  }
}

// ---------- источник ----------

function showSource(tab) {
  const hostEl = document.getElementById("pageHost");
  const plBtn = document.getElementById("ytdlpPlaylist");
  if (!tab) {
    hostEl.textContent = "—";
    plBtn.hidden = true;
    return;
  }
  try {
    const u = new URL(tab.url);
    hostEl.textContent = u.hostname + u.pathname.slice(0, 30);
  } catch {
    hostEl.textContent = tab.url || "";
  }
  plBtn.hidden = !playlistId(tab.url || "");
}

// В откреплённом окне следим за тем, что открыто в браузере.
async function syncSource() {
  const tab = await resolveTab();
  const changed = !currentTab || !tab || tab.id !== currentTab.id || tab.url !== currentTab.url;
  currentTab = tab;
  if (changed) {
    showSource(tab);
    loadMedia();
  }
}

// ---------- init ----------

async function init() {
  applyI18n();
  currentTab = await resolveTab();
  showSource(currentTab);

  document.getElementById("ytdlpBest").onclick = async () => {
    if (!currentTab) return;
    const format = document.getElementById("ytdlpFormat").value;
    // Сайты вроде HDRezka yt-dlp не знает: ему нужна ссылка из плеера, а не адрес страницы.
    const best = await bestExtracted(format);
    if (best) {
      enqueue(best.url, format, false, best.filename, best.filename);
    } else if (/rezka/i.test(currentTab.url || "")) {
      setStatus(t("noPlayerStreams"), "err");
    } else {
      enqueue(currentTab.url, format, false);
    }
  };
  document.getElementById("ytdlpPlaylist").onclick = () => {
    if (!currentTab) return;
    enqueue(currentTab.url, document.getElementById("ytdlpFormat").value, true);
  };

  document.getElementById("pickDir").onclick = pickFolder;
  document.getElementById("typeDir").onclick = () => toggleDirInput(document.getElementById("dirEditRow").hidden);
  document.getElementById("dirSave").onclick = saveTypedDir;
  document.getElementById("dirInput").onkeydown = (e) => { if (e.key === "Enter") saveTypedDir(); };
  document.getElementById("openDir").onclick = () => host.send({ cmd: "open_dir" }).catch(() => {});

  document.getElementById("cancelAll").onclick = async () => {
    await host.send({ cmd: "cancel_all" }).catch(() => {});
    refreshQueue();
  };
  document.getElementById("clearDone").onclick = async () => {
    await host.send({ cmd: "clear_done" }).catch(() => {});
    refreshQueue();
  };

  document.getElementById("refresh").onclick = () => { loadMedia(); refreshQueue(); };
  document.getElementById("clear").onclick = async () => {
    if (!currentTab) return;
    await chrome.runtime.sendMessage({ type: "CLEAR", tabId: currentTab.id });
    loadMedia();
  };

  const detachBtn = document.getElementById("detach");
  if (DETACHED) {
    detachBtn.hidden = true;
    document.body.classList.add("detached");
  } else {
    detachBtn.onclick = async () => {
      const url = chrome.runtime.getURL(
        `popup.html?detached=1&tabId=${currentTab ? currentTab.id : ""}`
      );
      await chrome.windows.create({ url, type: "popup", width: 420, height: 700 });
      window.close();
    };
  }

  loadMedia();
  refreshQueue();

  pollTimer = setInterval(() => {
    refreshQueue();
    if (DETACHED) syncSource();
  }, 1000);
}

window.addEventListener("unload", () => {
  if (pollTimer) clearInterval(pollTimer);
});

init();
