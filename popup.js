// Video Grabber — логика попапа.

const HOST_NAME = "com.videograbber.ytdlp";

let currentTab = null;

// Короткий доступ к переводам.
const t = (key, ...args) => chrome.i18n.getMessage(key, args.length ? args : undefined) || key;

// Проставляем переводы в разметку по data-атрибутам.
function applyI18n() {
  for (const el of document.querySelectorAll("[data-i18n]")) {
    el.textContent = t(el.dataset.i18n);
  }
  for (const el of document.querySelectorAll("[data-i18n-title]")) {
    el.title = t(el.dataset.i18nTitle);
  }
}

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

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

// ---------- Блок прямых медиа ----------

async function loadMedia() {
  const list = document.getElementById("mediaList");
  const empty = document.getElementById("empty");
  list.innerHTML = "";

  const resp = await chrome.runtime.sendMessage({ type: "GET_MEDIA", tabId: currentTab.id });
  const media = (resp && resp.media) || [];

  // Видео/аудио вперёд, стримы вниз.
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
    info.textContent = [humanSize(item.size), t(item.source === "page" ? "fromPlayer" : "fromNetwork")]
      .filter(Boolean).join(" · ");
    meta.append(name, info);

    const btn = document.createElement("button");
    btn.className = "btn small";

    if (item.kind === "hls" || item.kind === "dash") {
      // Стримы одной кнопкой не качаются — отдаём в yt-dlp.
      btn.textContent = "yt-dlp";
      btn.title = t("downloadStream");
      btn.onclick = () => runYtdlp(item.url, "best", btn);
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

// ---------- Блок yt-dlp (нативный хост) ----------

function setStatus(text, cls) {
  const el = document.getElementById("ytdlpStatus");
  el.textContent = text || "";
  el.className = "status" + (cls ? " " + cls : "");
}

async function runYtdlp(url, format, btn) {
  setStatus(t("statusStarting"), "work");
  if (btn) btn.disabled = true;

  // Берём куки сайта: без них не скачать видео с ограничением по возрасту
  // и приватные/закрытые ролики.
  let cookies = "";
  try {
    const r = await chrome.runtime.sendMessage({ type: "GET_COOKIES", url });
    cookies = (r && r.cookies) || "";
  } catch {
    /* без кук тоже попробуем */
  }

  let port;
  try {
    port = chrome.runtime.connectNative(HOST_NAME);
  } catch (e) {
    setStatus(t("hostNotInstalled"), "err");
    if (btn) btn.disabled = false;
    return;
  }

  port.onMessage.addListener((msg) => {
    if (msg.status === "progress") {
      setStatus(msg.line || t("statusDownloading"), "work");
    } else if (msg.status === "done") {
      setStatus(t("statusDone", msg.file || t("statusSavedDefault")), "ok");
      if (btn) btn.disabled = false;
    } else if (msg.status === "error") {
      setStatus(t("statusError", msg.message || t("statusSeeConsole")), "err");
      if (btn) btn.disabled = false;
    }
  });

  port.onDisconnect.addListener(() => {
    const err = chrome.runtime.lastError;
    if (err) {
      setStatus(t("hostUnavailable", err.message), "err");
      if (btn) btn.disabled = false;
    }
  });

  port.postMessage({ url, format, cookies });
}

// Проверяем, живой ли нативный хост, для футера.
function checkHost() {
  const state = document.getElementById("hostState");
  let port;
  try {
    port = chrome.runtime.connectNative(HOST_NAME);
  } catch {
    state.textContent = t("hostMissing");
    state.className = "host-state err";
    return;
  }
  let answered = false;
  port.onMessage.addListener((msg) => {
    answered = true;
    if (msg.status === "pong") {
      state.textContent = t("hostReady");
      state.className = "host-state ok";
    }
    port.disconnect();
  });
  port.onDisconnect.addListener(() => {
    if (!answered) {
      state.textContent = t("hostMissing");
      state.className = "host-state err";
    }
  });
  port.postMessage({ ping: true });
}

// ---------- init ----------

async function init() {
  applyI18n();
  currentTab = await getActiveTab();

  const hostEl = document.getElementById("pageHost");
  try {
    hostEl.textContent = new URL(currentTab.url).hostname + new URL(currentTab.url).pathname.slice(0, 30);
  } catch {
    hostEl.textContent = currentTab.url || "";
  }

  document.getElementById("ytdlpBest").onclick = () => {
    const fmt = document.getElementById("ytdlpFormat").value;
    runYtdlp(currentTab.url, fmt, document.getElementById("ytdlpBest"));
  };

  document.getElementById("refresh").onclick = loadMedia;
  document.getElementById("clear").onclick = async () => {
    await chrome.runtime.sendMessage({ type: "CLEAR", tabId: currentTab.id });
    loadMedia();
  };

  loadMedia();
  checkHost();
}

init();
