#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Установщик нативного хоста Video Grabber (Windows и Linux).

Сам находит ID расширения в профилях браузеров — вручную копировать ничего не нужно.

Что делает:
  1. Ищет установленное расширение Video Grabber во всех профилях Chrome/Edge/Brave/Chromium.
  2. Создаёт пусковой файл, который запускает yt_dlp_host.py тем же питоном
     (run_host.bat на Windows, run_host.sh на Linux).
  3. Пишет манифест нативного хоста com.videograbber.ytdlp.json.
  4. Регистрирует его: Windows — ветка HKCU (без прав админа),
     Linux — файл в <профиль браузера>/NativeMessagingHosts/.

Запуск:
    python3 install_native_host.py          # найдёт ID сам
    python3 install_native_host.py <ID>     # можно задать ID вручную
"""

import sys
import os
import re
import json
import glob
import stat
import shutil
from pathlib import Path

HOST_NAME = "com.videograbber.ytdlp"
ROOT = Path(__file__).resolve().parent
NATIVE_DIR = ROOT / "native"
HOST_SCRIPT = NATIVE_DIR / "yt_dlp_host.py"
MANIFEST_PATH = NATIVE_DIR / f"{HOST_NAME}.json"

IS_WIN = sys.platform == "win32"
LAUNCHER = NATIVE_DIR / ("run_host.bat" if IS_WIN else "run_host.sh")

ID_RE = re.compile(r"^[a-p]{32}$")

# (имя, где лежат профили, куда регистрировать хост)
# На Windows третий элемент — ветка реестра, на Linux — каталог NativeMessagingHosts.
if IS_WIN:
    BROWSERS = [
        ("Chrome", r"%LOCALAPPDATA%\Google\Chrome\User Data",
         r"Software\Google\Chrome\NativeMessagingHosts"),
        ("Edge", r"%LOCALAPPDATA%\Microsoft\Edge\User Data",
         r"Software\Microsoft\Edge\NativeMessagingHosts"),
        ("Brave", r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data",
         r"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts"),
        ("Chromium", r"%LOCALAPPDATA%\Chromium\User Data",
         r"Software\Chromium\NativeMessagingHosts"),
    ]
else:
    _cfg = Path.home() / ".config"
    BROWSERS = [
        ("Chrome", str(_cfg / "google-chrome"), str(_cfg / "google-chrome" / "NativeMessagingHosts")),
        ("Chrome Beta", str(_cfg / "google-chrome-beta"), str(_cfg / "google-chrome-beta" / "NativeMessagingHosts")),
        ("Chromium", str(_cfg / "chromium"), str(_cfg / "chromium" / "NativeMessagingHosts")),
        ("Brave", str(_cfg / "BraveSoftware" / "Brave-Browser"),
         str(_cfg / "BraveSoftware" / "Brave-Browser" / "NativeMessagingHosts")),
        ("Edge", str(_cfg / "microsoft-edge"), str(_cfg / "microsoft-edge" / "NativeMessagingHosts")),
    ]


def norm_path(p):
    """Сравнение путей: на Windows регистр не важен, на Linux важен."""
    s = str(p).rstrip("\\/")
    return s.lower() if IS_WIN else s


def find_extension_ids():
    """Ищем наше расширение по пути установки во всех профилях."""
    ids = {}  # id -> список "Браузер/Профиль"
    target = norm_path(ROOT)

    for name, base_tpl, _ in BROWSERS:
        base = os.path.expandvars(base_tpl)
        if not os.path.isdir(base):
            continue
        for fn in ("Secure Preferences", "Preferences"):
            for pref in glob.glob(os.path.join(base, "*", fn)):
                try:
                    with open(pref, encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    continue
                settings = data.get("extensions", {}).get("settings", {})
                if not isinstance(settings, dict):
                    continue
                for eid, val in settings.items():
                    if not isinstance(val, dict):
                        continue
                    if norm_path(val.get("path", "")) == target:
                        prof = os.path.basename(os.path.dirname(pref))
                        ids.setdefault(eid, []).append(f"{name}/{prof}")
    return ids


def python_for_launcher():
    """pythonw.exe — чтобы не мигало консольное окно. На Linux просто текущий питон."""
    exe = Path(sys.executable)
    if IS_WIN:
        pyw = exe.with_name("pythonw.exe")
        if pyw.exists():
            return pyw
    return exe


def write_launcher():
    py = python_for_launcher()
    if IS_WIN:
        LAUNCHER.write_text(f'@echo off\r\n"{py}" "{HOST_SCRIPT}" %*\r\n', encoding="ascii")
    else:
        # exec — чтобы хост унаследовал stdin/stdout Chrome напрямую,
        # без прослойки-шелла между ними.
        LAUNCHER.write_text(
            f'#!/bin/sh\nexec "{py}" "{HOST_SCRIPT}" "$@"\n', encoding="utf-8"
        )
        LAUNCHER.chmod(LAUNCHER.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"  [+] {LAUNCHER.name} -> {py}")


def build_manifest(ext_ids):
    return {
        "name": HOST_NAME,
        "description": "Video Grabber yt-dlp host",
        "path": str(LAUNCHER),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{i}/" for i in ext_ids],
    }


def write_manifest(ext_ids):
    manifest = build_manifest(ext_ids)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"  [+] {MANIFEST_PATH.name}")
    for i in ext_ids:
        print(f"      разрешено: {i}")
    return manifest


def register(manifest):
    """Windows — реестр, Linux — файл манифеста в каталоге браузера."""
    if IS_WIN:
        import winreg
        for name, base_tpl, subkey in BROWSERS:
            if not os.path.isdir(os.path.expandvars(base_tpl)):
                continue
            try:
                key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, subkey + "\\" + HOST_NAME)
                winreg.SetValueEx(key, None, 0, winreg.REG_SZ, str(MANIFEST_PATH))
                winreg.CloseKey(key)
                print(f"  [+] Реестр: {name}")
            except Exception as e:
                print(f"  [!] {name}: не смог записать ({e})")
        return

    blob = json.dumps(manifest, indent=2)
    for name, base, hosts_dir in BROWSERS:
        # Ставим только тем браузерам, чей профиль реально существует.
        if not os.path.isdir(base):
            continue
        try:
            Path(hosts_dir).mkdir(parents=True, exist_ok=True)
            dest = Path(hosts_dir) / f"{HOST_NAME}.json"
            dest.write_text(blob, encoding="utf-8")
            print(f"  [+] {name}: {dest}")
        except Exception as e:
            print(f"  [!] {name}: не смог записать ({e})")


def check_deps():
    print("\nПроверка зависимостей:")
    ok = True
    yt = shutil.which("yt-dlp")
    if yt:
        print(f"  [ok] yt-dlp: {yt}")
    else:
        try:
            import yt_dlp  # noqa
            print("  [ok] yt-dlp: как python-модуль")
        except Exception:
            print("  [!!] yt-dlp НЕ найден. Установи:  pip install -U yt-dlp")
            ok = False
    ff = shutil.which("ffmpeg")
    if ff:
        print(f"  [ok] ffmpeg: {ff}")
    else:
        print("  [!!] ffmpeg НЕ найден — без него не склеятся видео и звук.")
        print("       " + ("winget install Gyan.FFmpeg" if IS_WIN else "sudo apt install ffmpeg"))
        ok = False
    node = shutil.which("node") or shutil.which("deno")
    if node:
        print(f"  [ok] JS-движок: {node}")
    else:
        print("  [!] node/deno не найден — YouTube отдаст только низкие качества.")
    return ok


def main():
    print("=== Установка нативного хоста Video Grabber ===\n")
    print(f"Система: {'Windows' if IS_WIN else sys.platform}\n")

    if not HOST_SCRIPT.exists():
        print(f"Ошибка: нет {HOST_SCRIPT}")
        sys.exit(1)

    if len(sys.argv) > 1:
        ext_ids = [a.strip() for a in sys.argv[1:] if a.strip()]
        print(f"ID задан вручную: {', '.join(ext_ids)}")
    else:
        print("Ищу расширение в профилях браузеров…")
        found = find_extension_ids()
        if not found:
            print("\n  [!!] Расширение не найдено.")
            print("  Сначала загрузи его: chrome://extensions -> Режим разработчика ->")
            print(f"  Загрузить распакованное -> {ROOT}")
            print(f"\n  Либо укажи ID вручную:  python3 {Path(__file__).name} <ID>")
            sys.exit(1)
        ext_ids = list(found)
        for eid, where in found.items():
            print(f"  [ok] {eid}  ({', '.join(sorted(set(where)))})")

    bad = [i for i in ext_ids if not ID_RE.match(i)]
    if bad:
        print(f"\n  [!!] Не похоже на ID расширения: {', '.join(bad)}")
        sys.exit(1)

    print()
    write_launcher()
    manifest = write_manifest(ext_ids)
    register(manifest)

    deps_ok = check_deps()

    print("\n" + "=" * 46)
    if deps_ok:
        print("Готово. Полностью закрой браузер и открой заново,")
        print("затем жми иконку расширения — внизу попапа должно быть")
        print("'yt-dlp: готов'.")
    else:
        print("Хост установлен, но не хватает зависимостей — см. выше.")


if __name__ == "__main__":
    main()
