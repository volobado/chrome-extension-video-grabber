#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Video Grabber — нативный хост для Chrome.
Принимает от расширения {url, format} и качает через yt-dlp в папку Загрузки.
Общается по протоколу Chrome Native Messaging (4 байта длины + JSON).
"""

import sys
import os
import json
import struct
import shutil
import subprocess
from pathlib import Path

# --- бинарный ввод/вывод (Windows: чтобы \r\n не портил поток) ---
if sys.platform == "win32":
    import msvcrt
    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

STDIN = sys.stdin.buffer
STDOUT = sys.stdout.buffer

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def read_message():
    raw = STDIN.read(4)
    if len(raw) < 4:
        return None
    length = struct.unpack("<I", raw)[0]
    data = STDIN.read(length)
    return json.loads(data.decode("utf-8"))


def send_message(obj):
    data = json.dumps(obj).encode("utf-8")
    STDOUT.write(struct.pack("<I", len(data)))
    STDOUT.write(data)
    STDOUT.flush()


def find_ytdlp():
    """Ищем yt-dlp: PATH -> локальные папки -> python -m yt_dlp."""
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]

    # Рядом с хостом или в корне расширения — можно просто положить бинарник туда.
    here = Path(__file__).resolve().parent
    for d in (here, here.parent):
        for name in ("yt-dlp.exe", "yt-dlp"):
            c = d / name
            if c.exists():
                return [str(c)]

    # Модуль в текущем питоне?
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except Exception:
        return None


def downloads_dir():
    d = Path.home() / "Downloads"
    return d if d.exists() else Path.home()


def find_js_runtime():
    """YouTube с 2026 требует JS-движок. deno — родной для yt-dlp, node тоже подходит."""
    for name in ("deno", "node", "bun"):
        if shutil.which(name):
            return name
    return None


def build_args(fmt, out_tpl, cookie_file):
    """Формат -> набор ключей yt-dlp."""
    common = [
        "-o", out_tpl,
        "--no-playlist",
        "--newline",
        "--restrict-filenames",
        "--no-update",
        # Решатель JS-челленджей YouTube: без него пропадают качественные форматы.
        "--remote-components", "ejs:github",
    ]

    js = find_js_runtime()
    if js:
        common += ["--js-runtimes", js]

    if cookie_file:
        common += ["--cookies", str(cookie_file)]

    if fmt == "audio":
        return common + ["-x", "--audio-format", "mp3", "--audio-quality", "0"]

    heights = {"1080": 1080, "720": 720, "480": 480}
    if fmt in heights:
        h = heights[fmt]
        f = f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
    else:  # best
        f = "bestvideo*+bestaudio/best"

    return common + ["-f", f, "--merge-output-format", "mp4"]


def write_cookie_file(cookie_text):
    """Куки приходят из расширения — пишем во временный файл только для себя."""
    if not cookie_text or not cookie_text.strip():
        return None
    import tempfile
    fd, path = tempfile.mkstemp(prefix="vg_cookies_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(cookie_text)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return Path(path)


def do_download(url, fmt, cookie_text=""):
    yt = find_ytdlp()
    if not yt:
        send_message({
            "status": "error",
            "message": "yt-dlp не найден. Установи: pip install -U yt-dlp (нужен ещё ffmpeg).",
        })
        return

    cookie_file = None
    try:
        cookie_file = write_cookie_file(cookie_text)
    except Exception:
        cookie_file = None

    out_tpl = str(downloads_dir() / "%(title)s.%(ext)s")
    cmd = yt + build_args(fmt, out_tpl, cookie_file) + [url]

    send_message({"status": "progress", "line": "Старт yt-dlp…"})

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as e:
        cleanup_cookies(cookie_file)
        send_message({"status": "error", "message": f"Не запустился yt-dlp: {e}"})
        return

    last_file = ""
    tail = []  # последние строки — пригодятся для текста ошибки
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        if line.startswith("[download]") and "%" in line:
            # Прогресс — шлём как есть.
            send_message({"status": "progress", "line": line.replace("[download] ", "")})
        elif "Destination:" in line:
            last_file = line.split("Destination:", 1)[1].strip()
            send_message({"status": "progress", "line": "Файл: " + os.path.basename(last_file)})
        elif "Merging formats into" in line:
            send_message({"status": "progress", "line": "Склеиваю видео и звук…"})
        elif "[ExtractAudio]" in line:
            send_message({"status": "progress", "line": "Извлекаю звук…"})
        elif line.startswith("ERROR:"):
            tail.append(line)

        if line.startswith(("ERROR:", "WARNING:")):
            tail = tail[-3:]

    proc.wait()
    cleanup_cookies(cookie_file)

    if proc.returncode == 0:
        send_message({"status": "done", "file": os.path.basename(last_file) if last_file else ""})
    else:
        detail = tail[-1] if tail else f"код {proc.returncode}"
        detail = detail.replace("ERROR: ", "")
        if len(detail) > 160:
            detail = detail[:160] + "…"
        send_message({"status": "error", "message": detail})


def cleanup_cookies(cookie_file):
    if not cookie_file:
        return
    try:
        os.remove(cookie_file)
    except Exception:
        pass


def main():
    msg = read_message()
    if msg is None:
        return
    if msg.get("ping"):
        send_message({"status": "pong"})
        return

    url = msg.get("url")
    fmt = msg.get("format", "best")
    cookies = msg.get("cookies", "")
    if not url:
        send_message({"status": "error", "message": "Нет ссылки."})
        return

    try:
        do_download(url, fmt, cookies)
    except Exception as e:
        send_message({"status": "error", "message": str(e)})


if __name__ == "__main__":
    main()
