"""Разбор форматов Claude Code и Codex: лимиты, тариф, срок подписки, безопасные обёртки."""
import base64
import json
import os
import time
from datetime import datetime, timezone

import pytest

import tollgate as tg


def iso(s):
    return datetime.fromisoformat(s).timestamp()


# --- Claude ------------------------------------------------------------------------------------
def test_claude_statusline(home):
    windows, saved = tg.read_claude_statusline()
    assert windows == [("5h", 31.4, 1791190000), ("1w", 28, 1791252000)]
    assert saved == 1791167281.5


def test_claude_cache(home):
    windows, fetched = tg.read_claude_cache()
    assert windows == [("5h", 18, iso("2026-10-05T22:49:59.734199+00:00")),
                       ("1w", 32, iso("2026-10-06T01:59:59.734219+00:00"))]
    assert fetched == 1791223246.012  # fetchedAtMs -> секунды


def test_claude_cache_without_usage(home):
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    assert tg.read_claude_cache() == ([], None)


def test_claude_takes_fresher_source(home):
    # в фикстурах кэш свежее статус-строки
    assert tg.read_claude() == tg.read_claude_cache()
    usage = home / ".tollgate" / "claude-usage.json"
    data = json.loads(usage.read_text(encoding="utf-8"))
    data["saved_at"] = 1791223246.012 + 60  # статус-строка на минуту свежее кэша
    usage.write_text(json.dumps(data), encoding="utf-8")
    assert tg.read_claude() == tg.read_claude_statusline()


def test_claude_without_statusline(home):
    (home / ".tollgate" / "claude-usage.json").unlink()  # статус-строка не установлена
    assert tg.read_claude() == tg.read_claude_cache()


def test_claude_plan(home):
    plan, renew, exact = tg.read_claude_plan()
    assert (plan, exact) == ("Pro", False)
    now = time.time()
    assert now < renew <= now + 31 * 86400  # ближайшее месячное продление
    assert datetime.fromtimestamp(renew, timezone.utc).day == 2  # тот же день месяца, что и оформление


@pytest.mark.parametrize("src, dst", [
    ((2026, 1, 15), (2026, 2, 15)),
    ((2025, 1, 31), (2025, 2, 28)),
    ((2024, 1, 31), (2024, 2, 29)),  # високосный год
    ((2026, 3, 31), (2026, 4, 30)),
    ((2026, 12, 10), (2027, 1, 10)),
])
def test_add_month(src, dst):
    assert tg.add_month(datetime(*src)) == datetime(*dst)


# --- Codex -------------------------------------------------------------------------------------
def session(home, name):
    return home / ".codex" / "sessions" / "2026" / "10" / "04" / name


def test_codex(home):
    windows, ts = tg.read_codex()
    assert windows == [("5h", 12.5, 1791190000), ("1w", 3.0, 1791726114)]
    assert ts == iso("2026-10-04T13:41:57.212+00:00")


def test_codex_newest_session_without_limits(home):
    # в самой новой сессии событий с лимитами ещё нет - берутся из предыдущей
    newer = session(home, "rollout-newer.jsonl")
    newer.write_text('{"timestamp":"2026-10-05T10:00:00.000Z","type":"session_meta","payload":{}}\n', encoding="utf-8")
    os.utime(newer, (time.time() + 60, time.time() + 60))
    assert tg.read_codex()[0] == [("5h", 12.5, 1791190000), ("1w", 3.0, 1791726114)]


def test_codex_takes_newest_session(home):
    newer = session(home, "rollout-newer.jsonl")
    rec = {"timestamp": "2026-10-05T10:00:00.000Z", "type": "event_msg",
           "payload": {"type": "token_count", "rate_limits": {"primary": {"used_percent": 0.0, "window_minutes": 10080, "resets_at": 1}, "secondary": None}}}
    newer.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    os.utime(newer, (time.time() + 60, time.time() + 60))
    assert tg.read_codex() == ([("1w", 0.0, 1)], iso("2026-10-05T10:00:00+00:00"))


def test_codex_truncated_tail(home, monkeypatch):
    # хвост файла начинается с середины строки с rate_limits - битая строка пропускается, чтение не падает
    f = session(home, "rollout-test.jsonl")
    first = f.read_bytes().split(b"\n", 1)[0]
    monkeypatch.setattr(tg, "TAIL_BYTES", f.stat().st_size - len(first) - 1 - 5)  # отрезать начало второй строки
    assert tg.read_codex() == ([], None)


def test_codex_no_sessions(home):
    session(home, "rollout-test.jsonl").unlink()
    assert tg.read_codex() == ([], None)


@pytest.mark.parametrize("minutes, label", [(300, "5h"), (1440, "1d"), (10080, "1w"), (180, "3h"), (None, "?")])
def test_window_label(minutes, label):
    assert tg.window_label(minutes) == label


def write_auth(home, claims):
    """auth.json с поддельным id_token: заголовок.payload.подпись, payload - base64url без выравнивания."""
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    auth = {"tokens": {"id_token": f"eyJhbGciOiJub25lIn0.{payload}.sig", "access_token": "not-used"}}
    (home / ".codex" / "auth.json").write_text(json.dumps(auth), encoding="utf-8")


def test_codex_plan(home):
    write_auth(home, {"https://api.openai.com/auth": {"chatgpt_plan_type": "prolite",
                                                      "chatgpt_subscription_active_until": "2026-11-01T10:00:00+00:00"}})
    assert tg.read_codex_plan() == ("Pro Lite", iso("2026-11-01T10:00:00+00:00"), True)


def test_codex_plan_unknown_type(home):
    write_auth(home, {"https://api.openai.com/auth": {"chatgpt_plan_type": "enterprise"}})
    assert tg.read_codex_plan() == ("Enterprise", None, True)


# --- обёртки и форматирование ------------------------------------------------------------------
def test_effective():
    future, past = time.time() + 3 * 3600 + 30, time.time() - 10
    assert tg.effective([("5h", 40.6, future), ("1w", 120, None), ("1d", 50, past)], {"5h": time.time() + 40 * 60 + 30}) == [
        ("5h", 41, "3ч 0м", "~40м"),  # прогноз: при текущем темпе 100% через 40 минут
        ("1w", 100, "", None),  # процент ограничен 100, без времени сброса - пустая строка
        ("1d", 0, None, None),  # окно уже сбросилось, а свежих данных нет
    ]


def test_load_error(home):
    def broken():
        raise OSError
    assert tg.load(broken) == ([], "ошибка: OSError")


def plan_reader(until):
    return lambda: ("Pro", until, True)


def test_load_plan_colors(home):
    day = 86400
    plan, text, color = tg.load_plan(plan_reader(time.time() + 20 * day + 60))
    assert (plan, color) == ("Pro", tg.DIM) and text.endswith("21д")
    assert tg.load_plan(plan_reader(time.time() + 2 * day))[2] == tg.AMBER  # за 3 дня до конца - жёлтый
    _, text, color = tg.load_plan(plan_reader(time.time() - day))
    assert color == tg.RED and text.startswith("истекла")


def test_load_plan_without_date_or_file(home):
    assert tg.load_plan(lambda: ("Pro", None, False)) == ("Pro", None, None)
    assert tg.load_plan(tg.read_codex_plan) == (None, None, None)  # auth.json нет - тариф не показываем


@pytest.mark.parametrize("windows, pct", [
    ([("5h", 10, "1ч"), ("1w", 50, "3д")], 10),  # недельные 50% не красят иконку, если 5ч почти пусто
    ([("5h", 10, "1ч"), ("1w", 97, "3д")], 97),  # неделя почти исчерпана - работать нельзя, показать её
    ([("1w", 4, "6д")], 4),  # у Codex Pro Lite одно недельное окно
    ([("1w", 30, "6д"), ("3h", 60, "1ч")], 60),  # порядок окон не важен
    ([], None),
])
def test_tray_pct(windows, pct):
    assert tg.tray_pct(windows) == pct
