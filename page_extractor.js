// Video Grabber — page_extractor.js
// Работает в MAIN-мире страницы (content.js — в ISOLATED), поэтому может
// перехватывать запросы самого плеера. Найденное отдаёт в content.js через postMessage.

(function () {
  // Плеер HDRezka (CDNPlayer). Зеркал у сайта много, поэтому узнаём его
  // не только по адресу, но и по вызову инициализации плеера в разметке.
  const html = document.documentElement.outerHTML;
  const isRezka = /rezka/i.test(location.hostname) || /sof\.tv\.initCDN\w*Events\(/.test(html);
  if (!isRezka) return;

  // Всё отправленное помним: content.js мог подняться позже нас и попросит повторить.
  const sent = new Map();
  let meta = null;
  function send(items) {
    for (const it of items) sent.set(it.url, it);
    if (items.length) window.postMessage({ type: "VG_EXTRACTED_STREAMS", items }, "*");
  }
  function sendMeta() {
    if (meta) window.postMessage({ type: "VG_PAGE_META", meta }, "*");
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

  // --- Конфиг плеера в разметке ---
  // sof.tv.initCDNMoviesEvents(id, translator, ..., {"streams":"#h..."});
  // sof.tv.initCDNSeriesEvents(id, translator, season, episode, ..., {...});
  // Количество аргументов перед JSON не фиксируем: сайт его меняет.
  const call = html.match(/sof\.tv\.initCDN(Movies|Series)Events\(([^{]*)\{/);
  const args = call ? call[2].split(",").map((a) => a.trim().replace(/^'|'$/g, "")) : [];
  const isSeries = !!call && call[1] === "Series";
  const postId = args[0] || "";
  const initialTranslator = args[1] || "";

  // --- Озвучки ---
  // Сайт помнит последнюю выбранную озвучку и в разметку кладёт ссылки именно на неё.
  // Поэтому список озвучек отдаём попапу: там выбирают нужную, по умолчанию первую.
  function readTranslators() {
    return [...document.querySelectorAll(".b-translator__item")]
      .map((li) => ({
        id: li.dataset.translator_id || "",
        name: (li.getAttribute("title") || li.textContent || "").trim(),
        camrip: li.dataset.camrip || "0",
        ads: li.dataset.ads || "0",
        director: li.dataset.director || "0",
      }))
      .filter((x) => x.id);
  }
  const translators = readTranslators();

  function dubName(id) {
    const tr = translators.find((x) => x.id === String(id));
    return tr ? tr.name : "";
  }

  // Имя озвучки попадает в имя файла, только когда их на выбор несколько.
  function label(translatorId, season, episode) {
    const dub = translators.length > 1 ? dubName(translatorId) : "";
    return pageTitle() + episodeTag(season, episode) + (dub ? ` [${dub}]` : "");
  }

  // "[360p]https://a/360.mp4:hls:manifest.m3u8 or https://b/360.mp4,[720p]..."
  function parseStreams(decoded, name, translatorId) {
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
        items.push({
          url, kind, quality: rank,
          translator: String(translatorId || ""),
          filename: `${name} [${quality}]${mirror}`,
        });
      }
    }
    return items;
  }

  function extractInitial() {
    if (!call) return [];
    const sm = html.slice(call.index).match(/"streams"\s*:\s*"((?:[^"\\]|\\.)*)"/);
    if (!sm) return [];
    const season = isSeries ? args[2] : "", episode = isSeries ? args[3] : "";
    return parseStreams(decodeStreams(JSON.parse(`"${sm[1]}"`)), label(initialTranslator, season, episode), initialTranslator);
  }

  // Серия, открытая сейчас: активный пункт списка серий, иначе та, с которой открылась страница.
  function currentEpisode() {
    const active = document.querySelector(".b-simple_episode__item.active");
    if (active && active.dataset.season_id && active.dataset.episode_id) {
      return { season: active.dataset.season_id, episode: active.dataset.episode_id };
    }
    return args[2] && args[3] ? { season: args[2], episode: args[3] } : null;
  }

  // Ссылки на другую озвучку — тем же запросом, которым их берёт сам плеер.
  async function fetchDub(translatorId) {
    const tr = translators.find((x) => x.id === String(translatorId)) || { id: String(translatorId) };
    const favs = document.getElementById("ctrl_favs");
    const body = new URLSearchParams({ id: postId, translator_id: tr.id, favs: favs ? favs.value : "" });
    let ep = null;
    if (isSeries) {
      ep = currentEpisode();
      if (!ep) return;
      body.set("season", ep.season);
      body.set("episode", ep.episode);
      body.set("action", "get_stream");
    } else {
      body.set("is_camrip", tr.camrip || "0");
      body.set("is_ads", tr.ads || "0");
      body.set("is_director", tr.director || "0");
      body.set("action", "get_movie");
    }
    const resp = await fetch(`/ajax/get_cdn_series/?t=${Date.now()}`, {
      method: "POST",
      body,
      credentials: "include",
      headers: { "X-Requested-With": "XMLHttpRequest" },
    });
    const data = await resp.json();
    if (!data || !data.success || !data.url) {
      window.postMessage({ type: "VG_DUB_FAILED", translator: tr.id, message: (data && data.message) || "" }, "*");
      return;
    }
    send(parseStreams(decodeStreams(data.url), label(tr.id, ep && ep.season, ep && ep.episode), tr.id));
  }

  // Попап просит ссылки на выбранную озвучку (через content.js).
  window.addEventListener("message", (event) => {
    if (event.source !== window || !event.data) return;
    if (event.data.type === "VG_HELLO") {
      sendMeta();
      send([...sent.values()]);
      return;
    }
    if (event.data.type !== "VG_FETCH_DUB") return;
    fetchDub(event.data.translator).catch((e) => {
      window.postMessage({ type: "VG_DUB_FAILED", translator: String(event.data.translator), message: String(e) }, "*");
    });
  });

  // --- Переключение серии/озвучки на самом сайте: плеер ходит в /ajax/get_cdn_series/ ---
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
          const tr = params.get("translator_id") || initialTranslator;
          send(parseStreams(decodeStreams(data.url), label(tr, params.get("season"), params.get("episode")), tr));
        } catch (e) {
          /* не JSON или чужой формат — пропускаем */
        }
      });
    }
    return origSend.apply(this, arguments);
  };

  meta = {
    translators: translators.map(({ id, name }) => ({ id, name })),
    current: initialTranslator,
    series: isSeries,
  };
  sendMeta();

  try {
    send(extractInitial());
  } catch (e) {
    console.warn("[VideoGrabber] HDRezka extract error:", e);
  }
})();
