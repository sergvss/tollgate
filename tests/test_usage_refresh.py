"""Автообновление лимитов Claude: Tollgate запускает `claude -p /usage`, токены не трогает."""
import subprocess

import tollgate as tg


def test_no_claude_installed(home, monkeypatch):
    monkeypatch.setattr(tg.shutil, "which", lambda name: None)
    assert tg.refresh_claude_usage() is False


def test_runs_usage_command_quietly(home, monkeypatch):
    calls = []
    monkeypatch.setattr(tg.shutil, "which", lambda name: r"C:\bin\claude.exe")
    monkeypatch.setattr(tg.subprocess, "run", lambda args, **kw: calls.append((args, kw)))
    assert tg.refresh_claude_usage() is True
    (args, kw), = calls
    assert args[:3] == [r"C:\bin\claude.exe", "-p", "/usage"]
    assert "--no-session-persistence" in args and "--strict-mcp-config" in args  # без сохранения сессии и MCP-серверов
    assert kw["cwd"] == tg.TOLLGATE_DIR and kw["creationflags"] == subprocess.CREATE_NO_WINDOW


def test_hanging_claude_does_not_break(home, monkeypatch):
    def hang(args, **kw):
        raise subprocess.TimeoutExpired(args, kw["timeout"])
    monkeypatch.setattr(tg.shutil, "which", lambda name: r"C:\bin\claude.exe")
    monkeypatch.setattr(tg.subprocess, "run", hang)
    assert tg.refresh_claude_usage() is False
