"""Общие фикстуры: подменённый домашний каталог с данными Claude и Codex из tests/fixtures."""
import shutil
from pathlib import Path

import pytest

import tollgate

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Временный «домашний каталог»: tollgate читает файлы отсюда, а не из настоящего ~."""
    (tmp_path / ".tollgate").mkdir()
    sessions = tmp_path / ".codex" / "sessions" / "2026" / "10" / "04"  # Codex раскладывает логи по датам
    sessions.mkdir(parents=True)
    shutil.copy(FIXTURES / "claude.json", tmp_path / ".claude.json")
    shutil.copy(FIXTURES / "claude-usage.json", tmp_path / ".tollgate" / "claude-usage.json")
    shutil.copy(FIXTURES / "codex-session.jsonl", sessions / "rollout-test.jsonl")
    monkeypatch.setattr(tollgate, "HOME", tmp_path)
    monkeypatch.setattr(tollgate, "CLAUDE_JSON", tmp_path / ".claude.json")
    monkeypatch.setattr(tollgate, "CLAUDE_STATUSLINE", tmp_path / ".tollgate" / "claude-usage.json")
    monkeypatch.setattr(tollgate, "CODEX_SESSIONS", tmp_path / ".codex" / "sessions")
    monkeypatch.setattr(tollgate, "STATE_FILE", tmp_path / ".tollgate" / "state.json")  # не трогать настройки пользователя
    monkeypatch.setattr(tollgate, "LANG", "ru")
    return tmp_path
