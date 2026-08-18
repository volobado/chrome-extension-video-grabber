#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Установщик нативного хоста Video Grabber (Windows).

Сам находит ID расширения в профилях браузеров — вручную копировать ничего не нужно.

Что делает:
  1. Ищет установленное расширение Video Grabber во всех профилях Chrome/Edge/Brave.
  2. Создаёт run_host.bat, который запускает yt_dlp_host.py тем же питоном.
  3. Пишет манифест нативного хоста com.videograbber.ytdlp.json.
  4. Регистрирует его в реестре (HKCU — без прав админа).

Запуск:
    python install_native_host.py          # найдёт ID сам
    python install_native_host.py <ID>     # можно задать ID вручную
"""

import sys
import os
import re
import json
import glob
import shutil
from pathlib import Path

HOST_NAME = "com.videograbber.ytdlp"
ROOT = Path(__file__).resolve().parent
NATIVE_DIR = ROOT / "native"
HOST_SCRIPT = NATIVE_DIR / "yt_dlp_host.py"
BAT_PATH = NATIVE_DIR / "run_host.bat"
MANIFEST_PATH = NATIVE_DIR / f"{HOST_NAME}.json"

ID_RE = re.compile(r"^[a-p]{32}$")

# Где искать профили и куда писать в реестр.
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


def find_extension_ids():
    """Ищем наше расширение по пути установки во всех профилях."""
    ids = {}  # id -> список "Браузер/Профиль"
    target = str(ROOT).lower().rstrip("\\/")

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
                    path = str(val.get("path", "")).lower().rstrip("\\/")
                    if path == target:
                        prof = os.path.basename(os.path.dirname(pref))
                        ids.setdefault(eid, []).append(f"{name}/{prof}")
    return ids


def get_python_for_bat():
    """pythonw.exe — чтобы не мигало консольное окно. Иначе обычный python."""
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return pyw if pyw.exists() else exe


def write_bat():
    py = get_python_for_bat()
    content = (
        "@echo off\r\n"
        f'"{py}" "{HOST_SCRIPT}" %*\r\n'
    )
    BAT_PATH.write_text(content, encoding="ascii")
    print(f"  [+] {BAT_PATH.name} -> {py}")


def write_manifest(ext_ids):
    manifest = {
        "name": HOST_NAME,
        "description": "Video Grabber yt-dlp host",
        "path": str(BAT_PATH),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{i}/" for i in ext_ids],
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"  [+] {MANIFEST_PATH.name}")
    for i in ext_ids:
        print(f"      разрешено: {i}")


def register_registry():
    if sys.platform != "win32":
        print("  [i] Не Windows — пропускаю реестр. См. README.")
        return
    import winreg

    for name, base_tpl, subkey in BROWSERS:
        # Пишем только для установленных браузеров.
        if not os.path.isdir(os.path.expandvars(base_tpl)):
            continue
        try:
            key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, subkey + "\\" + HOST_NAME)
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, str(MANIFEST_PATH))
            winreg.CloseKey(key)
            print(f"  [+] Реестр: {name}")
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
        print("       winget install Gyan.FFmpeg")
        ok = False
    return ok


def main():
    print("=== Установка нативного хоста Video Grabber ===\n")

    if not HOST_SCRIPT.exists():
        print(f"Ошибка: нет {HOST_SCRIPT}")
        sys.exit(1)

    # 1. Определяем ID расширения.
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
            print("\n  Либо укажи ID вручную:  python install_native_host.py <ID>")
            sys.exit(1)
        ext_ids = list(found)
        for eid, where in found.items():
            print(f"  [ok] {eid}  ({', '.join(sorted(set(where)))})")

    bad = [i for i in ext_ids if not ID_RE.match(i)]
    if bad:
        print(f"\n  [!!] Не похоже на ID расширения: {', '.join(bad)}")
        sys.exit(1)

    # 2-4. Ставим.
    print()
    write_bat()
    write_manifest(ext_ids)
    register_registry()

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
