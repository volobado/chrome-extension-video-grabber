// Video Grabber — фоновый сервис-воркер.
// Слушает сетевые запросы и собирает медиа-ссылки по каждой вкладке.

// tabId -> Map(url -> mediaInfo). Держим в памяти воркера.
const mediaByTab = new Map();
// tabId -> { translators, current, series, dubError } — озвучки со страницы плеера.
const metaByTab = new Map();

// Типы контента и расширения, которые считаем видео/аудио.
const MEDIA_EXT = /\.(mp4|m4v|webm|ogv|mov|mkv|avi|flv|m3u8|mpd|ts|mp3|m4a|aac|ogg|wav|flac)(\?|#|$)/i;
const MEDIA_CT = /(video\/|audio\/|application\/(x-mpegurl|vnd\.apple\.mpegurl|dash\+xml|octet-stream))/i;

function getTabStore(tabId) {
  let store = mediaByTab.get(tabId);
  if (!store) {
    store = new Map();
    mediaByTab.set(tabId, store);
  }
  return store;
}

function guessKind(url, contentType) {
  const u = url.toLowerCase();
  if (u.includes(".m3u8") || /mpegurl/i.test(contentType)) return "hls";
  if (u.includes(".mpd") || /dash\+xml/i.test(contentType)) return "dash";
  if (/audio\//i.test(contentType) || /\.(mp3|m4a|aac|ogg|wav|flac)(\?|#|$)/i.test(u)) return "audio";
  return "video";
}

function filenameFromUrl(url) {
  try {
    const path = new URL(url).pathname;
    const name = decodeURIComponent(path.split("/").pop() || "");
    return name || "video";
  } catch {
    return "video";
  }
}

// Имя, подсказанное экстрактором (название фильма + качество), приводим к виду,
// который примет chrome.downloads: без запрещённых в Windows символов и с расширением.
function namedFile(name, url, kind) {
  const base = String(name)
    .replace(/[\\/:*?"<>|\x00-\x1f]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/[. ]+$/, "")
    .slice(0, 150);
  if (!base) return filenameFromUrl(url);
  // Потоки качает yt-dlp, для них имя — только подпись в списке.
  if (kind === "hls" || kind === "dash") return base;
  const ext = (filenameFromUrl(url).match(/\.[a-z0-9]{2,4}$/i) || [".mp4"])[0];
  return base.toLowerCase().endsWith(ext.toLowerCase()) ? base : base + ext;
}

function addMedia(tabId, url, contentType, sizeHeader) {
  if (!tabId || tabId < 0) return;
  if (!url || url.startsWith("blob:") || url.startsWith("data:")) return;

  const isMediaByExt = MEDIA_EXT.test(url);
  const isMediaByCt = contentType && MEDIA_CT.test(contentType);
  if (!isMediaByExt && !isMediaByCt) return;

  const store = getTabStore(tabId);
  if (store.has(url)) {
    // Обновим размер, если раньше не знали.
    if (sizeHeader && !store.get(url).size) store.get(url).size = Number(sizeHeader) || 0;
    return;
  }

  const kind = guessKind(url, contentType || "");
  store.set(url, {
    url,
    kind, // video | audio | hls | dash
    contentType: contentType || "",
    size: Number(sizeHeader) || 0,
    filename: filenameFromUrl(url),
    source: "network",
  });

  updateBadge(tabId);
}

function updateBadge(tabId) {
  const store = mediaByTab.get(tabId);
  const count = store ? store.size : 0;
  const text = count > 0 ? String(count > 99 ? "99+" : count) : "";
  chrome.action.setBadgeText({ tabId, text });
  chrome.action.setBadgeBackgroundColor({ tabId, color: "#e11d48" });
}

// Ловим завершённые запросы — тут доступны заголовки ответа.
chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    const headers = details.responseHeaders || [];
    let contentType = "";
    let contentLength = "";
    for (const h of headers) {
      const n = h.name.toLowerCase();
      if (n === "content-type") contentType = h.value || "";
      else if (n === "content-length") contentLength = h.value || "";
    }
    addMedia(details.tabId, details.url, contentType, contentLength);
  },
  { urls: ["<all_urls>"] },
  ["responseHeaders"]
);

// Подстраховка: некоторые запросы ловим ещё до заголовков (по расширению в URL).
chrome.webRequest.onBeforeRequest.addListener(
  (details) => {
    if (MEDIA_EXT.test(details.url)) addMedia(details.tabId, details.url, "", "");
  },
  { urls: ["<all_urls>"] }
);

// Чистим память вкладки при полной перезагрузке страницы (новый документ).
chrome.webNavigation?.onCommitted?.addListener?.(() => {});
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.status === "loading" && changeInfo.url) {
    mediaByTab.delete(tabId);
    metaByTab.delete(tabId);
    updateBadge(tabId);
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  mediaByTab.delete(tabId);
  metaByTab.delete(tabId);
});

// --- Куки для yt-dlp ---

// Грубое приведение хоста к домену вида "youtube.com".
function registrableDomain(hostname) {
  const parts = hostname.split(".").filter(Boolean);
  if (parts.length <= 2) return hostname;
  // Двухуровневые зоны: co.uk, com.ua, com.br и т.п.
  const twoLevel = /^(co|com|net|org|gov|edu|ac)\.[a-z]{2}$/i;
  const last2 = parts.slice(-2).join(".");
  if (twoLevel.test(last2) && parts.length >= 3) return parts.slice(-3).join(".");
  return last2;
}

function toNetscapeLine(c) {
  const domain = c.domain.startsWith(".") ? c.domain : (c.hostOnly ? c.domain : "." + c.domain);
  const includeSub = domain.startsWith(".") ? "TRUE" : "FALSE";
  const expiry = c.session ? 0 : Math.floor(c.expirationDate || 0);
  return [
    domain,
    includeSub,
    c.path || "/",
    c.secure ? "TRUE" : "FALSE",
    expiry,
    c.name,
    c.value,
  ].join("\t");
}

async function buildCookieFile(pageUrl) {
  let host;
  try {
    host = new URL(pageUrl).hostname;
  } catch {
    return "";
  }

  const domains = new Set([registrableDomain(host)]);
  // Для YouTube нужны ещё и куки аккаунта Google.
  if (/(^|\.)youtube\.com$/i.test(host) || /(^|\.)youtu\.be$/i.test(host)) {
    domains.add("youtube.com");
    domains.add("google.com");
  }

  const all = [];
  for (const d of domains) {
    try {
      const list = await chrome.cookies.getAll({ domain: d });
      all.push(...list);
    } catch {
      /* домен без доступа — пропускаем */
    }
  }

  if (!all.length) return "";

  // Убираем дубли (одно имя может прийти из разных доменов).
  const seen = new Set();
  const lines = ["# Netscape HTTP Cookie File", "# Video Grabber"];
  for (const c of all) {
    const key = `${c.domain}|${c.path}|${c.name}`;
    if (seen.has(key)) continue;
    seen.add(key);
    lines.push(toNetscapeLine(c));
  }
  return lines.join("\n") + "\n";
}

// Сообщения от popup и content-скрипта.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "GET_MEDIA") {
    const tabId = msg.tabId;
    const store = mediaByTab.get(tabId);
    const list = store ? Array.from(store.values()) : [];
    sendResponse({ media: list, meta: metaByTab.get(tabId) || null });
    return true;
  }

  if (msg.type === "SET_PAGE_META") {
    const tabId = sender.tab?.id;
    if (tabId) {
      const old = metaByTab.get(tabId);
      metaByTab.set(tabId, { ...msg.meta, dubError: old ? old.dubError : null });
    }
    sendResponse({ ok: true });
    return true;
  }

  if (msg.type === "DUB_FAILED") {
    // Для этой озвучки сайт ссылок не дал (например, у неё нет открытой серии).
    const meta = metaByTab.get(sender.tab?.id);
    if (meta) meta.dubError = { translator: msg.translator, message: msg.message, at: Date.now() };
    sendResponse({ ok: true });
    return true;
  }

  if (msg.type === "ADD_FROM_PAGE") {
    // Ссылки из content.js (теги <video>/<source>).
    const tabId = sender.tab?.id;
    if (tabId) {
      const store = getTabStore(tabId);
      for (const item of msg.items || []) {
        if (!item.url) continue;
        const known = store.get(item.url);
        if (known) {
          // Ссылку уже поймали в сети, но экстрактор знает про неё больше: имя и качество.
          if (item.extracted && known.source !== "extractor") {
            known.source = "extractor";
            known.quality = Number(item.quality) || 0;
            known.translator = item.translator || "";
            if (item.filename) known.filename = namedFile(item.filename, item.url, item.kind);
          }
          continue;
        }
        if (item.url.startsWith("blob:")) {
          // blob нельзя скачать напрямую — пометим, но покажем.
        }
        store.set(item.url, {
          url: item.url,
          kind: item.kind || "video",
          contentType: "",
          size: 0,
          filename: item.filename ? namedFile(item.filename, item.url, item.kind) : filenameFromUrl(item.url),
          // "extractor" — ссылка вынута из конфига плеера (page_extractor.js).
          source: item.extracted ? "extractor" : "page",
          quality: Number(item.quality) || 0,
          translator: item.translator || "",
          poster: item.poster || "",
        });
      }
      updateBadge(tabId);
    }
    sendResponse({ ok: true });
    return true;
  }

  if (msg.type === "DOWNLOAD") {
    chrome.downloads.download(
      { url: msg.url, filename: msg.filename || undefined, saveAs: !!msg.saveAs },
      (id) => sendResponse({ ok: id !== undefined, id })
    );
    return true;
  }

  if (msg.type === "GET_COOKIES") {
    // Отдаём куки для сайта в формате Netscape — yt-dlp понимает его напрямую.
    // Так обходим блокировку базы кук, пока браузер запущен.
    buildCookieFile(msg.url)
      .then((text) => sendResponse({ cookies: text }))
      .catch(() => sendResponse({ cookies: "" }));
    return true;
  }

  if (msg.type === "CLEAR") {
    mediaByTab.delete(msg.tabId);
    updateBadge(msg.tabId);
    sendResponse({ ok: true });
    return true;
  }
});
