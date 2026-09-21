// Video Grabber — page_extractor.js
// Работает в MAIN-мире страницы (content.js — в ISOLATED), поэтому может
// перехватывать запросы самого плеера. Найденное отдаёт в content.js через postMessage.

(function () {
  // Плеер HDRezka (CDNPlayer). Зеркал у сайта много, поэтому узнаём его
  // не только по адресу, но и по вызову инициализации плеера в разметке.
  const html = document.documentElement.outerHTML;
  const isRezka = /rezka/i.test(location.hostname) || /sof\.tv\.initCDN\w*Events\(/.test(html);
  if (!isRezka) return;

  function send(items) {
    if (items.length) window.postMessage({ type: "VG_EXTRACTED_STREAMS", items }, "*");
  }

  // --- Расшифровка поля streams ---
  // Сайт отдаёт ссылки строкой "#h" + base64, в которую подмешан мусор:
  // разделители "//_//" и base64 от сочетаний символов @#!^$ длиной 2 и 3.
  const TRASH = (() => {
    const chars = ["@", "#", "!", "^", "$"];
    const two = [], three = [];
    for (const a of chars) for (const b of chars) {
      two.push(btoa(a + b));
      for (const c of chars) three.push(btoa(a + b + c));
    }
    return two.concat(three);
  })();

  function decodeStreams(s) {
    if (!s || !s.startsWith("#h")) return s || "";
    let data = s.slice(2).split("//_//").join("");
    for (const junk of TRASH) data = data.split(junk).join("");
    data = data.replace(/=+$/, "");
    data += "=".repeat((4 - (data.length % 4)) % 4);
    const bytes = Uint8Array.from(atob(data), (ch) => ch.charCodeAt(0));
    return new TextDecoder().decode(bytes);
  }

  function pageTitle() {
    const h1 = document.querySelector(".b-post__title h1");
    return (h1 && h1.textContent.trim()) || document.title || "HDRezka";
  }

  function episodeTag(season, episode) {
    if (!season || !episode) return "";
    return ` S${String(season).padStart(2, "0")}E${String(episode).padStart(2, "0")}`;
  }

  // "[360p]https://a/360.mp4:hls:manifest.m3u8 or https://b/360.mp4,[720p]..."
  function parseStreams(decoded, label) {
    const items = [];
    const re = /\[([^\]]+)\]([^\[]+)/g;
    let m;
    while ((m = re.exec(decoded))) {
      const quality = m[1];
      const urls = m[2].split(" or ").map((u) => u.trim().replace(/,$/, "")).filter((u) => /^https?:\/\//.test(u));
      const seenKind = {};
      for (const url of urls) {
        const kind = /\.m3u8(\?|#|$)/i.test(url) ? "hls" : "video";
        // Зеркал одного вида может быть несколько — номер отличает их в списке.
        seenKind[kind] = (seenKind[kind] || 0) + 1;
        const mirror = seenKind[kind] > 1 ? ` (mirror ${seenKind[kind] - 1})` : "";
        // Число для выбора лучшего качества: "1080p Ultra" выше просто "1080p".
        const rank = (parseInt(quality, 10) || 0) * 10 + (/ultra/i.test(quality) ? 5 : 0);
        items.push({ url, kind, quality: rank, filename: `${label} [${quality}]${mirror}` });
      }
    }
    return items;
  }

  // --- Первый показ: конфиг плеера лежит прямо в разметке ---
  // sof.tv.initCDNMoviesEvents(id, translator, ..., {"streams":"#h..."});
  // sof.tv.initCDNSeriesEvents(id, translator, season, episode, ..., {...});
  // Количество аргументов перед JSON не фиксируем: сайт его меняет.
  function extractInitial() {
    const call = html.match(/sof\.tv\.initCDN(Movies|Series)Events\(([^{]*)\{/);
    if (!call) return [];
    const tail = html.slice(call.index);
    const sm = tail.match(/"streams"\s*:\s*"((?:[^"\\]|\\.)*)"/);
    if (!sm) return [];
    let label = pageTitle();
    if (call[1] === "Series") {
      const args = call[2].split(",").map((a) => a.trim());
      label += episodeTag(args[2], args[3]);
    }
    return parseStreams(decodeStreams(JSON.parse(`"${sm[1]}"`)), label);
  }

  // --- Переключение серии/озвучки: плеер берёт ссылки через /ajax/get_cdn_series/ ---
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__vgUrl = String(url);
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    if (this.__vgUrl && this.__vgUrl.includes("/ajax/get_cdn_series/")) {
      const params = new URLSearchParams(typeof body === "string" ? body : "");
      this.addEventListener("load", () => {
        try {
          const data = JSON.parse(this.responseText);
          if (!data || !data.success || !data.url) return;
          const label = pageTitle() + episodeTag(params.get("season"), params.get("episode"));
          send(parseStreams(decodeStreams(data.url), label));
        } catch (e) {
          /* не JSON или чужой формат — пропускаем */
        }
      });
    }
    return origSend.apply(this, arguments);
  };

  try {
    send(extractInitial());
  } catch (e) {
    console.warn("[VideoGrabber] HDRezka extract error:", e);
  }
})();
