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
