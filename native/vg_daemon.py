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
VERSION = "2.3.0"

APP_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "VideoGrabber"
DAEMON_FILE = APP_DIR / "daemon.json"
SETTINGS_FILE = APP_DIR / "settings.json"
LOG_FILE = APP_DIR / "daemon.log"
# Список задач переживает перезапуск службы: по нему дашборд показывает историю загрузок.
HISTORY_FILE = APP_DIR / "history.json"
COOKIE_DIR = APP_DIR / "cookies"

# Без задач и без запросов от браузера столько времени — выходим, чтобы не висеть в памяти.
IDLE_EXIT_SEC = 30 * 60
MAX_TASKS_KEPT = 200
# Сколько загрузок идёт одновременно. Остальные ждут в очереди и стартуют по мере освобождения мест.
MAX_PARALLEL = 5

# YouTube перестаёт пускать yt-dlp старше пары месяцев: меняются клиенты плеера и
# JS-челленджи, и загрузки начинают падать на ровном месте (403 в середине файла).
# Поэтому демон сам подтягивает свежий yt-dlp, пока очередь пуста.
UPDATE_EVERY_SEC = 3 * 24 * 3600
UPDATE_TIMEOUT_SEC = 300
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Демон живёт без консоли (pythonw), и yt-dlp тогда печатает в кодировке системы (cp1251),
# а мы читаем UTF-8 — кириллица в именах превращалась в «�». Заставляем его писать UTF-8.
CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
UTF8_OUT = ["--encoding", "utf-8"]

# Расширение в готовом имени лишнее: его допишет yt-dlp через %(ext)s, иначе выходит .mp4.mp4.
MEDIA_SUFFIX_RE = re.compile(r"\.(mp4|m4v|webm|mkv|mov|avi|flv|ts|m3u8|mpd|mp3|m4a|aac|ogg|wav|flac)$", re.I)

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


def ytdlp_version(yt):
    try:
        r = subprocess.run(yt + ["--version", "--no-update", "--ignore-config"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60,
                           creationflags=CREATE_NO_WINDOW)
        return (r.stdout or "").strip().splitlines()[0].strip()
    except Exception:
        return ""


def ytdlp_update_cmd(yt):
    """Чем обновлять: pip того питона, которому принадлежит найденный yt-dlp,
    либо самообновление отдельного бинаря. Обёртке из pip ключ -U не поможет —
    она на него отвечает «обновляйся тем, чем ставил»."""
    if len(yt) == 3 and yt[1] == "-m":          # sys.executable -m yt_dlp
        py = yt[0]
    else:
        exe = Path(yt[0])
        py = ""
        # Обёртки pip лежат в Scripts/ рядом с python.exe (Windows) или прямо в bin/ (venv, posix).
        if exe.parent.name.lower() in ("scripts", "bin"):
            for cand in (exe.parent.parent / "python.exe", exe.parent.parent / "python3.exe",
                         exe.parent / "python3", exe.parent / "python",
                         exe.parent.parent / "python3", exe.parent.parent / "python"):
                if cand.exists():
                    py = str(cand)
                    break
        if not py:
            return yt + ["-U"]                  # отдельный бинарь умеет обновлять себя сам
    return [py, "-m", "pip", "install", "-U", "--disable-pip-version-check", "yt-dlp"]


def update_ytdlp():
    yt = find_ytdlp()
    if not yt:
        return
    before = ytdlp_version(yt)
    cmd = ytdlp_update_cmd(yt)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=UPDATE_TIMEOUT_SEC,
                           creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        log(f"обновление yt-dlp не вышло: {e}")
        return

    after = ytdlp_version(yt)
    if after and before and after != before:
        log(f"yt-dlp обновлён: {before} -> {after}")
    elif r.returncode == 0:
        log(f"yt-dlp свежий: {after or before or '?'}")
    else:
        detail = ((r.stderr or "") + (r.stdout or "")).strip().splitlines()
        log(f"обновление yt-dlp не удалось (код {r.returncode}): {detail[-1][:200] if detail else ''}")


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

TASK_DEFAULTS = {
    "id": 0,
    "kind": "video",
    "url": "",
    "title": "",
    "format": "best",
    "status": "queued",   # queued | running | paused | done | error | canceled
    "progress": 0.0,
    "line": "",
    "file": "",
    "message": "",
    "dir": "",
    "index": 0,
    "playlist": "",
    "name": "",           # готовое имя файла без расширения (у потоков с плеера)
    "cookie_file": "",
    "added": 0,
    "started": 0,
    "finished": 0,
    "path": "",           # полный путь к файлу — для «показать в папке»
    "total": "",          # размер, скорость и остаток времени — как их печатает yt-dlp
    "speed": "",
    "eta": "",
    "log": [],            # последние строки yt-dlp: по ним видно, что сломалось
}

# Незаконченные задачи: их не чистим из списка и держим для них файл кук.
# Пауза сюда входит — задача на паузе ещё будет качаться.
ACTIVE = ("queued", "running", "paused")


class Manager:
    def __init__(self):
        self.lock = threading.RLock()
        self.cv = threading.Condition(self.lock)
        self.tasks = []          # список словарей в порядке добавления
        self.seq = 0
        self.procs = {}          # task id -> Popen
        # id задач, чей поток ещё не вышел. После паузы yt-dlp умирает не мгновенно,
        # и второй запуск той же задачи поверх первого подрался бы за один .part.
        self.active = set()
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
            task = dict(TASK_DEFAULTS, id=self.seq, dir=self.settings["dir"], added=time.time(), log=[])
            task.update(kw)
            self.tasks.append(task)
            if len(self.tasks) > MAX_TASKS_KEPT:
                # Чистим только завершённые с головы списка.
                keep = [t for t in self.tasks if t["status"] in ACTIVE]
                done = [t for t in self.tasks if t["status"] not in ACTIVE]
                self.tasks = done[-(MAX_TASKS_KEPT - len(keep)):] + keep
                self.tasks.sort(key=lambda t: t["id"])
            self.cv.notify_all()
            return task

    def public_tasks(self):
        # Все задачи, а не хвост: при очереди в полсотни роликов дашборд должен видеть каждую.
        with self.lock:
            return [{k: v for k, v in t.items() if k != "cookie_file"} for t in self.tasks]

    # --- история на диске ---

    def load_history(self):
        """Прошлые задачи после перезапуска. Незаконченные ставим на паузу, а не
        запускаем сами: молча качать заново не стоит. Кнопка «продолжить» докачает
        их с места обрыва. Файлы кук при этом уже потеряны."""
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
        except Exception:
            return
        with self.lock:
            for t in saved.get("tasks", []):
                t = dict(TASK_DEFAULTS, **t)
                if t.get("status") in ("queued", "running"):
                    t["status"] = "paused"
                    t["message"] = "прервано: служба перезапускалась"
                t["cookie_file"] = ""
                t["line"] = ""
                t["speed"] = t["eta"] = ""
                self.tasks.append(t)
            self.tasks = self.tasks[-MAX_TASKS_KEPT:]
            self.seq = max([self.seq] + [t.get("id", 0) for t in self.tasks])

    def history_saver(self):
        """Раз в пару секунд пишем список на диск, если он поменялся."""
        last = ""
        while not self.stop:
            time.sleep(2)
            with self.lock:
                # Прогресс и скорость меняются каждую секунду, их в сравнение не берём.
                snap = [{k: v for k, v in t.items()
                         if k not in ("cookie_file", "line", "progress", "speed", "eta")}
                        for t in self.tasks]
            text = json.dumps({"tasks": snap}, ensure_ascii=False)
            if text == last:
                continue
            try:
                tmp = HISTORY_FILE.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(text)
                tmp.replace(HISTORY_FILE)
                last = text
            except Exception as e:
                log(f"история не записана: {e}")

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
                # У потока с плеера yt-dlp знает только имя вида "manifest", поэтому
                # имя файла приходит от расширения.
                name=sanitize_folder(MEDIA_SUFFIX_RE.sub("", req.get("filename", "").strip()))
                if req.get("filename") else "",
                cookie_file=cookie_file,
            )
        # Повтор упавшей загрузки: новая задача встаёт в конец очереди, старая запись
        # больше не нужна, иначе в истории копились бы дубли.
        if req.get("replaces"):
            self.cmd_remove({"id": req["replaces"]})
        log(f"enqueue #{t['id']} {t['kind']} {t['url']}")
        return {"ok": True, "id": t["id"]}

    def cmd_remove(self, req):
        """Убрать одну завершённую задачу из списка (файл на диске не трогаем)."""
        tid = req.get("id")
        with self.lock:
            before = len(self.tasks)
            self.tasks = [t for t in self.tasks
                          if not (t["id"] == tid and t["status"] not in ACTIVE)]
            return {"ok": len(self.tasks) < before}

    def cmd_reveal(self, req):
        """Показать файл в проводнике, выделив его. Нет файла — открываем папку задачи."""
        tid = req.get("id")
        with self.lock:
            task = next((t for t in self.tasks if t["id"] == tid), None)
        if not task:
            return {"ok": False, "message": "нет такой задачи"}
        path = task.get("path") or ""
        if not path and task.get("file"):
            path = str(Path(task["dir"]) / task["file"])
        try:
            if path and Path(path).exists() and sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", str(Path(path))])
                return {"ok": True}
            if path and Path(path).is_dir():
                return self.cmd_open_dir({"dir": path})
            return self.cmd_open_dir({"dir": task.get("dir", "")})
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def cmd_cancel(self, req):
        tid = req.get("id")
        with self.lock:
            for t in self.tasks:
                if t["id"] != tid:
                    continue
                if t["status"] in ("queued", "paused"):
                    t["status"] = "canceled"
                    t["message"] = "отменено"
                    t["finished"] = time.time()
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
            ids = [t["id"] for t in self.tasks if t["status"] in ACTIVE]
        for tid in ids:
            self.cmd_cancel({"id": tid})
        return {"ok": True, "count": len(ids)}

    def cmd_pause(self, req):
        """Пауза = остановить yt-dlp, оставив недокачанный .part на диске. При
        продолжении yt-dlp сам докачает его с того же места (--continue по умолчанию)."""
        tid = req.get("id")
        with self.lock:
            for t in self.tasks:
                if t["id"] != tid:
                    continue
                if t["status"] not in ("queued", "running"):
                    return {"ok": False, "message": "задача не качается"}
                was_running = t["status"] == "running"
                t["status"] = "paused"
                t["message"] = ""
                t["speed"] = t["eta"] = ""
                proc = self.procs.get(tid)
                if was_running and proc:
                    kill_tree(proc.pid)
                log(f"#{tid} на паузе")
                return {"ok": True}
        return {"ok": False, "message": "нет такой задачи"}

    def cmd_resume(self, req):
        """Обратно в очередь на своё прежнее место: раздатчик берёт задачи по порядку."""
        tid = req.get("id")
        with self.lock:
            for t in self.tasks:
                if t["id"] != tid:
                    continue
                if t["status"] != "paused":
                    return {"ok": False, "message": "задача не на паузе"}
                t["status"] = "queued"
                t["message"] = ""
                t["line"] = ""
                self.cv.notify_all()
                return {"ok": True}
        return {"ok": False, "message": "нет такой задачи"}

    def cmd_pause_all(self, req):
        with self.lock:
            ids = [t["id"] for t in self.tasks if t["status"] in ("queued", "running")]
            for tid in ids:
                self.cmd_pause({"id": tid})
        return {"ok": True, "count": len(ids)}

    def cmd_resume_all(self, req):
        with self.lock:
            ids = [t["id"] for t in self.tasks if t["status"] == "paused"]
            for tid in ids:
                self.cmd_resume({"id": tid})
        return {"ok": True, "count": len(ids)}

    def cmd_clear_done(self, req):
        with self.lock:
            self.tasks = [t for t in self.tasks if t["status"] in ACTIVE]
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
            "parallel": MAX_PARALLEL,
            "version": VERSION,
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
        if cmd == "pause":
            return self.cmd_pause(req)
        if cmd == "resume":
            return self.cmd_resume(req)
        if cmd == "pause_all":
            return self.cmd_pause_all(req)
        if cmd == "resume_all":
            return self.cmd_resume_all(req)
        if cmd == "clear_done":
            return self.cmd_clear_done(req)
        if cmd == "set_dir":
            return self.cmd_set_dir(req)
        if cmd == "pick_folder":
            return self.cmd_pick_folder(req)
        if cmd == "open_dir":
            return self.cmd_open_dir(req)
        if cmd == "remove":
            return self.cmd_remove(req)
        if cmd == "reveal":
            return self.cmd_reveal(req)
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
                t["cookie_file"] == path and t["status"] in ACTIVE
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
        """Раздаёт задачи из очереди по порядку, не больше MAX_PARALLEL сразу.
        Каждая загрузка идёт в своём потоке; закончилась — место сразу берёт следующая."""
        while not self.stop:
            task = None
            idle = False
            with self.cv:
                running = sum(1 for t in self.tasks if t["status"] == "running")
                if running < MAX_PARALLEL:
                    task = next((t for t in self.tasks if t["status"] == "queued"
                                 and t["id"] not in self.active), None)
                if task is None:
                    self.cv.wait(timeout=5)
                    idle = not any(t["status"] in ("queued", "running") for t in self.tasks)
                else:
                    task["status"] = "running"
                    task["line"] = "запуск…"
                    self.active.add(task["id"])
            if task is not None:
                threading.Thread(target=self.run_task, args=(task,), daemon=True).start()
                continue
            if idle:
                if self.should_exit():
                    log("простой — демон завершается")
                    os._exit(0)
                # Только на пустой очереди: pip не перезапишет файлы под работающим yt-dlp.
                self.maybe_update_ytdlp()

    def run_task(self, task):
        # После паузы время старта прежнее: иначе «заняло» в дашборде врало бы.
        task["started"] = task["started"] or time.time()
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
            with self.cv:
                if task["status"] not in ACTIVE:
                    task["finished"] = time.time()
                task["speed"] = task["eta"] = ""
                self.active.discard(task["id"])
                # Место освободилось — будим раздатчик, чтобы следующая стартовала сразу.
                self.cv.notify_all()
            self.release_cookies(task)

    def maybe_update_ytdlp(self):
        """Метку времени ставим до попытки: без сети иначе долбились бы каждые пять секунд."""
        if time.time() - self.settings.get("ytdlp_checked", 0) < UPDATE_EVERY_SEC:
            return
        with self.lock:
            self.settings["ytdlp_checked"] = time.time()
            save_settings(self.settings)
        update_ytdlp()

    def should_exit(self):
        # С задачами на паузе не выходим: вместе со службой пропали бы их куки.
        with self.lock:
            busy = any(t["status"] in ACTIVE for t in self.tasks)
        return (not busy) and (not self.dialog_open) and (time.time() - self.last_activity > IDLE_EXIT_SEC)

    # --- плейлист ---

    def expand_playlist(self, task):
        yt = find_ytdlp()
        if not yt:
            task["status"] = "error"
            task["message"] = "yt-dlp не найден"
            return

        cmd = yt + ["--flat-playlist", "-J", "--no-warnings", "--ignore-config"] + UTF8_OUT
        if task["cookie_file"]:
            cmd += ["--cookies", task["cookie_file"]]
        cmd += [task["url"]]

        task["line"] = "читаю плейлист…"
        # Через Popen, а не run: длинный плейлист читается долго, и отмена должна работать.
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW, env=CHILD_ENV,
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

        # Отменили или поставили на паузу, пока читали.
        if task["status"] != "running":
            return

        if proc.returncode != 0 or not (out or "").strip():
            tail = (err or "").strip().splitlines()
            task["log"] = [s[:400] for s in tail[-15:]]
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
        title_tpl = esc_tpl(task["name"]) if task.get("name") else "%(title)s"
        if task["index"]:
            name_tpl = f"{task['index']:03d} - {title_tpl}.%(ext)s"
        else:
            name_tpl = f"{title_tpl}.%(ext)s"
        out_tpl = esc_tpl(task["dir"]) + os.sep + name_tpl

        args = [
            "-o", out_tpl,
            "--no-playlist",
            "--newline",
            "--no-update",
            "--ignore-config",
            "--windows-filenames",
            "--trim-filenames", "150",
            *UTF8_OUT,
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
                env=CHILD_ENV,
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
            if not m:
                # Всё, кроме строк прогресса, — в журнал задачи для дашборда.
                task["log"] = (task["log"] + [line[:400]])[-15:]
            if m:
                task["progress"] = float(m.group("pct"))
                speed = m.group("speed") or ""
                eta = m.group("eta") or ""
                task["total"] = m.group("total").lstrip("~")
                task["speed"] = "" if speed.startswith("Unknown") else speed
                task["eta"] = "" if eta.startswith("Unknown") else eta
                task["line"] = f"{m.group('pct')}% из {m.group('total')}" + \
                               (f" · {speed}" if speed else "") + (f" · ETA {eta}" if eta else "")
            elif "Destination:" in line:
                task["path"] = line.split("Destination:", 1)[1].strip()
                task["file"] = os.path.basename(task["path"])
                if not task["title"] or task["title"].startswith("http"):
                    task["title"] = task["file"]
            elif line.endswith("has already been downloaded"):
                m3 = re.match(r"\[download\]\s+(.+?)\s+has already been downloaded", line)
                if m3:
                    task["path"] = m3.group(1)
                    task["file"] = os.path.basename(task["path"])
            elif "Merging formats into" in line:
                task["line"] = "склеиваю видео и звук…"
                task["progress"] = 99.0
                # Имя после склейки и есть итоговое — до него в Destination был кусок (.f251.webm).
                m2 = re.search(r'Merging formats into "(.+?)"', line)
                if m2:
                    task["path"] = m2.group(1)
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

        # Отмена или пауза: процесс убит нами, его код возврата ничего не значит.
        if task["status"] != "running":
            log(f"#{task['id']} остановлено: {task['status']}")
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

    MANAGER.load_history()
    # Куки прошлой жизни службы никому не принадлежат: ссылки на них load_history стёр.
    for old in COOKIE_DIR.glob("*.txt"):
        try:
            old.unlink()
        except Exception:
            pass
    threading.Thread(target=MANAGER.history_saver, daemon=True).start()
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
