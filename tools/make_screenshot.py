#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Генератор скриншота попапа для README.

Берёт настоящие popup.html/popup.css/popup.js, подставляет вместо chrome.* API
заглушку с демо-данными и снимает окно через headless Chrome.
Так картинка в README всегда соответствует реальному коду.

Запуск:  python tools/make_screenshot.py
Итог:    docs/screenshot.png
"""

import re
import os
import sys
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
OUT = DOCS / "screenshot.png"

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# Заглушка chrome.* — отдаёт демо-данные, но код попапа исполняется настоящий.
MOCK = """
<script>
// Переводы берём из настоящего _locales/en/messages.json (подставляются ниже).
const MESSAGES = __MESSAGES__;

const DEMO_MEDIA = [
  { url: "https://cdn.example.com/lecture-2026-final.mp4", kind: "video",
    size: 428703744, filename: "lecture-2026-final.mp4", source: "network" },
  { url: "https://videos.example.org/interview_4k.webm", kind: "video",
    size: 194837606, filename: "interview_4k.webm", source: "page" },
  { url: "https://stream.example.net/live/master.m3u8", kind: "hls",
    size: 0, filename: "master.m3u8", source: "network" },
  { url: "https://media.example.com/podcast-ep-42.mp3", kind: "audio",
    size: 38273024, filename: "podcast-ep-42.mp3", source: "network" }
];

window.chrome = {
  i18n: {
    getMessage: (key, args) => {
      const entry = MESSAGES[key];
      if (!entry) return "";
      let msg = entry.message;
      if (args && args.length) {
        // Подставляем плейсхолдеры вида $FILE$ / $ERR$ / $MSG$.
        msg = msg.replace(/\\$[A-Z]+\\$/g, () => args.shift() ?? "");
      }
      return msg;
    }
  },
  tabs: {
    query: async () => ([{ id: 1, url: "https://www.youtube.com/watch?v=aqz-KE-bpKQ" }])
  },
  runtime: {
    sendMessage: async (msg) => {
      if (msg.type === "GET_MEDIA") return { media: DEMO_MEDIA };
      if (msg.type === "GET_COOKIES") return { cookies: "" };
      return { ok: true };
    },
    connectNative: () => ({
      onMessage: { addListener: (fn) => { window.__pong = fn; } },
      onDisconnect: { addListener: () => {} },
      postMessage: () => { setTimeout(() => window.__pong &&
        window.__pong({ status: "pong" }), 30); },
      disconnect: () => {}
    }),
    lastError: null
  }
};
</script>
"""


def find_chrome():
    for c in CHROME_CANDIDATES:
        if os.path.isfile(c):
            return c
    for name in ("chrome", "chromium", "msedge"):
        p = shutil.which(name)
        if p:
            return p
    return None


def build_demo_html():
    html = (ROOT / "popup.html").read_text(encoding="utf-8")
    messages = (ROOT / "_locales" / "en" / "messages.json").read_text(encoding="utf-8")
    mock = MOCK.replace("__MESSAGES__", messages)
    # Вставляем заглушку перед подключением popup.js.
    html = html.replace('<script src="popup.js"></script>',
                        mock + '<script src="popup.js"></script>')
    # Абсолютные пути к css/js, чтобы работало из временной папки.
    html = html.replace('href="popup.css"', f'href="file:///{(ROOT / "popup.css").as_posix()}"')
    html = html.replace('src="popup.js"', f'src="file:///{(ROOT / "popup.js").as_posix()}"')
    # Попап в браузере не скроллится — фиксируем высоту под контент.
    html = html.replace("</head>", "<style>body{width:360px}</style></head>")
    return html


def main():
    chrome = find_chrome()
    if not chrome:
        print("Chrome не найден — скриншот не сделать.")
        sys.exit(1)

    DOCS.mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="vg_shot_"))
    demo = tmp / "demo.html"
    demo.write_text(build_demo_html(), encoding="utf-8")

    cmd = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--force-device-scale-factor=2",
        "--window-size=360,466",
        f"--screenshot={OUT}",
        "--virtual-time-budget=3000",
        f"file:///{demo.as_posix()}",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=90)

    if OUT.exists() and OUT.stat().st_size > 0:
        print(f"OK: {OUT}  ({OUT.stat().st_size // 1024} КБ)")
    else:
        print("Не получилось снять скриншот.")
        print(res.stdout[-800:], res.stderr[-800:])
        sys.exit(1)

    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
