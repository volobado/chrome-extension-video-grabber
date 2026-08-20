#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Video Grabber — нативный хост для Chrome.

Сам ничего не качает: это тонкий клиент к демону (vg_daemon.py), который живёт
отдельно от браузера. Так закрытие попапа или переключение окна не обрывает
загрузку — Chrome убивает только хост, а он к этому моменту уже не нужен.

Общается с расширением по протоколу Native Messaging (4 байта длины + JSON).
"""

import json
import os
import socket
import struct
import subprocess
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
DAEMON_PY = HERE / "vg_daemon.py"
APP_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "VideoGrabber"
DAEMON_FILE = APP_DIR / "daemon.json"
HOST_LOG = APP_DIR / "host.log"

# --- бинарный ввод/вывод (Windows: чтобы \r\n не портил поток) ---
if sys.platform == "win32":
    import msvcrt
    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

STDIN = sys.stdin.buffer
STDOUT = sys.stdout.buffer

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def log(msg):
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        if HOST_LOG.exists() and HOST_LOG.stat().st_size > 500_000:
            HOST_LOG.replace(HOST_LOG.with_suffix(".log.1"))
        with open(HOST_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def read_message():
    raw = STDIN.read(4)
    if len(raw) < 4:
        return None
    length = struct.unpack("<I", raw)[0]
    data = STDIN.read(length)
    if len(data) < length:
        return None
    return json.loads(data.decode("utf-8"))


def send_message(obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    STDOUT.write(struct.pack("<I", len(data)))
    STDOUT.write(data)
    STDOUT.flush()


# ---------- связь с демоном ----------

def daemon_info():
    try:
        with open(DAEMON_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def call_daemon(req, timeout=30):
    """Один запрос — одно соединение. Демон отвечает строкой JSON."""
    info = daemon_info()
    if not info:
        return None
    try:
        s = socket.create_connection(("127.0.0.1", info["port"]), timeout=timeout)
    except Exception:
        return None
    try:
        s.settimeout(timeout)
        payload = dict(req)
        payload["token"] = info["token"]
        s.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        s.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
            if b.endswith(b"\n"):
                break
        data = b"".join(chunks).decode("utf-8").strip()
        return json.loads(data) if data else None
    except Exception as e:
        log(f"call_daemon failed: {e!r}")
        return None
    finally:
        try:
            s.close()
        except Exception:
            pass


def python_for_daemon():
    """Тот же питон, но по возможности без консольного окна."""
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        w = exe.with_name("pythonw.exe")
        if w.exists():
            return str(w)
    return str(exe)


def start_via_wmi(exe, script):
    """Запуск чужими руками: процесс порождает служба WMI, поэтому он не попадает
    в job браузера и переживает закрытие попапа вместе с хостом."""
    def q(s):
        return str(s).replace("'", "''")

    ps = (
        "$a = @{{CommandLine='\"{exe}\" \"{script}\"'; CurrentDirectory='{cwd}'}}; "
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments $a; "
        "if ($r.ReturnValue -ne 0) {{ exit 1 }}"
    ).format(exe=q(exe), script=q(script), cwd=q(HERE))

    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, timeout=60, creationflags=0x08000000,
        )
        if res.returncode == 0:
            return True
        log(f"WMI-запуск не удался: rc={res.returncode} {res.stderr[:300]!r}")
    except Exception as e:
        log(f"WMI-запуск сорвался: {e!r}")
    return False


def start_daemon():
    """Запускаем отвязанным процессом: Chrome не должен утащить его за собой."""
    exe = python_for_daemon()
    cmd = [exe, str(DAEMON_PY)]
    base = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "cwd": str(HERE),
        "close_fds": True,
    }
    if sys.platform == "win32":
        # Прямая отвязка работает, только если job браузера её разрешает — обычно нет.
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
        try:
            subprocess.Popen(cmd, creationflags=flags, **base)
            if wait_daemon_up(6):
                return True
            log("breakaway прошёл, но демон не отозвался — пробую WMI")
        except OSError as e:
            log(f"breakaway отклонён ({e}) — иду через WMI")

        if start_via_wmi(exe, DAEMON_PY):
            return True
        try:
            subprocess.Popen(cmd, creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, **base)
            return True
        except Exception as e2:
            log(f"демон не запустился: {e2!r}")
            return False
    try:
        subprocess.Popen(cmd, start_new_session=True, **base)
        return True
    except Exception as e:
        log(f"демон не запустился: {e!r}")
        return False


def wait_daemon_up(seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if call_daemon({"cmd": "ping"}, timeout=2):
            return True
        time.sleep(0.25)
    return False


def wait_daemon_gone(seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not call_daemon({"cmd": "ping"}, timeout=2):
            return True
        time.sleep(0.3)
    return False


def kill_old_daemon():
    """Версия постарше могла не уметь корректно выходить — снимаем по pid."""
    info = daemon_info() or {}
    pid = info.get("pid")
    if pid:
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               creationflags=0x08000000,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.kill(pid, 9)
            log(f"старый демон снят: pid {pid}")
        except Exception as e:
            log(f"не снять старый демон: {e}")
    try:
        DAEMON_FILE.unlink()
    except Exception:
        pass
    time.sleep(0.5)


def expected_version():
    """Версия из исходника демона — сверяем с той, что отвечает работающий процесс."""
    try:
        with open(DAEMON_PY, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VERSION"):
                    return line.split("=", 1)[1].strip().strip('"\'')
    except Exception:
        pass
    return ""


_daemon_ok_until = 0.0


def ensure_daemon():
    """Возвращает True, если демон отвечает (и его версия совпадает с кодом на диске).
    Результат ненадолго кэшируем: попап опрашивает статус раз в секунду."""
    global _daemon_ok_until
    if time.time() < _daemon_ok_until:
        return True

    pong = call_daemon({"cmd": "ping"}, timeout=3)
    if pong:
        want = expected_version()
        have = pong.get("version", "")
        if not want or want == have:
            _daemon_ok_until = time.time() + 10
            return True
        # Код обновился — старый процесс надо сменить, иначе правки не подхватятся.
        # Но не посреди загрузки: подождём, пока очередь опустеет.
        st = call_daemon({"cmd": "status"}, timeout=10) or {}
        if any(t.get("status") in ("queued", "running") for t in st.get("tasks", [])):
            log(f"версия демона {have} != {want}, но идут загрузки — оставляю старый")
            return True
        log(f"версия демона {have} != {want}, перезапускаю")
        call_daemon({"cmd": "shutdown"}, timeout=5)
        if not wait_daemon_gone(3.0):
            kill_old_daemon()
    if not DAEMON_PY.exists():
        log(f"нет файла демона: {DAEMON_PY}")
        return False
    start_daemon()
    # Подъём занимает доли секунды, но на холодном старте питона бывает и дольше.
    for _ in range(40):
        time.sleep(0.25)
        if call_daemon({"cmd": "ping"}, timeout=3):
            log("демон поднят по требованию")
            return True
    log("демон не ответил после запуска")
    return False


# ---------- цикл сообщений ----------

def handle(msg):
    # Старый формат {"ping": true} остаётся рабочим.
    cmd = msg.get("cmd") or ("ping" if msg.get("ping") else "")
    if not cmd:
        cmd = "enqueue" if msg.get("url") else "status"

    if not ensure_daemon():
        return {
            "ok": False,
            "status": "error",
            "message": "Служба загрузок не запустилась. Проверь python и файл native/vg_daemon.py.",
        }

    req = {k: v for k, v in msg.items() if k != "token"}
    req["cmd"] = cmd
    resp = call_daemon(req, timeout=120)
    if resp is None:
        return {"ok": False, "status": "error", "message": "Служба загрузок не отвечает."}
    if cmd == "ping":
        resp.setdefault("status", "pong")
    return resp


def main():
    while True:
        try:
            msg = read_message()
        except Exception as e:
            log(f"чтение сообщения сорвалось: {e!r}")
            return
        if msg is None:
            return
        try:
            send_message(handle(msg))
        except Exception:
            log("ФАТАЛЬНО: " + traceback.format_exc())
            try:
                send_message({"ok": False, "status": "error", "message": "Сбой хоста, см. host.log"})
            except Exception:
                return


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("ФАТАЛЬНО: " + traceback.format_exc())
