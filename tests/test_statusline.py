"""statusline.py: сохранение лимитов, строка для Claude Code, установка без перетирания чужих настроек."""
import io
import json
import sys
from types import SimpleNamespace

import pytest

import statusline


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """Все файлы statusline.py - во временной папке."""
    monkeypatch.setattr(statusline, "OUT", tmp_path / ".tollgate" / "claude-usage.json")
    monkeypatch.setattr(statusline, "STATE", tmp_path / ".tollgate" / "state.json")
    monkeypatch.setattr(statusline, "SETTINGS", tmp_path / ".claude" / "settings.json")
    return tmp_path


def run(monkeypatch, payload):
    """Запустить main() как Claude Code: JSON сессии в stdin, строка - из stdout."""
    out = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode("utf-8"))))
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=out))
    statusline.main()
    return out.getvalue().decode("utf-8")


LIMITS = {"five_hour": {"used_percentage": 31.4, "resets_at": 1791190000},
          "seven_day": {"used_percentage": 25, "resets_at": 1791252000}}


def test_saves_limits_and_prints_line(paths, monkeypatch):
    assert run(monkeypatch, {"model": {"id": "x"}, "rate_limits": LIMITS}) == "5ч 31% · 1н 25%"
    saved = json.loads(statusline.OUT.read_text(encoding="utf-8"))
    assert saved["rate_limits"] == LIMITS and saved["saved_at"] > 0
    assert not statusline.OUT.with_suffix(".tmp").exists()  # временный файл переименован


def test_language_from_widget_state(paths, monkeypatch):
    statusline.STATE.parent.mkdir()
    statusline.STATE.write_text('{"lang": "en"}', encoding="utf-8")
    assert run(monkeypatch, {"rate_limits": LIMITS}) == "5h 31% · 1w 25%"


def test_without_limits(paths, monkeypatch):
    assert run(monkeypatch, {"model": {"id": "x"}}) == ""
    assert not statusline.OUT.exists()  # старые данные не затираются пустыми


def test_install_into_empty_settings(paths):
    statusline.install()
    cmd = json.loads(statusline.SETTINGS.read_text(encoding="utf-8"))["statusLine"]
    assert cmd["type"] == "command" and "statusline.py" in cmd["command"]


def test_install_keeps_other_settings_and_backup(paths):
    statusline.SETTINGS.parent.mkdir()
    statusline.SETTINGS.write_text('{"theme": "dark"}', encoding="utf-8")
    statusline.install()
    settings = json.loads(statusline.SETTINGS.read_text(encoding="utf-8"))
    assert settings["theme"] == "dark" and "statusLine" in settings
    backup = statusline.SETTINGS.with_name("settings.json.bak-tollgate")
    assert json.loads(backup.read_text(encoding="utf-8")) == {"theme": "dark"}


def test_install_does_not_replace_own_statusline(paths):
    statusline.SETTINGS.parent.mkdir()
    mine = '{"statusLine": {"type": "command", "command": "my-line"}}'
    statusline.SETTINGS.write_text(mine, encoding="utf-8")
    statusline.install()
    assert statusline.SETTINGS.read_text(encoding="utf-8") == mine  # чужая статус-строка не тронута
    assert not statusline.SETTINGS.with_name("settings.json.bak-tollgate").exists()
