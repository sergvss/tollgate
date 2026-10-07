"""Автообновление лимитов Claude: Tollgate запускает `claude -p /usage`, токены не трогает."""
import json
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


class FakeAppServer:
    """Подставной `codex app-server`: на рукопожатие и запрос лимитов отвечает заготовленными строками."""

    def __init__(self, answer):
        import io
        self.stdin = io.BytesIO()
        self.stdin.flush = lambda: None
        self.stdout = iter([b'{"id": 1, "result": {}}\n', (answer + "\n").encode()])
        self.killed = False

    def kill(self):
        self.killed = True


def fake_codex(monkeypatch, answer):
    proc = FakeAppServer(answer)
    monkeypatch.setattr(tg.shutil, "which", lambda name: r"C:\bin\codex.exe")
    monkeypatch.setattr(tg.subprocess, "Popen", lambda args, **kw: proc)
    return proc


def test_codex_refresh_saves_limits(home, monkeypatch):
    proc = fake_codex(monkeypatch, '{"id": 2, "result": {"rateLimits": {"limitId": "codex", '
                                   '"primary": {"usedPercent": 4, "windowDurationMins": 10080, "resetsAt": 1791968192}, "secondary": null}}}')
    assert tg.refresh_codex_usage() is True
    assert b"account/rateLimits/read" in proc.stdin.getvalue() and proc.killed  # спросили лимиты и закрыли процесс
    windows, saved = tg.read_codex_cache()
    assert windows == [("1w", 4, 1791968192)] and saved > 0


def test_codex_refresh_error_answer(home, monkeypatch):
    fake_codex(monkeypatch, '{"id": 2, "error": {"code": -32600, "message": "not logged in"}}')
    assert tg.refresh_codex_usage() is False
    assert not tg.CODEX_USAGE.exists()


def test_codex_takes_fresher_source(home):
    # в логах (фикстура) - запись 2026-10-04; запрос к Codex свежее - берётся он
    tg.CODEX_USAGE.write_text(json.dumps({"saved_at": 1891000000, "rate_limits": {"primary": {"used_percent": 7, "window_minutes": 300, "resets_at": 1}}}), encoding="utf-8")
    assert tg.read_codex() == ([("5h", 7, 1)], 1891000000)
    tg.CODEX_USAGE.write_text(json.dumps({"saved_at": 1000, "rate_limits": {}}), encoding="utf-8")  # запрос старее логов
    assert tg.read_codex() == tg.read_codex_logs()
