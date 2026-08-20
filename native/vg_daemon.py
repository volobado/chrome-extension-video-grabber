#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Video Grabber — демон загрузок.

Живёт отдельным процессом, не привязанным к браузеру: попап можно закрывать,
переключать вкладки и окна, загрузка от этого не обрывается. Нативный хост
(yt_dlp_host.py) — тонкий клиент к этому демону.

Протокол: TCP на 127.0.0.1, порт и токен лежат в %LOCALAPPDATA%/VideoGrabber/daemon.json.
Один запрос — одна строка JSON, один ответ — одна строка JSON.
"""

import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

# Хост сверяет её со своей копией и перезапускает демон, если код обновился.
VERSION = "2.0.0"

APP_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "VideoGrabber"
DAEMON_FILE = APP_DIR / "daemon.json"
SETTINGS_FILE = APP_DIR / "settings.json"
LOG_FILE = APP_DIR / "daemon.log"
COOKIE_DIR = APP_DIR / "cookies"

# Без задач и без запросов от браузера столько времени — выходим, чтобы не висеть в памяти.
IDLE_EXIT_SEC = 30 * 60
MAX_TASKS_KEPT = 200
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_log_lock = threading.Lock()


def log(msg):
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with _log_lock:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
                LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{stamp}] {msg}\n")
    except Exception:
        pass


# ---------- инструменты ----------

def find_ytdlp():
    """yt-dlp: PATH -> папка рядом -> модуль текущего питона."""
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    here = Path(__file__).resolve().parent
    for d in (here, here.parent):
        for name in ("yt-dlp.exe", "yt-dlp"):
            c = d / name
            if c.exists():
                return [str(c)]
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except Exception:
        return None


def find_js_runtime():
    """YouTube с 2026 требует JS-движок: без него пропадают качественные форматы."""
    for name in ("deno", "node", "bun"):
        if shutil.which(name):
            return name
    return None


def default_dir():
    d = Path.home() / "Downloads"
    return d if d.exists() else Path.home()


# ---------- настройки ----------

def load_settings():
    s = {"dir": str(default_dir())}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            s.update(json.load(f))
    except Exception:
        pass
    if not s.get("dir") or not Path(s["dir"]).is_dir():
        s["dir"] = str(default_dir())
    return s


def save_settings(s):
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        tmp.replace(SETTINGS_FILE)
    except Exception as e:
        log(f"save_settings failed: {e}")


# ---------- имена файлов ----------

BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_folder(name):
    name = BAD_CHARS.sub("_", (name or "").strip())
    name = name.rstrip(". ")
    if len(name) > 90:
        name = name[:90].rstrip(". ")
    return name or "playlist"


def esc_tpl(text):
    """Литеральный кусок пути в шаблоне -o: % надо удвоить."""
    return str(text).replace("%", "%%")


PROGRESS_RE = re.compile(
    r"\[download\]\s+(?P<pct>\d+(?:\.\d+)?)%\s+of\s+~?\s*(?P<total>\S+)"
    r"(?:\s+at\s+(?P<speed>\S+))?(?:\s+ETA\s+(?P<eta>\S+))?"
)


# ---------- менеджер очереди ----------

class Manager:
    def __init__(self):
        self.lock = threading.RLock()
        self.cv = threading.Condition(self.lock)
        self.tasks = []          # список словарей в порядке добавления
        self.seq = 0
        self.procs = {}          # task id -> Popen
        self.settings = load_settings()
        self.last_activity = time.time()
        self.dialog_open = False
        self.stop = False

    # --- служебное ---

    def touch(self):
        self.last_activity = time.time()

    def new_task(self, **kw):
        with self.lock:
            self.seq += 1
            task = {
                "id": self.seq,
                "kind": "video",
                "url": "",
                "title": "",
                "format": "best",
                "status": "queued",   # queued | running | done | error | canceled
                "progress": 0.0,
                "line": "",
                "file": "",
                "message": "",
                "dir": self.settings["dir"],
                "index": 0,
                "playlist": "",
                "cookie_file": "",
                "added": time.time(),
            }
            task.update(kw)
            self.tasks.append(task)
            if len(self.tasks) > MAX_TASKS_KEPT:
                # Чистим только завершённые с головы списка.
                keep = [t for t in self.tasks if t["status"] in ("queued", "running")]
                done = [t for t in self.tasks if t["status"] not in ("queued", "running")]
                self.tasks = done[-(MAX_TASKS_KEPT - len(keep)):] + keep
                self.tasks.sort(key=lambda t: t["id"])
            self.cv.notify_all()
            return task

    def public_tasks(self):
        with self.lock:
            out = []
            for t in self.tasks[-60:]:
                out.append({k: v for k, v in t.items() if k != "cookie_file"})
            return out

    # --- команды ---

    def cmd_enqueue(self, req):
        cookie_file = self.write_cookies(req.get("cookies", ""))
        target = req.get("dir") or self.settings["dir"]
        if not Path(target).is_dir():
            try:
                Path(target).mkdir(parents=True, exist_ok=True)
            except Exception:
                target = self.settings["dir"]

        if req.get("playlist"):
            t = self.new_task(
                kind="playlist",
                url=req.get("url", ""),
                title=req.get("title", "") or req.get("url", ""),
                format=req.get("format", "best"),
                dir=target,
                cookie_file=cookie_file,
            )
        else:
            # index/playlist_title приходят при повторе ролика из плейлиста —
            # чтобы файл снова лёг в ту же папку и под тем же номером.
            try:
                index = int(req.get("index") or 0)
            except (TypeError, ValueError):
                index = 0
            t = self.new_task(
                url=req.get("url", ""),
                title=req.get("title", "") or req.get("url", ""),
                format=req.get("format", "best"),
                dir=target,
                index=index,
                playlist=req.get("playlist_title", ""),
                cookie_file=cookie_file,
            )
        log(f"enqueue #{t['id']} {t['kind']} {t['url']}")
        return {"ok": True, "id": t["id"]}

    def cmd_cancel(self, req):
        tid = req.get("id")
        with self.lock:
            for t in self.tasks:
                if t["id"] != tid:
                    continue
                if t["status"] == "queued":
                    t["status"] = "canceled"
                    t["message"] = "отменено"
                    self.release_cookies(t)
                    return {"ok": True}
                if t["status"] == "running":
                    proc = self.procs.get(tid)
                    t["status"] = "canceled"
                    t["message"] = "отменено"
                    if proc:
                        kill_tree(proc.pid)
                    return {"ok": True}
        return {"ok": False, "message": "нет такой задачи"}

    def cmd_cancel_all(self, req):
        with self.lock:
            ids = [t["id"] for t in self.tasks if t["status"] in ("queued", "running")]
        for tid in ids:
            self.cmd_cancel({"id": tid})
        return {"ok": True, "count": len(ids)}

    def cmd_clear_done(self, req):
        with self.lock:
            self.tasks = [t for t in self.tasks if t["status"] in ("queued", "running")]
        return {"ok": True}

    def cmd_set_dir(self, req):
        d = req.get("dir", "")
        if not d or not Path(d).is_dir():
            return {"ok": False, "message": "папка не найдена"}
        self.settings["dir"] = str(Path(d))
        save_settings(self.settings)
        return {"ok": True, "dir": self.settings["dir"]}

    def cmd_pick_folder(self, req):
        """Диалог выбора папки открывает демон, а не хост: попап Chrome при потере
        фокуса закрывается и убил бы окно диалога вместе с процессом хоста."""
        if self.dialog_open:
            return {"ok": True, "already": True}
        self.dialog_open = True
        threading.Thread(target=self._folder_dialog, daemon=True).start()
        return {"ok": True}

    def _folder_dialog(self):
        try:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askdirectory(
                title="Video Grabber — куда сохранять видео",
                initialdir=self.settings["dir"],
            )
            root.destroy()
            if path:
                self.settings["dir"] = str(Path(path))
                save_settings(self.settings)
                log(f"папка сохранения: {path}")
        except Exception as e:
            log(f"folder dialog failed: {e}")
        finally:
            self.dialog_open = False

    def cmd_open_dir(self, req):
        d = req.get("dir") or self.settings["dir"]
        if not Path(d).is_dir():
            d = self.settings["dir"]
        try:
            if sys.platform == "win32":
                os.startfile(d)  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", d])
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def cmd_status(self, req):
        yt = find_ytdlp()
        return {
            "ok": True,
            "tasks": self.public_tasks(),
            "settings": self.settings,
            "dialog_open": self.dialog_open,
            "tools": {
                "ytdlp": bool(yt),
                "ffmpeg": bool(shutil.which("ffmpeg")),
                "js": find_js_runtime() or "",
            },
        }

    def handle(self, req):
        self.touch()
        cmd = req.get("cmd", "")
        if cmd == "ping":
            return {"ok": True, "status": "pong", "pid": os.getpid(), "version": VERSION}
        if cmd == "enqueue":
            return self.cmd_enqueue(req)
        if cmd == "status":
            return self.cmd_status(req)
        if cmd == "cancel":
            return self.cmd_cancel(req)
        if cmd == "cancel_all":
            return self.cmd_cancel_all(req)
        if cmd == "clear_done":
            return self.cmd_clear_done(req)
        if cmd == "set_dir":
            return self.cmd_set_dir(req)
        if cmd == "pick_folder":
            return self.cmd_pick_folder(req)
        if cmd == "open_dir":
            return self.cmd_open_dir(req)
        if cmd == "shutdown":
            self.stop = True
            with self.lock:
                for t in self.tasks:
                    if t["status"] == "running":
                        proc = self.procs.get(t["id"])
                        if proc:
                            kill_tree(proc.pid)
                self.cv.notify_all()
            # Отвечаем и только потом умираем, иначе клиент увидит обрыв.
            threading.Timer(0.3, lambda: os._exit(0)).start()
            return {"ok": True}
        return {"ok": False, "message": f"неизвестная команда: {cmd}"}

    # --- куки ---

    def write_cookies(self, text):
        if not text or not text.strip():
            return ""
        try:
            COOKIE_DIR.mkdir(parents=True, exist_ok=True)
            path = COOKIE_DIR / f"c{int(time.time()*1000)}_{self.seq+1}.txt"
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
            return str(path)
        except Exception as e:
            log(f"cookies write failed: {e}")
            return ""

    def release_cookies(self, task):
        """Файл кук общий у плейлиста и его роликов — удаляем, когда он больше никому не нужен."""
        path = task.get("cookie_file")
        if not path:
            return
        with self.lock:
            still_needed = any(
                t["cookie_file"] == path and t["status"] in ("queued", "running")
                for t in self.tasks
            )
        if still_needed:
            return
        try:
            os.remove(path)
        except Exception:
            pass

    # --- рабочий цикл ---

    def worker(self):
        while not self.stop:
            task = None
            with self.cv:
                for t in self.tasks:
                    if t["status"] == "queued":
                        task = t
                        break
                if task is None:
                    self.cv.wait(timeout=5)
                    if self.should_exit():
                        log("простой — демон завершается")
                        os._exit(0)
                    continue
                task["status"] = "running"
                task["line"] = "запуск…"
            try:
                if task["kind"] == "playlist":
                    self.expand_playlist(task)
                else:
                    self.download(task)
            except Exception as e:
                log(f"task #{task['id']} crashed: {e!r}")
                with self.lock:
                    if task["status"] == "running":
                        task["status"] = "error"
                        task["message"] = str(e)
            finally:
                self.release_cookies(task)

    def should_exit(self):
        with self.lock:
            busy = any(t["status"] in ("queued", "running") for t in self.tasks)
        return (not busy) and (not self.dialog_open) and (time.time() - self.last_activity > IDLE_EXIT_SEC)

    # --- плейлист ---

    def expand_playlist(self, task):
        yt = find_ytdlp()
        if not yt:
            task["status"] = "error"
            task["message"] = "yt-dlp не найден"
            return

        cmd = yt + ["--flat-playlist", "-J", "--no-warnings", "--ignore-config"]
        if task["cookie_file"]:
            cmd += ["--cookies", task["cookie_file"]]
        cmd += [task["url"]]

        task["line"] = "читаю плейлист…"
        # Через Popen, а не run: длинный плейлист читается долго, и отмена должна работать.
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as e:
            task["status"] = "error"
            task["message"] = f"не прочитать плейлист: {e}"
            return

        with self.lock:
            self.procs[task["id"]] = proc
        try:
            out, err = proc.communicate(timeout=600)
        except subprocess.TimeoutExpired:
            kill_tree(proc.pid)
            out, err = "", "слишком долгое чтение плейлиста"
        finally:
            with self.lock:
                self.procs.pop(task["id"], None)

        if task["status"] == "canceled":
            return

        if proc.returncode != 0 or not (out or "").strip():
            tail = (err or "").strip().splitlines()
            task["status"] = "error"
            task["message"] = (tail[-1] if tail else "плейлист не прочитан")[:200]
            return

        try:
            data = json.loads(out)
        except Exception as e:
            task["status"] = "error"
            task["message"] = f"плейлист не разобран: {e}"
            return

        entries = [e for e in (data.get("entries") or []) if e]
        title = data.get("title") or data.get("playlist_title") or "playlist"
        folder = Path(task["dir"]) / sanitize_folder(title)

        if not entries:
            task["status"] = "error"
            task["message"] = "в плейлисте нет роликов"
            return

        try:
            folder.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            task["status"] = "error"
            task["message"] = f"не создать папку: {e}"
            return

        with self.lock:
            children = []
            for i, e in enumerate(entries, 1):
                url = e.get("url") or e.get("webpage_url") or e.get("id")
                if url and not str(url).startswith("http"):
                    url = f"https://www.youtube.com/watch?v={url}"
                if not url:
                    continue
                children.append(self.new_task(
                    url=url,
                    title=e.get("title") or url,
                    format=task["format"],
                    dir=str(folder),
                    index=i,
                    playlist=title,
                    cookie_file=task["cookie_file"],
                ))
            task["status"] = "done"
            task["progress"] = 100.0
            task["playlist"] = title
            task["file"] = str(folder)
            task["message"] = f"роликов: {len(children)}"
        log(f"плейлист «{title}»: {len(children)} роликов -> {folder}")

    # --- загрузка ---

    def build_args(self, task):
        if task["index"]:
            name_tpl = f"{task['index']:03d} - %(title)s.%(ext)s"
        else:
            name_tpl = "%(title)s.%(ext)s"
        out_tpl = esc_tpl(task["dir"]) + os.sep + name_tpl

        args = [
            "-o", out_tpl,
            "--no-playlist",
            "--newline",
            "--no-update",
            "--ignore-config",
            "--windows-filenames",
            "--trim-filenames", "150",
            "--retries", "10",
            "--fragment-retries", "20",
            # Решатель JS-челленджей YouTube: без него остаются только низкие качества.
            "--remote-components", "ejs:github",
        ]

        js = find_js_runtime()
        if js:
            args += ["--js-runtimes", js]
        if task["cookie_file"]:
            args += ["--cookies", task["cookie_file"]]

        fmt = task["format"]
        if fmt == "audio":
            return args + ["-x", "--audio-format", "mp3", "--audio-quality", "0"]

        heights = {"1080": 1080, "720": 720, "480": 480}
        if fmt in heights:
            h = heights[fmt]
            f = f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
        else:
            f = "bestvideo*+bestaudio/best"
        return args + ["-f", f, "--merge-output-format", "mp4"]

    def download(self, task):
        yt = find_ytdlp()
        if not yt:
            task["status"] = "error"
            task["message"] = "yt-dlp не найден. Установи: pip install -U yt-dlp"
            return

        try:
            Path(task["dir"]).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            task["status"] = "error"
            task["message"] = f"не создать папку: {e}"
            return

        cmd = yt + self.build_args(task) + [task["url"]]
        log(f"start #{task['id']}: {' '.join(cmd[-4:])}")

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
            task["status"] = "error"
            task["message"] = f"не запустился yt-dlp: {e}"
            return

        with self.lock:
            self.procs[task["id"]] = proc

        tail = []
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue

            m = PROGRESS_RE.search(line)
            if m:
                task["progress"] = float(m.group("pct"))
                speed = m.group("speed") or ""
                eta = m.group("eta") or ""
                task["line"] = f"{m.group('pct')}% из {m.group('total')}" + \
                               (f" · {speed}" if speed else "") + (f" · ETA {eta}" if eta else "")
            elif "Destination:" in line:
                task["file"] = os.path.basename(line.split("Destination:", 1)[1].strip())
                if not task["title"] or task["title"].startswith("http"):
                    task["title"] = task["file"]
            elif "Merging formats into" in line:
                task["line"] = "склеиваю видео и звук…"
                task["progress"] = 99.0
                # Имя после склейки и есть итоговое — до него в Destination был кусок (.f251.webm).
                m2 = re.search(r'Merging formats into "(.+?)"', line)
                if m2:
                    task["file"] = os.path.basename(m2.group(1))
                    task["title"] = task["file"]
            elif "[ExtractAudio]" in line:
                task["line"] = "извлекаю звук…"
                task["progress"] = 99.0
            elif line.startswith(("ERROR:", "WARNING:")):
                tail.append(line)
                tail = tail[-3:]

        proc.wait()
        with self.lock:
            self.procs.pop(task["id"], None)

        if task["status"] == "canceled":
            log(f"#{task['id']} отменено")
            return

        if proc.returncode == 0:
            task["status"] = "done"
            task["progress"] = 100.0
            task["line"] = ""
            task["message"] = task["file"] or "сохранено"
            log(f"done #{task['id']}: {task['file']}")
        else:
            detail = ""
            for t in reversed(tail):
                if t.startswith("ERROR:"):
                    detail = t[len("ERROR:"):].strip()
                    break
            if not detail:
                detail = tail[-1] if tail else f"код {proc.returncode}"
            task["status"] = "error"
            task["line"] = ""
            task["message"] = detail[:300]
            log(f"error #{task['id']}: {detail[:300]}")


def kill_tree(pid):
    """yt-dlp запускает ffmpeg — убиваем всё дерево, иначе склейка останется висеть."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           creationflags=CREATE_NO_WINDOW,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.kill(pid, 9)
    except Exception as e:
        log(f"kill_tree({pid}) failed: {e}")


# ---------- сервер ----------

MANAGER = Manager()
TOKEN = ""


class Handler(socketserver.StreamRequestHandler):
    timeout = 30

    def handle(self):
        try:
            line = self.rfile.readline()
            if not line:
                return
            req = json.loads(line.decode("utf-8"))
        except Exception as e:
            self.reply({"ok": False, "message": f"плохой запрос: {e}"})
            return

        if req.get("token") != TOKEN:
            self.reply({"ok": False, "message": "неверный токен"})
            return

        try:
            resp = MANAGER.handle(req)
        except Exception as e:
            log(f"handler error: {e!r}")
            resp = {"ok": False, "message": str(e)}
        self.reply(resp)

    def reply(self, obj):
        try:
            self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()
        except Exception:
            pass


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False


def already_running():
    """Демон уже поднят? Тогда второй не нужен."""
    try:
        with open(DAEMON_FILE, "r", encoding="utf-8") as f:
            info = json.load(f)
        s = socket.create_connection(("127.0.0.1", info["port"]), timeout=2)
        s.sendall((json.dumps({"token": info["token"], "cmd": "ping"}) + "\n").encode())
        s.settimeout(3)
        data = s.recv(4096)
        s.close()
        return b"pong" in data
    except Exception:
        return False


def main():
    global TOKEN
    APP_DIR.mkdir(parents=True, exist_ok=True)

    if already_running():
        return

    TOKEN = os.urandom(16).hex()
    server = Server(("127.0.0.1", 0), Handler)
    port = server.server_address[1]

    tmp = DAEMON_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"port": port, "token": TOKEN, "pid": os.getpid()}, f)
    tmp.replace(DAEMON_FILE)
    log(f"демон поднят: порт {port}, pid {os.getpid()}")

    threading.Thread(target=MANAGER.worker, daemon=True).start()
    try:
        server.serve_forever(poll_interval=1)
    finally:
        try:
            DAEMON_FILE.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        log("ФАТАЛЬНО: " + traceback.format_exc())
        raise
