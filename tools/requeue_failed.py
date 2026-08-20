#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Вернуть в очередь задачи, которые упали.

Список задач живёт только в памяти службы, поэтому её перезапуск (например, после
правки кода) уносит с собой и упавшие загрузки. Этот скрипт ставит их обратно из
файла-выгрузки, сохраняя папку, номер в плейлисте и выбранное качество, — файлы
лягут туда же и под теми же именами, что и при первом заходе.

    python tools/requeue_failed.py                     # из %LOCALAPPDATA%/VideoGrabber/failed_backup.json
    python tools/requeue_failed.py путь/к/файлу.json
    python tools/requeue_failed.py --dry-run           # только показать, что будет поставлено

Формат файла — список объектов с полями url, title, format, dir, index, playlist_title.
Куки в выгрузку не попадают: ролики, которым нужен логин, надо перезапускать из попапа.
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "VideoGrabber"
DAEMON_FILE = APP_DIR / "daemon.json"
DAEMON_PY = Path(__file__).resolve().parent.parent / "native" / "vg_daemon.py"
DEFAULT_BACKUP = APP_DIR / "failed_backup.json"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def call_daemon(payload, timeout=10):
    info = json.loads(DAEMON_FILE.read_text(encoding="utf-8"))
    payload = dict(payload, token=info["token"])
    s = socket.create_connection(("127.0.0.1", info["port"]), timeout=timeout)
    try:
        s.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        s.settimeout(timeout)
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        s.close()
    return json.loads(data.decode("utf-8"))


def daemon_alive():
    try:
        return "pong" in json.dumps(call_daemon({"cmd": "ping"}, timeout=3))
    except Exception:
        return False


def start_daemon():
    """Служба сама себя не поднимет: обычно её запускает нативный хост по запросу попапа."""
    subprocess.Popen(
        [sys.executable, str(DAEMON_PY)],
        creationflags=0x08000000 if sys.platform == "win32" else 0,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(20):
        time.sleep(0.5)
        if daemon_alive():
            return True
    return False


def main():
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry = "--dry-run" in sys.argv[1:]
    backup = Path(args[0]) if args else DEFAULT_BACKUP

    if not backup.exists():
        print(f"нет файла с задачами: {backup}")
        return 1
    try:
        tasks = json.loads(backup.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"не прочитать {backup}: {e}")
        return 1
    if not isinstance(tasks, list) or not tasks:
        print("в файле нет задач")
        return 1

    print(f"задач к возврату: {len(tasks)}")
    for t in tasks:
        where = Path(t.get("dir", "")).name or "папка по умолчанию"
        num = f"{t.get('index') or 0:03d} " if t.get("index") else ""
        print(f"  {num}{(t.get('title') or t.get('url'))[:50]}  ->  {where}")
    if dry:
        print("\n--dry-run: ничего не поставлено")
        return 0

    if not daemon_alive() and not start_daemon():
        print("служба не отвечает и не поднялась — открой попап расширения и попробуй снова")
        return 1

    ok = 0
    for t in tasks:
        try:
            r = call_daemon({
                "cmd": "enqueue",
                "url": t.get("url", ""),
                "title": t.get("title", ""),
                "format": t.get("format", "best"),
                "dir": t.get("dir", ""),
                "index": t.get("index", 0),
                "playlist_title": t.get("playlist_title", ""),
            })
            if r.get("ok"):
                ok += 1
            else:
                print(f"  не принято: {(t.get('title') or t.get('url'))[:40]} — {r.get('message')}")
        except Exception as e:
            print(f"  ошибка постановки: {(t.get('title') or t.get('url'))[:40]} — {e}")

    print(f"\nпоставлено в очередь: {ok} из {len(tasks)}")
    print("ход загрузки видно в попапе расширения и в daemon.log")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
