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

// Ссылки от page_extractor.js (MAIN-мир). Написать такое сообщение может любой
// скрипт страницы, поэтому пропускаем только http(s)-ссылки и известные виды.
window.addEventListener("message", (event) => {
  if (event.source !== window || !event.data || event.data.type !== "VG_EXTRACTED_STREAMS") return;
  if (!Array.isArray(event.data.items)) return;
  const items = event.data.items
    .filter((it) => it && typeof it.url === "string" && /^https?:\/\//i.test(it.url))
    .map((it) => ({
      url: it.url,
      kind: ["video", "audio", "hls", "dash"].includes(it.kind) ? it.kind : "video",
      filename: typeof it.filename === "string" ? it.filename.slice(0, 200) : "",
      quality: Number(it.quality) || 0,
      extracted: true,
    }));
  if (!items.length) return;
  for (const it of items) extracted.set(it.url, it);
  chrome.runtime.sendMessage({ type: "ADD_FROM_PAGE", items });
});

// Фоновый воркер Chrome усыпляет, и собранное в его памяти пропадает, а экстрактор
// шлёт ссылки один раз. Поэтому помним их здесь и досылаем, когда открывают окно.
const extracted = new Map();
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type !== "VG_RESEND" || window !== window.top) return;
  if (!extracted.size) {
    sendResponse({ ok: true });
    return;
  }
  chrome.runtime
    .sendMessage({ type: "ADD_FROM_PAGE", items: Array.from(extracted.values()) })
    .finally(() => sendResponse({ ok: true }));
  return true;
});
