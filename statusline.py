"""Статус-строка Claude Code для Tollgate.

Claude Code вызывает её после каждого ответа и передаёт в stdin JSON сессии, включая rate_limits.
Скрипт сохраняет свежие лимиты в ~/.tollgate/claude-usage.json (их читает tollgate.py)
и печатает короткую строку с лимитами внизу Claude Code.

python statusline.py --install    - прописать в ~/.claude/settings.json (только если статус-строка не задана)
python statusline.py --uninstall  - убрать оттуда (только если это статус-строка Tollgate)
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

OUT = Path.home() / ".tollgate" / "claude-usage.json"
SETTINGS = Path.home() / ".claude" / "settings.json"
STATE = Path.home() / ".tollgate" / "state.json"  # язык выбирается в настройках виджета
NAMES = {"ru": {"five_hour": "5ч", "seven_day": "1н"},
         "en": {"five_hour": "5h", "seven_day": "1w"},
         "zh": {"five_hour": "5时", "seven_day": "1周"}}


def names():
    """Подписи окон на языке, выбранном в виджете (по умолчанию русский)."""
    try:
        lang = json.loads(STATE.read_text(encoding="utf-8")).get("lang")
    except (OSError, ValueError):
        lang = None
    return NAMES.get(lang, NAMES["ru"])


def install():
    """Добавляет statusLine в настройки Claude Code, чужую статус-строку не перетирает."""
    sys.stdout.reconfigure(encoding="utf-8")
    settings = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}
    if settings.get("statusLine"):
        print("В settings.json уже есть своя статус-строка - не трогаю. Данные Claude будут обновляться реже.")
        return
    if SETTINGS.exists():
        shutil.copy2(SETTINGS, SETTINGS.with_name("settings.json.bak-tollgate"))  # бэкап перед правкой
    script = Path(__file__).resolve().as_posix()
    settings["statusLine"] = {"type": "command", "command": f'"{Path(sys.executable).as_posix()}" "{script}"'}
    SETTINGS.parent.mkdir(exist_ok=True)
    SETTINGS.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("Статус-строка Tollgate добавлена в", SETTINGS)


def uninstall():
    """Убирает statusLine из настроек Claude Code, только если она указывает на этот скрипт; остальное не трогает."""
    sys.stdout.reconfigure(encoding="utf-8")
    settings = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}
    if Path(__file__).resolve().as_posix() not in (settings.get("statusLine") or {}).get("command", ""):
        print("Статус-строки Tollgate в settings.json нет - ничего не меняю.")
        return
    # бэкап под своим именем: settings.json.bak-tollgate хранит настройки до установки, его не перетираем
    shutil.copy2(SETTINGS, SETTINGS.with_name("settings.json.bak-tollgate-uninstall"))
    del settings["statusLine"]
    SETTINGS.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("Статус-строка Tollgate убрана из", SETTINGS)


def main():
    data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    limits = data.get("rate_limits")
    if limits:
        # атомарная запись: виджет никогда не прочитает наполовину записанный файл
        OUT.parent.mkdir(exist_ok=True)
        tmp = OUT.with_suffix(".tmp")
        tmp.write_text(json.dumps({"saved_at": time.time(), "rate_limits": limits}), encoding="utf-8")
        os.replace(tmp, OUT)
    parts, labels = [], names()
    for key, w in (limits or {}).items():
        pct = (w or {}).get("used_percentage")
        if pct is not None:
            parts.append(f"{labels.get(key, key)} {round(pct)}%")
    sys.stdout.buffer.write(" · ".join(parts).encode("utf-8"))


if __name__ == "__main__":
    install() if "--install" in sys.argv else uninstall() if "--uninstall" in sys.argv else main()
