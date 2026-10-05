"""История замеров (~/.tollgate/history.jsonl) и прогноз «при текущем темпе лимит кончится через ...»."""
import json
import time

import pytest

import tollgate as tg

MIN = 60


@pytest.fixture
def history(home, monkeypatch):
    monkeypatch.setattr(tg, "HISTORY_FILE", home / ".tollgate" / "history.jsonl")
    return tg.History()


def samples(*points):
    """Замеры (минут назад, процент) -> формат истории."""
    now = time.time()
    return [{"t": now - m * MIN, "pct": p} for m, p in points]


def test_pace_end_growth():
    now = time.time()
    # 10% -> 40% за 30 мин: 1% в минуту, до 100% ещё 60 мин
    assert tg.pace_end(samples((30, 10)), 40, now, now + 3 * 3600) == pytest.approx(now + 60 * MIN)


def test_pace_end_uses_last_hour_only():
    now = time.time()
    # замер двухчасовой давности не в счёт: темп по последнему часу - 10% за 20 мин
    assert tg.pace_end(samples((120, 0), (20, 30)), 40, now, None) == pytest.approx(now + 120 * MIN)


@pytest.mark.parametrize("points, pct, age_min, reset_min", [
    (((30, 10),), 40, 0, 30),  # окно сбросится раньше, чем кончится лимит
    (((30, 40),), 40, 0, 300),  # расход не растёт
    (((5, 10),), 40, 0, 300),  # отрезок короче 10 минут - темп ненадёжен
    ((), 40, 0, 300),  # замеров нет
    (((60, 10),), 40, 31, 300),  # данные старше 30 минут - работа остановилась
    (((30, 10),), 100, 0, 300),  # лимит уже кончился
])
def test_pace_end_none(points, pct, age_min, reset_min):
    now = time.time()
    assert tg.pace_end(samples(*points), pct, now - age_min * MIN, now + reset_min * MIN) is None


def test_track_writes_changes_and_forecasts(history):
    now = time.time()
    reset = now + 3 * 3600
    assert history.track("Claude", [("5h", 10, reset)], now - 30 * MIN) == {}  # первый замер - прогноза нет
    assert history.track("Claude", [("5h", 10, reset)], now - 20 * MIN) == {}  # процент тот же - не пишется
    etas = history.track("Claude", [("5h", 40, reset)], now)
    assert etas["5h"] == pytest.approx(now + 60 * MIN)
    lines = [json.loads(line) for line in tg.HISTORY_FILE.read_text(encoding="utf-8").splitlines()]
    assert [(s["p"], s["w"], s["pct"]) for s in lines] == [("Claude", "5h", 10), ("Claude", "5h", 40)]


def test_track_new_window_starts_fresh(history):
    now = time.time()
    history.track("Claude", [("5h", 90, now - MIN)], now - 30 * MIN)  # прошлое окно
    # новое окно (другой сброс): старые замеры не участвуют, иначе 90% -> 5% дало бы «падение»
    assert history.track("Claude", [("5h", 5, now + 4 * 3600)], now) == {}
    assert len(history.samples) == 2


def test_track_without_timestamp(history):
    assert history.track("Codex", [("1w", 10, None)], None) == {}


def test_load_drops_old_and_broken_lines(history):
    now = time.time()
    tg.HISTORY_FILE.write_text(
        json.dumps({"t": now - 8 * 86400, "p": "Claude", "w": "5h", "pct": 1, "reset": None}) + "\n"
        + '{"t": 17912\n'  # обрыв записи
        + json.dumps({"t": now - 3600, "p": "Claude", "w": "5h", "pct": 2, "reset": None}) + "\n", encoding="utf-8")
    history.track("Codex", [], now)
    assert [s["pct"] for s in history.samples] == [2]
    assert len(tg.HISTORY_FILE.read_text(encoding="utf-8").splitlines()) == 1


def test_load_with_name_uses_history(history, monkeypatch):
    monkeypatch.setattr(tg, "HISTORY", history)
    now = time.time()
    history.track("Claude", [("5h", 10, now + 3 * 3600)], now - 30 * MIN)
    windows, _ = tg.load(lambda: ([("5h", 40, now + 3 * 3600)], now), "Claude")
    assert windows == [("5h", 40, "3ч 0м", "~1ч 0м")] or windows == [("5h", 40, "2ч 59м", "~59м")]
