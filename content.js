// Video Grabber — content-скрипт.
// Собирает ссылки из тегов <video> и <source> прямо со страницы
// (то, что не всегда видно в сетевых запросах).

function collectFromPage() {
  const items = [];

  const videos = document.querySelectorAll("video");
  for (const v of videos) {
    const poster = v.poster || "";
    if (v.currentSrc && !v.currentSrc.startsWith("blob:")) {
      items.push({ url: v.currentSrc, kind: "video", poster });
    }
    if (v.src && !v.src.startsWith("blob:")) {
      items.push({ url: v.src, kind: "video", poster });
    }
    const sources = v.querySelectorAll("source");
    for (const s of sources) {
      if (s.src && !s.src.startsWith("blob:")) {
        items.push({ url: s.src, kind: "video", poster });
      }
    }
  }

  // Отдельные <audio> тоже прихватим.
  const audios = document.querySelectorAll("audio");
  for (const a of audios) {
    if (a.currentSrc && !a.currentSrc.startsWith("blob:")) items.push({ url: a.currentSrc, kind: "audio" });
    if (a.src && !a.src.startsWith("blob:")) items.push({ url: a.src, kind: "audio" });
    for (const s of a.querySelectorAll("source")) {
      if (s.src && !s.src.startsWith("blob:")) items.push({ url: s.src, kind: "audio" });
    }
  }

  if (items.length) {
    chrome.runtime.sendMessage({ type: "ADD_FROM_PAGE", items });
  }
}

// Первый проход и повторы — сайты часто подгружают плеер с задержкой.
collectFromPage();
setTimeout(collectFromPage, 1500);
setTimeout(collectFromPage, 4000);

// Следим за появлением новых видео (SPA, ленты).
const observer = new MutationObserver(() => {
  clearTimeout(window.__vgTimer);
  window.__vgTimer = setTimeout(collectFromPage, 800);
});
observer.observe(document.documentElement, { childList: true, subtree: true });

// ---------- мост к page_extractor.js (MAIN-мир) ----------
// Писать в window может любой скрипт страницы, поэтому из сообщений берём
// только http(s)-ссылки, известные виды и строки ограниченной длины.

const str = (v, max) => (typeof v === "string" || typeof v === "number" ? String(v).slice(0, max) : "");

// Фоновый воркер Chrome усыпляет, и собранное в его памяти пропадает, а экстрактор
// шлёт ссылки один раз. Поэтому помним их здесь и досылаем, когда открывают окно.
const extracted = new Map();
let pageMeta = null;

window.addEventListener("message", (event) => {
  if (event.source !== window || !event.data) return;
  const data = event.data;

  if (data.type === "VG_EXTRACTED_STREAMS" && Array.isArray(data.items)) {
    const items = data.items
      .filter((it) => it && typeof it.url === "string" && /^https?:\/\//i.test(it.url))
      .map((it) => ({
        url: it.url,
        kind: ["video", "audio", "hls", "dash"].includes(it.kind) ? it.kind : "video",
        filename: str(it.filename, 200),
        quality: Number(it.quality) || 0,
        translator: str(it.translator, 20),
        extracted: true,
      }));
    if (!items.length) return;
    for (const it of items) extracted.set(it.url, it);
    chrome.runtime.sendMessage({ type: "ADD_FROM_PAGE", items });
  }

  // Список озвучек со страницы: попап показывает его для выбора.
  if (data.type === "VG_PAGE_META" && data.meta && Array.isArray(data.meta.translators)) {
    pageMeta = {
      translators: data.meta.translators.slice(0, 100).map((x) => ({ id: str(x.id, 20), name: str(x.name, 120) })),
      current: str(data.meta.current, 20),
      series: !!data.meta.series,
    };
    chrome.runtime.sendMessage({ type: "SET_PAGE_META", meta: pageMeta });
  }

  if (data.type === "VG_DUB_FAILED") {
    chrome.runtime.sendMessage({ type: "DUB_FAILED", translator: str(data.translator, 20), message: str(data.message, 300) });
  }
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (window !== window.top) return;

  if (msg.type === "VG_RESEND") {
    const jobs = [];
    if (pageMeta) jobs.push(chrome.runtime.sendMessage({ type: "SET_PAGE_META", meta: pageMeta }));
    if (extracted.size) jobs.push(chrome.runtime.sendMessage({ type: "ADD_FROM_PAGE", items: [...extracted.values()] }));
    Promise.allSettled(jobs).then(() => sendResponse({ ok: true }));
    return true;
  }

  // Попап выбрал озвучку: просим экстрактор достать ссылки на неё.
  if (msg.type === "VG_FETCH_DUB") {
    window.postMessage({ type: "VG_FETCH_DUB", translator: String(msg.translator) }, "*");
    sendResponse({ ok: true });
  }
});

// Экстрактор мог отработать раньше нас — просим его повторить всё, что он нашёл.
if (window === window.top) window.postMessage({ type: "VG_HELLO" }, "*");

