"""Tollgate - лимиты Claude Code и Codex для Windows: иконка в трее + всплывающая панель.

Данные читаются только локально, в сеть ничего не отправляется:
- Claude: ~/.tollgate/claude-usage.json (пишет statusline.py) или кэш ~/.claude.json - что свежее
- Codex: последний ~/.codex/sessions/**/*.jsonl -> последнее событие с rate_limits
"""
__version__ = "0.8.0"

import base64
import ctypes
import ctypes.wintypes
import json
import math
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from pathlib import Path

import pystray
from PIL import Image, ImageDraw, ImageFont, ImageTk

HOME = Path.home()
CLAUDE_JSON = HOME / ".claude.json"
CODEX_SESSIONS = HOME / ".codex" / "sessions"
TOLLGATE_DIR = HOME / ".tollgate"
CLAUDE_STATUSLINE = TOLLGATE_DIR / "claude-usage.json"  # пишет statusline.py
CODEX_USAGE = TOLLGATE_DIR / "codex-usage.json"  # пишет refresh_codex_usage
STATE_FILE = TOLLGATE_DIR / "state.json"  # состояние виджета: пин, язык, тема, масштаб
REFRESH_MS = 30_000  # период обновления данных
CLAUDE_USAGE_EVERY = 5 * 60  # как часто просить Claude Code обновить лимиты (claude -p /usage), с
TAIL_BYTES = 512 * 1024  # сколько читать с конца лог-файла Codex
MARGIN = 25  # отступ панели от краёв рабочей области, логические px

GREEN, AMBER, RED = "#34c759", "#ff9f0a", "#ff3b30"  # цвета полосок - одинаковые в обеих темах
THEMES = {
    "light": {"BG": "#ffffff", "FG": "#1d1d1f", "DIM": "#86868b", "TRACK": "#ececf0",
              "ALERT_BG": {AMBER: "#fff4e0", RED: "#ffeceb"},
              "BADGES": {"Claude": ("#fbeee8", "#b4583a"), "Codex": ("#e3f4ee", "#0b7d61")},
              "PILL": "#ffffff",  # выбранный вариант в переключателе
              "BORDER": 0xFFFFFFFF},  # рамка окна - цвет Windows по умолчанию
    "dark": {"BG": "#1f1f23", "FG": "#f2f2f5", "DIM": "#8e8e96", "TRACK": "#34343a",
             "ALERT_BG": {AMBER: "#3a2f17", RED: "#3e2222"},
             "BADGES": {"Claude": ("#3d2a23", "#f0a587"), "Codex": ("#17332a", "#5fd3ae")},
             "PILL": "#4a4a52",
             "BORDER": 0x00403A3A},  # COLORREF 0x00BBGGRR - тёмно-серая рамка вместо светлой
}
SCALES = (1.0, 1.25)  # масштаб интерфейса: как сейчас и крупнее
THEME, SCALE = "light", 1.0  # текущие тема и масштаб, задаются из state.json и в настройках
DPI = 1.0  # масштаб главного монитора (1.0 = 96 DPI), перечитывается при каждом обновлении


def apply_theme(name):
    """Переключить палитру: цвета - глобальные, их читают все функции отрисовки."""
    global THEME, BG, FG, DIM, TRACK, ALERT_BG, BADGES, PILL
    THEME = name
    t = THEMES[name]
    BG, FG, DIM, TRACK, ALERT_BG, BADGES = t["BG"], t["FG"], t["DIM"], t["TRACK"], t["ALERT_BG"], t["BADGES"]
    PILL = t["PILL"]


apply_theme("light")
ALERT_LEVELS = (70, 80, 90)  # первый порог уведомления - на выбор в настройках
ALERT_ALWAYS = 95  # на 95% уведомление приходит всегда, отключить нельзя
ALERT_AT = 80  # выбранный первый порог, задаётся из state.json и в настройках
# шрифт значков Windows: (файл, контурный пин, залитый пин, угол иглы в глифе - градусы против часовой от «вправо»)
PIN_FONTS = ((r"C:\Windows\Fonts\SegoeIcons.ttf", "\ue840", "\ue842", 225),  # Windows 11: пин наклонён
             (r"C:\Windows\Fonts\segmdl2.ttf", "\ue718", "\ue841", 180))  # Windows 10: пин лежит горизонтально
GEAR, REFRESH, CLOCK, CLOSE = "\ue713", "\ue72c", "\ue823", "\ue711"  # шестерёнка, обновление, часы, крестик - одинаковые в обоих шрифтах

# --- локализация -------------------------------------------------------------------------------
LANGS = {"ru": "Русский", "en": "English", "zh": "中文"}
STRINGS = {
    "ru": {"no_data": "нет данных", "ago_s": "{n}с", "ago_m": "{n}м",
           "ago_h": "{n}ч", "ago_d": "{n}д", "error": "ошибка: {e}", "reset": "сброшен", "left_dh": "{d}д {h}ч",
           "left_hm": "{h}ч {m}м", "left_m": "{m}м", "eta": "~{left}", "expired": "истекла {date}", "days": "{date} · {n}д",
           "no_limits": "нет данных о лимитах", "show": "Показать", "refresh": "Обновить", "quit": "Выход",
           "alerts": "Уведомлять при (95% - всегда)", "language": "Язык", "theme": "Тема", "light": "Светлая", "dark": "Тёмная", "scale": "Масштаб", "win_5h": "5ч", "win_1d": "1д", "win_1w": "1н", "h": "ч", "date": "%d.%m"},
    "en": {"no_data": "no data", "ago_s": "{n}s", "ago_m": "{n}m",
           "ago_h": "{n}h", "ago_d": "{n}d", "error": "error: {e}", "reset": "reset", "left_dh": "{d}d {h}h",
           "left_hm": "{h}h {m}m", "left_m": "{m}m", "eta": "~{left}", "expired": "expired {date}", "days": "{date} · {n}d",
           "no_limits": "no limit data", "show": "Show", "refresh": "Refresh", "quit": "Quit",
           "alerts": "Alert at (95% - always)", "language": "Language", "theme": "Theme", "light": "Light", "dark": "Dark", "scale": "Scale", "win_5h": "5h", "win_1d": "1d", "win_1w": "1w", "h": "h", "date": "%b %d"},
    "zh": {"no_data": "无数据", "ago_s": "{n}秒", "ago_m": "{n}分",
           "ago_h": "{n}时", "ago_d": "{n}天", "error": "错误: {e}", "reset": "已重置", "left_dh": "{d}天{h}小时",
           "left_hm": "{h}小时{m}分", "left_m": "{m}分", "eta": "约{left}", "expired": "已于 {date} 到期", "days": "{date} · {n}天",
           "no_limits": "无额度数据", "show": "显示", "refresh": "刷新", "quit": "退出",
           "alerts": "提醒阈值（95% 始终提醒）", "language": "语言", "theme": "主题", "light": "浅色", "dark": "深色", "scale": "缩放", "win_5h": "5时", "win_1d": "1天", "win_1w": "1周", "h": "时", "date": "%m月%d日"},
}
LANG = "ru"  # текущий язык, задаётся из state.json и в настройках


def T(key, **kw):
    """Строка интерфейса на текущем языке."""
    return STRINGS[LANG][key].format(**kw)


def win_name(key):
    """Каноническое имя окна ('5h', '1w', '3h') -> подпись на текущем языке."""
    return STRINGS[LANG].get(f"win_{key}") or key.replace("h", T("h"))


def F(size, bold=False, lang=None):
    """Шрифт интерфейса: для китайского - Microsoft YaHei UI (в Segoe UI нет иероглифов).
    Размер в пикселях (отрицательный) по текущему DPI: пункты tk пересчитывает по DPI на момент запуска."""
    size = -round(round(size * SCALE) * DPI * 96 / 72)
    if (lang or LANG) == "zh":
        return ("Microsoft YaHei UI", size, "bold") if bold else ("Microsoft YaHei UI", size)
    return ("Segoe UI Semibold", size) if bold else ("Segoe UI", size)


# --- чтение данных -----------------------------------------------------------------------------
def window_label(minutes):
    """Каноническое имя окна лимита по его длине в минутах: 1w, 1d, 5h."""
    if minutes == 10080:
        return "1w"
    if minutes == 1440:
        return "1d"
    return f"{minutes // 60}h" if minutes else "?"


def parse_iso(s):
    """ISO-строка -> unix-время (или None)."""
    return datetime.fromisoformat(s).timestamp() if s else None


def refresh_claude_usage():
    """Попросить Claude Code обновить лимиты: `claude -p /usage` спрашивает их у Anthropic и пишет в кэш ~/.claude.json
    (тот же запрос Claude Code делает и сам, но редко; статус-строку десктоп-приложение не вызывает вовсе).
    Tollgate токены не читает и сам в сеть не ходит. Запуск ~4 с - только из фонового потока."""
    exe = shutil.which("claude")
    if not exe:
        return False
    TOLLGATE_DIR.mkdir(exist_ok=True)
    try:
        # пустая папка и без пользовательских настроек: не запускаются MCP-серверы, плагины и их хуки, сессия не сохраняется
        subprocess.run([exe, "-p", "/usage", "--no-session-persistence", "--strict-mcp-config", "--setting-sources", "project", "--no-chrome"],
                       cwd=TOLLGATE_DIR, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)  # без мелькающего окна консоли
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def refresh_codex_usage():
    """Спросить у Codex свежие лимиты: `codex app-server` (JSON-RPC построчно через stdio), метод account/rateLimits/read.
    Запрос к OpenAI делает сам Codex, к модели он не идёт; Tollgate токены не читает. Сохраняет в CODEX_USAGE
    в формате логов сессий. Логи Codex пишет, только пока им пользуются, - а лимит тратится и в облаке, и на других ПК."""
    exe = shutil.which("codex")
    if not exe:
        return False
    TOLLGATE_DIR.mkdir(exist_ok=True)
    try:
        proc = subprocess.Popen([exe, "app-server"], cwd=TOLLGATE_DIR, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError:
        return False
    killer = threading.Timer(30, proc.kill)  # завис - через 30 с прервать, чтение stdout тогда закончится
    killer.start()

    def send(msg):
        proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        proc.stdin.flush()

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "tollgate", "version": __version__}}})
        for raw in proc.stdout:
            msg = json.loads(raw)
            if msg.get("id") == 1:  # рукопожатие прошло - можно спрашивать
                send({"method": "initialized"})
                send({"id": 2, "method": "account/rateLimits/read", "params": None})
            elif msg.get("id") == 2:
                rl = (msg.get("result") or {}).get("rateLimits")  # ошибка - result нет
                if not rl:
                    return False
                limits = {k: {"used_percent": w["usedPercent"], "window_minutes": w.get("windowDurationMins"), "resets_at": w.get("resetsAt")}
                          for k in ("primary", "secondary") if (w := rl.get(k))}
                tmp = CODEX_USAGE.with_suffix(".tmp")  # атомарная запись: виджет не прочитает половину файла
                tmp.write_text(json.dumps({"saved_at": time.time(), "rate_limits": limits}), encoding="utf-8")
                tmp.replace(CODEX_USAGE)
                return True
        return False
    except (OSError, ValueError, KeyError):
        return False
    finally:
        killer.cancel()
        proc.kill()


def read_claude():
    """Возвращает (список окон, время получения данных): свежее из статус-строки или кэша Claude Code."""
    try:
        fresh = read_claude_statusline()
    except (OSError, ValueError):  # статус-строка не установлена или файл ещё не создан
        fresh = ([], None)
    cached = read_claude_cache()
    return fresh if (fresh[1] or 0) >= (cached[1] or 0) else cached


def read_claude_statusline():
    """Лимиты, которые statusline.py сохраняет после каждого ответа Claude Code."""
    data = json.loads(CLAUDE_STATUSLINE.read_text(encoding="utf-8"))
    limits = data.get("rate_limits") or {}
    windows = []
    for key, label in (("five_hour", "5h"), ("seven_day", "1w")):
        w = limits.get(key)
        if w:
            windows.append((label, w.get("used_percentage") or 0, w.get("resets_at")))
    return windows, data.get("saved_at")


def read_claude_cache():
    """Лимиты из кэша ~/.claude.json (Claude Code обновляет его редко)."""
    data = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
    cache = data.get("cachedUsageUtilization") or {}
    util = cache.get("utilization") or {}
    windows = []
    for key, label in (("five_hour", "5h"), ("seven_day", "1w")):
        w = util.get(key)
        if w:
            windows.append((label, w.get("utilization") or 0, parse_iso(w.get("resets_at"))))
    fetched = cache.get("fetchedAtMs")
    return windows, fetched / 1000 if fetched else None


def codex_windows(rl):
    """Окна из rate_limits Codex (формат логов сессий): [(имя, %, сброс)]."""
    return [(window_label(w.get("window_minutes")), w.get("used_percent") or 0, w.get("resets_at"))
            for w in (rl.get("primary"), rl.get("secondary")) if w]


def read_codex():
    """Возвращает (список окон, время данных): свежее из запроса к Codex (refresh_codex_usage) или из логов сессий."""
    try:
        fresh = read_codex_cache()
    except (OSError, ValueError):  # codex не установлен или запроса ещё не было
        fresh = ([], None)
    logs = read_codex_logs()
    return fresh if (fresh[1] or 0) >= (logs[1] or 0) else logs


def read_codex_cache():
    """Лимиты, которые refresh_codex_usage сохраняет после запроса к Codex."""
    data = json.loads(CODEX_USAGE.read_text(encoding="utf-8"))
    return codex_windows(data.get("rate_limits") or {}), data.get("saved_at")


def read_codex_logs():
    """Возвращает (список окон, время записи) из самого свежего лога сессии Codex."""
    files = sorted(CODEX_SESSIONS.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files[:5]:  # в самой новой сессии лимитов может ещё не быть - смотрим несколько
        with f.open("rb") as fh:
            fh.seek(max(0, f.stat().st_size - TAIL_BYTES))
            lines = fh.read().decode("utf-8", errors="ignore").splitlines()
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # первая строка хвоста может быть обрезана
            rl = (rec.get("payload") or {}).get("rate_limits") or rec.get("rate_limits")
            if not rl:
                continue
            ts = parse_iso(rec["timestamp"].replace("Z", "+00:00")) if rec.get("timestamp") else f.stat().st_mtime
            return codex_windows(rl), ts
    return [], None


CODEX_PLANS = {"prolite": "Pro Lite", "plus": "Plus", "pro": "Pro", "team": "Team", "free": "Free"}


def add_month(dt):
    """Та же дата через месяц (31 янв -> 28/29 фев)."""
    y, m = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    for day in (dt.day, 30, 29, 28):
        try:
            return dt.replace(year=y, month=m, day=day)
        except ValueError:
            continue


def read_claude_plan():
    """(тариф, дата продления, точная ли дата). Дата окончания Claude локально не хранится -
    оцениваем ближайшее месячное продление от даты оформления подписки."""
    acc = json.loads(CLAUDE_JSON.read_text(encoding="utf-8")).get("oauthAccount") or {}
    org = acc.get("organizationType") or ""
    plan = org.removeprefix("claude_").capitalize() or None  # claude_pro -> Pro, claude_max -> Max
    created = acc.get("subscriptionCreatedAt")
    renew = None
    if created:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        while dt.timestamp() < time.time():
            dt = add_month(dt)
        renew = dt.timestamp()
    return plan, renew, False


def read_codex_plan():
    """(тариф, дата окончания, точная ли дата) из claims id_token в ~/.codex/auth.json.
    Сам токен никуда не отправляется - декодируется только его открытая часть (payload)."""
    auth = json.loads((HOME / ".codex" / "auth.json").read_text(encoding="utf-8"))
    payload = ((auth.get("tokens") or {}).get("id_token") or "").split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    info = claims.get("https://api.openai.com/auth") or {}
    plan = info.get("chatgpt_plan_type")
    return CODEX_PLANS.get(plan, (plan or "").capitalize() or None), parse_iso(info.get("chatgpt_subscription_active_until")), True


# --- форматирование ----------------------------------------------------------------------------
def fmt_left(reset_ts):
    """Сколько осталось до сброса: '2д 3ч', '1ч 12м', '5м'."""
    sec = int(reset_ts - time.time())
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    if d:
        return T("left_dh", d=d, h=h)
    return T("left_hm", h=h, m=m) if h else T("left_m", m=m)


def fmt_age(ts):
    """Возраст данных коротко: 12с, 3м, 2ч, 1д."""
    if not ts:
        return T("no_data")
    sec = max(0, int(time.time() - ts))
    if sec < 60:
        return T("ago_s", n=sec)
    if sec < 3600:
        return T("ago_m", n=sec // 60)
    return T("ago_h", n=sec // 3600) if sec < 86400 else T("ago_d", n=sec // 86400)


def bar_color(p):
    return GREEN if p < 50 else AMBER if p < 80 else RED


def effective(windows, etas=None):
    """Окна -> [(имя, % 0..100, текст до сброса или None, если окно уже сбросилось, текст прогноза или None)].
    etas - {окно: когда при текущем темпе будет 100%} от History.track."""
    out = []
    for label, pct, reset in windows:
        if reset and reset < time.time():  # окно уже сбросилось, а свежих данных ещё нет
            out.append((label, 0, None, None))
        else:
            eta = (etas or {}).get(label)
            out.append((label, max(0, min(100, round(pct))), fmt_left(reset) if reset else "",
                        T("eta", left=fmt_left(eta)) if eta else None))
    return out


# --- история и прогноз -------------------------------------------------------------------------
HISTORY_FILE = TOLLGATE_DIR / "history.jsonl"  # замеры: одна строка - {"t", "p", "w", "pct", "reset"}
HISTORY_DAYS = 7  # сколько дней хранить замеры
PACE_MIN, PACE_MAX = 10 * 60, 60 * 60  # темп считается по отрезку не короче 10 мин и не длиннее часа
PACE_STALE = 30 * 60  # данные старше 30 мин - работа остановилась, «текущего темпа» нет


def pace_end(samples, pct, ts, reset):
    """Когда окно дойдёт до 100% при текущем темпе (unix) - или None: мало данных, данные старые,
    расход не растёт или окно сбросится раньше. samples - замеры этого окна по возрастанию времени."""
    if pct >= 100 or time.time() - ts > PACE_STALE:
        return None
    base = next((s for s in samples if ts - s["t"] <= PACE_MAX), None)  # самый ранний замер за последний час
    if not base or ts - base["t"] < PACE_MIN:
        return None
    rate = (pct - base["pct"]) / (ts - base["t"])  # % в секунду
    if rate <= 0:
        return None
    end = ts + (100 - pct) / rate
    return end if time.time() < end and (not reset or end < reset) else None


class History:
    """Замеры лимитов за 7 дней (~/.tollgate/history.jsonl): пишутся, когда процент меняется.
    Нужны для прогноза, позже - для графика расхода."""

    def __init__(self):
        self.samples = None  # файл читается при первом обращении

    def _load(self):
        """Прочитать замеры за 7 дней; старые и битые строки (обрыв при записи) выбросить и из файла."""
        cutoff = time.time() - HISTORY_DAYS * 86400
        try:
            lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        self.samples = []
        for line in lines:
            try:
                s = json.loads(line)
            except ValueError:
                continue
            if s.get("t", 0) > cutoff:
                self.samples.append(s)
        if len(self.samples) < len(lines):
            self._write("w", self.samples)

    @staticmethod
    def _write(mode, samples):
        try:
            TOLLGATE_DIR.mkdir(exist_ok=True)
            with HISTORY_FILE.open(mode, encoding="utf-8") as f:
                f.writelines(json.dumps(s) + "\n" for s in samples)
        except OSError:
            pass  # история - не главное, виджет работает и без неё

    def track(self, name, windows, ts):
        """Записать новые значения окон провайдера; вернуть {окно: когда при текущем темпе будет 100%}."""
        if self.samples is None:
            self._load()
        if not isinstance(ts, (int, float)):
            return {}
        etas = {}
        for label, pct, reset in windows:
            # замеры текущего окна: тот же сброс (источники Claude округляют его по-разному - допуск 2 мин)
            mine = [s for s in self.samples if s["p"] == name and s["w"] == label and abs((s["reset"] or 0) - (reset or 0)) < 120]
            if not mine or (ts > mine[-1]["t"] and pct != mine[-1]["pct"]):
                s = {"t": ts, "p": name, "w": label, "pct": pct, "reset": reset}
                self.samples.append(s)
                self._write("a", [s])
            end = pace_end(mine, pct, ts, reset)
            if end:
                etas[label] = end
        return etas


HISTORY = History()


def load(reader, name=None):
    """Безопасное чтение провайдера: (окна, время данных или текст ошибки).
    С именем провайдера замеры пишутся в историю и считается прогноз; без имени (--print, тесты) - нет."""
    try:
        windows, ts = reader()
        return effective(windows, HISTORY.track(name, windows, ts) if name else None), ts
    except Exception as ex:  # битый/отсутствующий файл не должен ронять виджет
        return [], T("error", e=type(ex).__name__)


def age_text(status):
    """Подпись возраста данных: время -> '12с' / '3м', текст ошибки - как есть."""
    return status if isinstance(status, str) else fmt_age(status)


def load_plan(reader):
    """Безопасное чтение тарифа: (название, текст про срок, цвет текста) или Nones."""
    try:
        plan, until, exact = reader()
    except Exception:  # нет файла/полей - просто не показываем тариф
        return None, None, None
    if not until:
        return plan, None, None
    days = math.ceil((until - time.time()) / 86400)
    date = datetime.fromtimestamp(until).strftime(T("date"))
    if days <= 0:
        return plan, T("expired", date=date), RED
    text = T("days", date=date, n=days)
    return plan, text, AMBER if days <= 3 else DIM


# --- картинки ----------------------------------------------------------------------------------
def rounded_bar(pct, w, h, color, bg=None):
    """Гладкая полоска со скруглёнными концами: рисуем в 4x и уменьшаем (антиалиасинг)."""
    k = 4
    img = Image.new("RGB", (w * k, h * k), bg or BG)
    d = ImageDraw.Draw(img)
    r = h * k // 2
    d.rounded_rectangle([0, 0, w * k - 1, h * k - 1], radius=r, fill=TRACK)
    if pct > 0:
        d.rounded_rectangle([0, 0, max(2 * r, w * k * pct / 100) - 1, h * k - 1], radius=r, fill=color)
    return img.resize((w, h), Image.LANCZOS)


def icon_font(px):
    """Первый доступный шрифт значков Windows: (шрифт, запись из PIN_FONTS) или (None, None)."""
    for entry in PIN_FONTS:
        try:
            return ImageFont.truetype(entry[0], px), entry
        except OSError:
            continue
    return None, None


def flatten(img, size, bg):
    """RGBA-картинка в 4x -> RGB нужного размера на фоне панели."""
    out = Image.new("RGB", img.size, bg or BG)
    out.paste(img, mask=img)
    return out.resize((size, size), Image.LANCZOS)


def pin_image(pinned, size, bg=None):
    """Значок пина: откреплён - контур с наклоном 45°, закреплён - заливка, игла вертикально вниз."""
    k = 4  # рисуем крупно и уменьшаем - гладкие края после поворота
    img = Image.new("RGBA", (size * k, size * k), (0, 0, 0, 0))
    font, entry = icon_font(int(size * k * 0.8))
    if font:
        _, outline, filled, needle = entry
        d = ImageDraw.Draw(img)
        # залитый глиф - только «головка» без иглы, поэтому кладём его поверх контура
        for glyph in (outline, filled) if pinned else (outline,):
            d.text((size * k / 2, size * k / 2), glyph, font=font, anchor="mm", fill=FG if pinned else DIM)
        # 225° = игла влево-вниз (наклон), 270° = вниз (воткнут); rotate() крутит против часовой
        img = img.rotate((270 if pinned else 225) - needle, resample=Image.BICUBIC)
    return flatten(img, size, bg)


def glyph_image(glyph, color, size, bg=None):
    """Значок из шрифта значков Windows (шестерёнка, стрелка обновления)."""
    k = 4
    img = Image.new("RGBA", (size * k, size * k), (0, 0, 0, 0))
    font, _ = icon_font(int(size * k * 0.75))
    if font:
        ImageDraw.Draw(img).text((size * k / 2, size * k / 2), glyph, font=font, anchor="mm", fill=color)
    return flatten(img, size, bg)


def tray_image(pcts):
    """Иконка трея: две вертикальные полоски (Claude, Codex), заполненные по максимальному окну."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i, p in enumerate(pcts):
        x0 = 8 + i * 28
        d.rounded_rectangle([x0, 4, x0 + 20, 60], radius=8, fill=(150, 150, 160, 110))
        if p is not None:
            fill = max(6, 56 * p / 100)  # минимум - точка, чтобы цвет был виден и при 0%
            d.rounded_rectangle([x0, 60 - fill, x0 + 20, 60], radius=min(8, fill / 2), fill=bar_color(p))
    return img


def animate(widget, duration_ms, frame, then=None):
    """Вызывать frame(k) с k от 0 до 1 (ease-out) в течение duration_ms по реальному времени.
    Длительность не растягивается, даже если отдельные кадры запаздывают."""
    t0 = time.perf_counter()

    def step():
        k = min(1.0, (time.perf_counter() - t0) * 1000 / duration_ms)
        frame(1 - (1 - k) ** 3)
        if k < 1:
            widget.after(10, step)
        elif then:
            then()

    step()


def work_area():
    """Рабочая область главного монитора (без панели задач), физические px."""
    r = ctypes.wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA
    return r.right, r.bottom


def monitor_dpi():
    """Масштаб главного монитора (на нём панель): 1.0 = 96 DPI, 2.5 = 250%. Меняется на ходу,
    например при отключении внешнего монитора."""
    user32 = ctypes.windll.user32
    user32.MonitorFromPoint.restype = ctypes.c_void_p  # HMONITOR - указатель, не обрезать до int
    mon = user32.MonitorFromPoint(ctypes.wintypes.POINT(0, 0), 1)  # MONITOR_DEFAULTTOPRIMARY
    x, y = ctypes.c_uint(), ctypes.c_uint()
    ctypes.windll.shcore.GetDpiForMonitor(ctypes.c_void_p(mon), 0, ctypes.byref(x), ctypes.byref(y))  # MDT_EFFECTIVE_DPI
    return x.value / 96 if x.value else 1.0


# --- окно --------------------------------------------------------------------------------------
def row_style(pct):
    """Цвет полоски, подсвечена ли строка, фон строки - по проценту заполнения окна."""
    color = bar_color(pct)
    hot = pct >= ALERT_AT  # подсветка - с первого порога уведомлений
    return color, hot, ALERT_BG[color] if hot else BG


class Widget:
    PROVIDERS = (("Claude", read_claude, read_claude_plan), ("Codex", read_codex, read_codex_plan))
    ANIM_FRAMES, ANIM_MS = 12, 25  # анимация полоски: ~0.3 с

    def __init__(self):
        global LANG, SCALE, DPI, ALERT_AT
        self.root = tk.Tk()
        self.root.withdraw()  # на старте показываем только иконку в трее
        self.root.overrideredirect(True)  # без рамки и заголовка
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG)
        DPI = monitor_dpi()  # коэффициент масштабирования экрана
        self.card = None  # рамка с содержимым: строится один раз, дальше обновляется на месте
        self.screens = {}  # вид -> готовый экран {key, card, refs, rows, heads, bar_w}: переключение без пересборки
        self.layout_key = None  # структура панели - пересборка только при её изменении
        self.refs = {}  # ссылки на элементы, которые меняются при обновлении
        self.rows = []  # строки полосок (для отмены анимаций при пересборке)
        self.statuses = {}  # время данных по провайдерам - для ежесекундного тика возраста
        self.tray_state = None  # что сейчас нарисовано в трее - не дёргать иконку без изменений
        self.placed = None  # размер, под который окно уже прижато к углу
        self.visible = False
        self.hidden_at = 0.0
        self.rounded = False
        self.alerted = {}  # (провайдер, окно) -> последний порог, о котором уже уведомили
        self.toasts = {}  # (провайдер, окно или "plan") -> плашка-уведомление {win, refs, style, values}
        self.view = "limits"  # что показывает панель: limits или settings
        self.limits_size = None  # размер панели лимитов - настройки открываются в том же размере
        self.ip = 0  # общий внутренний отступ слева и справа (задаётся при сборке - зависит от масштаба)
        self.hwnd = None  # окно Windows - для скругления и цвета рамки
        self.bar_w = self.px(150)  # ширина полоски, подгоняется под ширину заголовков
        self.heads = []  # строки-заголовки (шапка, провайдеры) - по ним считается ширина панели
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        self.pinned = state.get("pinned", False)
        LANG = state.get("lang") if state.get("lang") in LANGS else "ru"
        SCALE = state.get("scale") if state.get("scale") in SCALES else 1.0
        ALERT_AT = state.get("alert") if state.get("alert") in ALERT_LEVELS else 80
        apply_theme(state.get("theme") if state.get("theme") in THEMES else "light")
        self.root.configure(bg=BG)  # фон окна - уже сохранённой темы
        self.root.bind("<Escape>", lambda e: self.hide())
        self.root.bind("<FocusOut>", self._on_focus_out)

        # трей живёт в своём потоке, команды в tk передаются через очередь
        self.cmds = queue.Queue()
        self.icon = pystray.Icon("tollgate", tray_image([None, None]), "Tollgate", self._menu())
        threading.Thread(target=self.icon.run, daemon=True).start()
        self.usage_now = threading.Event()  # ручное обновление - не ждать 5 минут
        threading.Thread(target=self._usage_loop, daemon=True).start()
        self._poll()
        self.refresh()
        self._tick()
        if self.pinned:  # закреплённая панель видна сразу после запуска
            self.show(focus=False)

    def _menu(self):
        """Меню иконки трея на текущем языке."""
        return pystray.Menu(
            pystray.MenuItem(f"Tollgate {__version__}", None, enabled=False),  # версия - видно, у кого какая сборка
            pystray.MenuItem(T("show"), lambda: self.cmds.put(self.toggle), default=True),
            pystray.MenuItem(T("refresh"), lambda: self.cmds.put(self.refresh_now)),
            pystray.MenuItem(T("quit"), lambda: self.cmds.put(self.quit)),
        )

    def px(self, v):
        return int(v * DPI * SCALE)

    def _usage_loop(self):
        """Фоновый поток: при старте и раз в 5 минут (или сразу по кнопке) обновить лимиты Claude и Codex, потом перечитать данные."""
        while True:
            claude, codex = refresh_claude_usage(), refresh_codex_usage()
            if claude or codex:
                self.cmds.put(self.refresh)  # tkinter - только из своего потока
            self.usage_now.wait(CLAUDE_USAGE_EVERY)
            self.usage_now.clear()

    def refresh_now(self):
        """Обновить вручную: перечитать файлы сейчас и попросить Claude Code обновить лимиты."""
        self.usage_now.set()
        self.refresh()

    def _poll(self):
        while not self.cmds.empty():
            self.cmds.get()()
        self.root.after(100, self._poll)

    def _on_focus_out(self, e):
        # клик мимо панели: проверяем чуть позже, что фокус ушёл из приложения совсем
        if e.widget is self.root and not self.pinned:
            self.root.after(100, lambda: self.root.focus_get() is None and self.hide())

    def toggle(self):
        if self.visible:
            self.hide()
        elif time.time() - self.hidden_at > 0.4:  # клик по иконке, который и закрыл панель через FocusOut
            self.show()

    def _place(self):
        """Прижать панель к правому нижнему углу рабочей области - только если размер изменился."""
        self.root.update_idletasks()
        w, h = self.card.winfo_reqwidth(), self.card.winfo_reqheight()
        if self.placed == (w, h):
            return
        right, bottom = work_area()
        self.root.geometry(f"{w}x{h}+{right - w - self.px(MARGIN)}+{bottom - h - self.px(MARGIN)}")
        self.placed = (w, h)

    def show(self, focus=True):
        self.visible = True
        self._toast_close_all()  # панель открыта - плашки свою задачу выполнили
        self.view = "limits"  # открытая заново панель всегда начинается с лимитов
        self.placed = None  # рабочая область могла измениться - прижать заново
        self.refresh()
        self.root.deiconify()
        if not self.rounded:  # скругление окна средствами Windows 11 (DWMWA_WINDOW_CORNER_PREFERENCE = ROUND)
            self.hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            pref = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(self.hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
            self.rounded = True
            self._border()
        if focus:
            self.root.focus_force()

    def hide(self):
        if self.visible:
            self.root.withdraw()
            self.visible = False
            self.hidden_at = time.time()

    def _save_state(self):
        try:
            TOLLGATE_DIR.mkdir(exist_ok=True)
            STATE_FILE.write_text(json.dumps({"pinned": self.pinned, "lang": LANG, "theme": THEME, "scale": SCALE, "alert": ALERT_AT}), encoding="utf-8")
        except OSError:
            pass  # не сохранилось - настройки работают до перезапуска

    def toggle_pin(self):
        self.pinned = not self.pinned
        self._save_state()
        self._set_image(self.refs["pin"], pin_image(self.pinned, self.px(16)))  # только значок, без пересборки

    def toggle_settings(self):
        self._switch_view()

    def _switch_view(self):
        self.view = "limits" if self.view == "settings" else "settings"
        self.refresh()

    def _freeze(self, on):
        """Заморозить/разморозить отрисовку окна (WM_SETREDRAW): пока заморожено, на экране остаётся
        старая картинка, а новое содержимое появляется целиком одним кадром - без мерцания."""
        if not (self.hwnd and self.visible):
            return
        user32 = ctypes.windll.user32
        user32.SendMessageW(self.hwnd, 0x000B, 0 if on else 1, 0)
        if not on:  # RDW_INVALIDATE | RDW_ALLCHILDREN | RDW_UPDATENOW - перерисовать всё, без стирания фона
            user32.RedrawWindow(self.hwnd, None, None, 0x0001 | 0x0080 | 0x0100)

    def set_lang(self, code):
        global LANG
        LANG = code
        self._save_state()
        self.icon.menu = self._menu()
        self.icon.update_menu()
        self.refresh()

    def _border(self):
        """Цвет рамки окна под тему (DWMWA_BORDER_COLOR, Windows 11; на Windows 10 просто игнорируется)."""
        if self.hwnd:
            color = ctypes.c_uint(THEMES[THEME]["BORDER"])
            ctypes.windll.dwmapi.DwmSetWindowAttribute(self.hwnd, 34, ctypes.byref(color), ctypes.sizeof(color))

    def set_theme(self, name):
        apply_theme(name)
        self.root.configure(bg=BG)
        self._border()
        self._save_state()
        self.refresh()

    def set_alert(self, level):
        """Первый порог уведомлений (70/80/90). С него же подсвечиваются строки - перерисовать их сразу."""
        global ALERT_AT
        ALERT_AT = level
        self._save_state()
        for screen in self.screens.values():  # строки готового экрана лимитов
            for r in screen["rows"]:
                self._paint_row(r, r["value"], r["value"])

    def set_scale(self, scale):
        global SCALE
        if self.limits_size:  # настройки откроются в размере панели лимитов уже нового масштаба
            k = scale / SCALE
            self.limits_size = (round(self.limits_size[0] * k), round(self.limits_size[1] * k))
        SCALE = scale
        self._save_state()
        self.refresh()

    def _check_dpi(self):
        """Сменился масштаб главного монитора (отключили внешний, поменяли масштаб в Windows) -
        запомнить новый DPI; панель пересоберётся, потому что DPI входит в _layout_key."""
        global DPI
        dpi = monitor_dpi()
        if dpi == DPI:
            return
        if self.limits_size:  # настройки откроются в размере панели лимитов уже нового DPI
            k = dpi / DPI
            self.limits_size = (round(self.limits_size[0] * k), round(self.limits_size[1] * k))
        DPI = dpi
        self.placed = None  # размер окна изменится - прижать к углу заново

    def _check_alerts(self, data):
        """Плашки-уведомления (свои окна, а не уведомления Windows - работают и в режиме «Не беспокоить»).
        Окно лимита перешло порог (выбранный или 95%) - плашка с его строкой; за 3 дня до конца подписки - плашка со сроком.
        Плашка обновляется на месте и висит, пока её не закроют; сбросилось окно или продлилась подписка - уходит сама."""
        live = {}  # ключ плашки -> свежие значения для неё
        fresh = set()  # плашки, которые надо показать (заново)
        for name, windows, _, (plan_name, until_text, until_color) in data:
            for label, pct, left, _ in windows:
                key = (name, label)
                level = max((t for t in (ALERT_AT, ALERT_ALWAYS) if pct >= t), default=0)
                if level > self.alerted.get(key, 0):
                    fresh.add(key)  # новый порог - плашка заново, даже если прошлую закрыли
                self.alerted[key] = level  # после сброса окна уровень падает и уведомление сработает снова
                if level:
                    live[key] = (pct, left)
            soon = until_color == AMBER  # load_plan красит срок жёлтым за 3 дня до окончания
            if soon and not self.alerted.get((name, "plan")):
                fresh.add((name, "plan"))
            self.alerted[(name, "plan")] = soon  # после продления флаг сбросится - в следующий раз уведомит снова
            if soon:
                live[(name, "plan")] = (plan_name, until_text)
        for key in list(self.toasts):
            if key not in live:  # окно сбросилось, подписка продлена
                self._toast_close(key)
        for key, values in live.items():
            if key in fresh or key in self.toasts:
                self._toast_show(key, values)
        self._toast_place()

    # --- плашки-уведомления ----------------------------------------------------------------------
    def _toast_show(self, key, values):
        """Собрать плашку (или пересобрать под новые язык/тему/масштаб) и обновить её значения."""
        style = LANG, THEME, SCALE, DPI
        t = self.toasts.get(key)
        if t and t["style"] != style:
            self._toast_close(key)
            t = None
        if not t:
            t = self.toasts[key] = self._toast_build(key, values)
            t["style"] = style
        if t["values"] == values:
            return
        t["values"] = values
        r = t["refs"]
        if key[1] == "plan":
            r["until"].configure(text=values[1])
            return
        pct, left = values
        color = bar_color(pct)
        self._set_image(r["bar"], rounded_bar(pct, self.px(90), self.px(6), color))
        r["name"].configure(fg=color)
        r["pct"].configure(text=f"{pct}%", fg=color)
        r["left"].configure(text=left or "")

    def _toast_build(self, key, values):
        """Плашка в одну строку в стиле панели: провайдер, окно, полоска, процент, до сброса (значок часов), крестик.
        Для подписки: провайдер, тариф, срок. Отдельное окно поверх всех, фокус не забирает."""
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=BG)
        f = tk.Frame(win, bg=BG, padx=self.px(12), pady=self.px(9))
        f.pack()
        refs = {}
        items = [(tk.Label(f, text=key[0], bg=BG, fg=FG, font=F(10, bold=True, lang="en")), 0)]
        if key[1] == "plan":
            refs["until"] = tk.Label(f, bg=BG, fg=AMBER, font=F(8))
            items += [(self._badge(f, values[0], *BADGES[key[0]]), 8), (refs["until"], 8)]
        else:
            refs["name"] = tk.Label(f, text=win_name(key[1]), bg=BG, font=F(9, bold=True))
            refs["bar"] = tk.Label(f, bg=BG, bd=0)
            refs["pct"] = tk.Label(f, bg=BG, font=F(9, bold=True, lang="en"))
            refs["left"] = tk.Label(f, bg=BG, fg=DIM, font=F(8))
            clock = tk.Label(f, bg=BG, bd=0)
            self._set_image(clock, glyph_image(CLOCK, DIM, self.px(12)))
            items += [(refs["name"], 8), (refs["bar"], 8), (refs["pct"], 8), (clock, 10), (refs["left"], 4)]
        close = tk.Label(f, bg=BG, bd=0, cursor="hand2")
        self._set_image(close, glyph_image(CLOSE, DIM, self.px(11)))
        close.bind("<Button-1>", lambda e: (self._toast_close(key), self._toast_place()))
        items.append((close, 12))
        for w, gap in items:
            w.pack(side="left", padx=(self.px(gap), 0))
            if w is not close:  # клик по плашке - открыть панель
                w.bind("<Button-1>", lambda e: self.show())
        f.bind("<Button-1>", lambda e: self.show())
        return {"win": win, "refs": refs, "values": None}

    def _toast_place(self):
        """Плашки - стопкой в правом нижнем углу (над панелью, если она видна); новые - выше."""
        right, bottom = work_area()
        y = (self.root.winfo_rooty() if self.visible else bottom) - self.px(MARGIN)
        for t in self.toasts.values():
            win = t["win"]
            win.update_idletasks()
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()
            win.geometry(f"{w}x{h}+{right - w - self.px(MARGIN)}+{y - h}")
            y -= h + self.px(8)
            if win.state() == "withdrawn":
                self._toast_reveal(win)

    @staticmethod
    def _toast_reveal(win):
        """Показать плашку, не забирая фокус (WS_EX_NOACTIVATE), с малым скруглением углов: у него короткая
        аккуратная тень, обычное скругление даёт тяжёлую тень, рассчитанную на большие окна."""
        win.deiconify()
        win.update_idletasks()
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(win.winfo_id())
        user32.SetWindowLongW(hwnd, -20, user32.GetWindowLongW(hwnd, -20) | 0x08000000)  # GWL_EXSTYLE |= WS_EX_NOACTIVATE
        for attr, value in ((33, 3), (34, THEMES[THEME]["BORDER"])):  # DWMWCP_ROUNDSMALL, цвет рамки под тему
            v = ctypes.c_uint(value)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))

    def _toast_close(self, key):
        t = self.toasts.pop(key, None)
        if t:
            t["win"].destroy()

    def _toast_close_all(self):
        for key in list(self.toasts):
            self._toast_close(key)

    def quit(self):
        self.icon.stop()
        self.root.destroy()

    # --- построение (один раз на структуру) ---------------------------------------------------
    @staticmethod
    def _set_image(label, image):
        """Поменять картинку у Label; ссылка хранится на самом Label, иначе tk её выбросит."""
        label.img = ImageTk.PhotoImage(image)
        label.configure(image=label.img)

    def _icon_button(self, parent, image, command=None, gap=8):
        """Значок справа в строке, gap - отступ слева; с command - кликабельный."""
        b = tk.Label(parent, bg=BG, bd=0, cursor="hand2" if command else "")
        self._set_image(b, image)
        b.pack(side="right", padx=(self.px(gap), 0))
        if command:
            b.bind("<Button-1>", lambda e: command())
        return b

    def _titlebar(self):
        """Шапка: название и версия слева; возраст самых свежих данных, шестерёнка и пин справа."""
        bar = tk.Frame(self.card, bg=BG)
        bar.grid(row=0, column=0, columnspan=4, sticky="ew", padx=self.ip)
        self.heads.append(bar)
        tk.Label(bar, text="Tollgate", bg=BG, fg=FG, font=F(10, bold=True, lang="en")).pack(side="left")
        tk.Label(bar, text=f"v{__version__}", bg=BG, fg=DIM, font=F(8, lang="en")).pack(side="left", padx=(self.px(5), 0), pady=(self.px(2), 0))
        self.refs["pin"] = self._icon_button(bar, pin_image(self.pinned, self.px(16)), self.toggle_pin)
        self._icon_button(bar, glyph_image(GEAR, FG if self.view == "settings" else DIM, self.px(16)), self.toggle_settings)
        self.refs["age"] = tk.Label(bar, bg=BG, fg=DIM, font=F(8))
        self.refs["age"].pack(side="right")
        self._icon_button(bar, glyph_image(REFRESH, DIM, self.px(13)), self.refresh_now, gap=0)  # клик - обновить вручную
        tk.Frame(self.card, bg=TRACK, height=1).grid(row=1, column=0, columnspan=4, sticky="ew", pady=(self.px(8), self.px(10)))
        return 2  # следующая свободная строка грида

    def _badge(self, parent, text, bg, fg):
        """Плашка тарифа - капсула: гладкая подложка (PIL, 4x и уменьшение) и текст поверх."""
        font = F(8, bold=True, lang="en")
        h = self.px(18)
        w = tkfont.Font(root=self.root, font=font).measure(text) + 2 * self.px(8)
        k = 4
        img = Image.new("RGB", (w * k, h * k), BG)
        ImageDraw.Draw(img).rounded_rectangle([0, 0, w * k - 1, h * k - 1], radius=h * k // 2, fill=bg)
        c = tk.Canvas(parent, width=w, height=h, bg=BG, highlightthickness=0, bd=0)
        c.img = ImageTk.PhotoImage(img.resize((w, h), Image.LANCZOS))  # ссылка, иначе tk выбросит
        c.create_image(0, 0, anchor="nw", image=c.img)
        c.create_text(w / 2, h / 2, text=text, font=font, fill=fg)
        return c

    def _section(self, row, title, windows, age, plan):
        """Заголовок провайдера и его полоски; ссылки на изменяемые элементы - в self.refs[title]."""
        ref = self.refs[title] = {"rows": {}}
        head = tk.Frame(self.card, bg=BG)
        head.grid(row=row, column=0, columnspan=4, sticky="ew", padx=self.ip, pady=(0, self.px(6)))
        self.heads.append(head)
        tk.Label(head, text=title, bg=BG, fg=FG, font=F(11, bold=True, lang="en"), padx=0).pack(side="left")
        name, until_text, _ = plan
        if name:
            self._badge(head, name, *BADGES[title]).pack(side="left", padx=(self.px(8), 0))
        if until_text:
            ref["until"] = tk.Label(head, bg=BG, font=F(8))
            ref["until"].pack(side="left", padx=(self.px(6), 0))
        ref["age"] = tk.Label(head, bg=BG, fg=DIM, font=F(8))
        ref["age"].pack(side="right")
        if not isinstance(age, str):  # у ошибки значка нет - только текст
            self._icon_button(head, glyph_image(CLOCK, DIM, self.px(12)), gap=12)  # отступ - не слипаться с датой
        row += 1
        if not windows:
            tk.Label(self.card, text=T("no_limits"), bg=BG, fg=DIM, font=F(9)).grid(row=row, column=0, columnspan=4, sticky="w", padx=self.ip)
            row += 1
        for label, pct, *_ in windows:
            pad, ip = (0, self.px(4)), self.ip  # внешний отступ между строками, внутренний - для фона подсветки
            r = {"value": pct, "left_text": None, "anim": None, "eta": False,
                 "name": tk.Label(self.card, text=win_name(label), anchor="w", padx=ip, pady=ip),
                 "bar": tk.Label(self.card, bd=0, padx=self.px(10)),
                 "pct": tk.Label(self.card, font=F(9, bold=True, lang="en"), anchor="e", padx=ip),
                 "left": tk.Label(self.card, font=F(8), anchor="e", padx=ip)}  # вправо - край совпадает с заголовками
            for col, key in enumerate(("name", "bar", "pct", "left")):
                r[key].grid(row=row, column=col, sticky="nsew", pady=pad)
            self._paint_row(r, pct, pct)
            ref["rows"][label] = r
            self.rows.append(r)
            row += 1
        return row

    def _segmented(self, options, current, command):
        """Переключатель-сегмент: дорожка-капсула, по которой ездит «пилюля» выбранного варианта.
        options - [(значение, текст, язык шрифта)]; command(значение) - после того, как пилюля доехала."""
        n = len(options)
        seg = max(tkfont.Font(root=self.root, font=F(9, lang=lang)).measure(text) for _, text, lang in options) + 2 * self.px(14)
        h, pad = self.px(24), self.px(2)

        def capsule(w, hh, color, bg):  # гладкая капсула: рисуем в 4x и уменьшаем
            k = 4
            img = Image.new("RGB", (w * k, hh * k), bg)
            ImageDraw.Draw(img).rounded_rectangle([0, 0, w * k - 1, hh * k - 1], radius=hh * k // 2, fill=color)
            return ImageTk.PhotoImage(img.resize((w, hh), Image.LANCZOS))

        c = tk.Canvas(self.card, width=seg * n, height=h, bg=BG, highlightthickness=0, bd=0, cursor="hand2")
        c.imgs = (capsule(seg * n, h, TRACK, BG), capsule(seg - 2 * pad, h - 2 * pad, PILL, TRACK))  # ссылки, иначе tk выбросит
        c.create_image(0, 0, anchor="nw", image=c.imgs[0])
        cur = [v for v, _, _ in options].index(current)
        knob = c.create_image(cur * seg + pad, pad, anchor="nw", image=c.imgs[1])
        texts = [c.create_text(i * seg + seg / 2, h / 2, text=text, font=F(9, lang=lang), fill=FG if i == cur else DIM)
                 for i, (_, text, lang) in enumerate(options)]
        state = {"cur": cur, "busy": False}

        def click(e):
            i = min(n - 1, max(0, int(e.x // seg)))
            if i == state["cur"] or state["busy"]:
                return
            state["busy"] = True
            for j, t in enumerate(texts):
                c.itemconfigure(t, fill=FG if j == i else DIM)
            start, end = state["cur"] * seg + pad, i * seg + pad

            def arrived():
                state["cur"], state["busy"] = i, False  # не все настройки пересобирают панель - пилюля снова кликабельна
                command(options[i][0])

            animate(c, 160, lambda k: c.coords(knob, start + (end - start) * k, pad), arrived)

        c.bind("<Button-1>", click)
        return c

    def _choice(self, row, title, options, current, command, last=False):
        """Группа настроек: подпись и переключатель. options - [(значение, текст, язык шрифта)]."""
        tk.Label(self.card, text=title, bg=BG, fg=DIM, font=F(8)).grid(row=row, column=0, columnspan=4, sticky="w", padx=self.ip)
        seg = self._segmented(options, current, command)
        # компактные отступы - панели лимитов меньше добирать до высоты настроек
        seg.grid(row=row + 1, column=0, columnspan=4, sticky="w", padx=self.ip, pady=(self.px(3), 0 if last else self.px(8)))
        return row + 2

    def _settings(self, row):
        """Настройки в том же окне: язык, тема, масштаб, пороги уведомлений."""
        row = self._choice(row, T("language"), [(c, t, c) for c, t in LANGS.items()], LANG, self.set_lang)
        row = self._choice(row, T("theme"), [(n, T(n), None) for n in THEMES], THEME, self.set_theme)
        row = self._choice(row, T("scale"), [(k, f"{round(k * 100)}%", "en") for k in SCALES], SCALE, self.set_scale)
        self._choice(row, T("alerts"), [(a, f"{a}%", "en") for a in ALERT_LEVELS], ALERT_AT, self.set_alert, last=True)

    def _show_screen(self, key, data):
        """Показать экран текущего вида. Готовый с тем же ключом раскладки - просто подменить (быстро, без
        дорисовки по частям), иначе собрать заново. Скрытый экран не уничтожается - переключение обратно тоже мгновенное."""
        old_card = self.card
        screen = self.screens.get(self.view)
        if screen and screen["key"] == key:
            self.card, self.refs, self.rows, self.heads, self.bar_w = (screen[k] for k in ("card", "refs", "rows", "heads", "bar_w"))
            self._set_image(self.refs["pin"], pin_image(self.pinned, self.px(16)))  # пин могли переключить на другом экране
        else:
            if screen and screen["card"] is not old_card:  # устаревший скрытый экран этого вида
                self._drop(screen)
            self._build(data)
            self.screens[self.view] = {"key": key, "card": self.card, "refs": self.refs, "rows": self.rows, "heads": self.heads, "bar_w": self.bar_w}
        if old_card is None:
            self.card.place(x=0, y=0)
        elif old_card is not self.card:  # подмена одним кадром: пока отрисовка заморожена, на экране старая картинка
            self._freeze(True)
            self.card.place(x=0, y=0)
            if any(s["card"] is old_card for s in self.screens.values()):
                old_card.place_forget()  # готовый экран другого вида - спрятать до следующего переключения
            else:
                self._drop({"card": old_card, "rows": screen["rows"] if screen else []})
            self.root.update_idletasks()
            if self.visible:  # высота экранов разная - размер окна меняется тоже под заморозкой, вместе с содержимым
                self._place()
            self._freeze(False)
            self.root.update_idletasks()  # дорисовать сразу, а не когда цикл событий дойдёт до простоя - иначе мелькает смесь экранов

    def _prebuild_settings(self):
        """Собрать экран настроек заранее и невидимо (без place), если его нет или он устарел (язык, тема, масштаб,
        ширина лимитов). Тогда по шестерёнке - просто подмена готового экрана на любом, даже слабом компьютере."""
        if self.view != "limits":
            return
        saved = self.card, self.refs, self.rows, self.heads, self.bar_w
        self.view = "settings"
        key = self._layout_key(None)  # ключ настроек от данных не зависит
        screen = self.screens.get("settings")
        if not screen or screen["key"] != key:
            if screen:
                self._drop(screen)
            self._build(None)
            self.screens["settings"] = {"key": key, "card": self.card, "refs": self.refs, "rows": self.rows, "heads": self.heads, "bar_w": self.bar_w}
        self.view = "limits"
        self.card, self.refs, self.rows, self.heads, self.bar_w = saved

    def _drop(self, screen):
        """Уничтожить экран; анимациям его строк больше некуда рисовать."""
        for r in screen["rows"]:
            if r["anim"]:
                self.root.after_cancel(r["anim"])
        screen["card"].destroy()

    def _build(self, data):
        """Сборка экрана текущего вида в новой рамке self.card (ещё не показанной)."""
        self.refs, self.rows, self.heads = {}, [], []
        self.ip, self.bar_w = self.px(4), self.px(150)
        self.card = tk.Frame(self.root, bg=BG, padx=self.px(12), pady=self.px(14))
        row = self._titlebar()
        if self.view == "settings":
            self._settings(row)
            self.card.grid_columnconfigure(3, weight=1)  # шапка тянется на всю ширину фиксированного окна
            if self.limits_size:  # ширина - как у панели лимитов (боковые края не прыгают), высота - своя
                self.root.update_idletasks()
                self.card.configure(width=max(self.limits_size[0], self.card.winfo_reqwidth()), height=self.card.winfo_reqheight())
                self.card.grid_propagate(False)
        else:
            self._grid_columns()
            for i, (name, windows, age, plan) in enumerate(data):
                if i:
                    tk.Frame(self.card, bg=TRACK, height=1).grid(row=row, column=0, columnspan=4, sticky="ew", pady=(self.px(4), self.px(10)))
                    row += 1
                row = self._section(row, name, windows, age, plan)
            self._fit_bars()
            # панель лимитов не уже настроек (мало окон, ошибка чтения) - боковые края не прыгают при переключении
            sw = self._settings_width()
            dw = sw - self.card.winfo_reqwidth()
            if dw > 0 and self.rows:  # удлинить полоски - правые края по-прежнему совпадают
                self.bar_w += dw
                for r in self.rows:
                    self._paint_row(r, r["value"], r["value"])
            # невидимая распорка 1px внизу держит ширину, даже когда полосок нет (тогда ширину задают заголовки,
            # а их тексты - дата, возраст - появятся только в _update)
            tk.Frame(self.card, bg=BG, width=sw - 2 * self.px(12), height=1).grid(row=row, column=0, columnspan=4)

    def _settings_width(self):
        """Ширина панели настроек: собрать её невидимо (без place), измерить и выбросить."""
        saved = self.card, self.refs, self.heads, self.view
        self.card, self.refs, self.heads, self.view = tk.Frame(self.root, padx=self.px(12), pady=self.px(14)), {}, [], "settings"
        self._settings(self._titlebar())
        self.root.update_idletasks()
        width = self.card.winfo_reqwidth()
        self.card.destroy()
        self.card, self.refs, self.heads, self.view = saved
        return width

    def _grid_columns(self):
        """Ширина колонок по самому длинному возможному тексту - при обновлениях сетка не гуляет."""
        def width(font, *texts):
            f = tkfont.Font(root=self.root, font=font)
            return max(f.measure(t) for t in texts) + 2 * self.ip
        self.colmin = [
            width(F(9, bold=True), *(win_name(k) for k in ("5h", "1d", "1w"))),  # подпись окна (жирная при подсветке)
            0,  # полоска - своей картинкой
            width(F(9, bold=True, lang="en"), "100%"),
            width(F(8), T("left_dh", d=6, h=23), T("left_hm", h=23, m=59), T("reset"),  # время до сброса или прогноз
                  T("eta", left=T("left_dh", d=6, h=23)), T("eta", left=T("left_hm", h=23, m=59))),
        ]
        for col, size in enumerate(self.colmin):
            self.card.grid_columnconfigure(col, minsize=size)

    def _fit_bars(self):
        """Если заголовки шире строк с полосками - удлинить полоски, чтобы правые края совпали."""
        self.root.update_idletasks()
        need = max(h.winfo_reqwidth() for h in self.heads) + 2 * self.ip
        have = sum(self.colmin) + self.bar_w + 2 * self.px(10)  # 10 - отступы вокруг полоски
        if need > have:
            self.bar_w += need - have
            for r in self.rows:
                self._paint_row(r, r["value"], r["value"])

    # --- обновление на месте (каждые 30 с) ----------------------------------------------------
    def _paint_row(self, r, shown, final):
        """Строка полоски: длина и число - по shown (кадр анимации), цвет и подсветка - по итоговому final."""
        color, hot, bg = row_style(final)
        self._set_image(r["bar"], rounded_bar(shown, self.bar_w, self.px(8), color, bg=bg))
        r["bar"].configure(bg=bg)
        r["name"].configure(bg=bg, fg=color if hot else DIM, font=F(9, bold=hot))
        r["pct"].configure(text=f"{round(shown)}%", bg=bg, fg=color if hot else FG)
        r["left"].configure(bg=bg, fg=color if hot else AMBER if r["eta"] else DIM)  # прогноз - жёлтым

    def _animate(self, r, start, end, step=1):
        """Плавное изменение полоски и числа от start к end (ease-out)."""
        t = step / self.ANIM_FRAMES
        self._paint_row(r, start + (end - start) * (1 - (1 - t) ** 3), end)
        r["anim"] = self.root.after(self.ANIM_MS, self._animate, r, start, end, step + 1) if step < self.ANIM_FRAMES else None

    @staticmethod
    def _set_text(label, text, **kw):
        """Сменить текст, только если он действительно другой (без лишней перерисовки)."""
        if label.cget("text") != text:
            label.configure(text=text, **kw)
        elif kw:
            label.configure(**kw)

    def _update(self, data):
        """Обновить значения в уже построенной панели; изменившиеся проценты - с анимацией."""
        if self.view != "limits":
            return
        for name, windows, _, plan in data:
            ref = self.refs[name]
            plan_name, until_text, until_color = plan
            if "until" in ref:
                self._set_text(ref["until"], until_text, fg=until_color)
            for label, pct, left, eta in windows:
                r = ref["rows"][label]
                left = eta or (T("reset") if left is None else left)  # лимит кончится раньше сброса - прогноз вместо сброса
                if left != r["left_text"]:
                    r["left"].configure(text=left)
                    r["left_text"] = left
                if pct != r["value"]:
                    if r["anim"]:
                        self.root.after_cancel(r["anim"])
                    r["eta"] = bool(eta)
                    start, r["value"] = r["value"], pct
                    self._animate(r, start, pct)
                elif bool(eta) != r["eta"]:  # процент тот же, а прогноз появился или пропал - перекрасить
                    r["eta"] = bool(eta)
                    self._paint_row(r, pct, pct)

    def _tick(self):
        """Раз в секунду: только тексты возраста данных (12с -> 13с), без чтения файлов."""
        if self.visible and self.refs:
            self._set_text(self.refs["age"], fmt_age(self.statuses.get("freshest")))
            for name, _, _ in self.PROVIDERS:
                if name in self.refs:
                    self._set_text(self.refs[name]["age"], age_text(self.statuses.get(name)))
        self.root.after(1000, self._tick)

    def _layout_key(self, data):
        """Всё, что меняет состав элементов панели. Совпало - обновляем на месте, иначе пересобираем."""
        if self.view == "settings":
            return "settings", LANG, THEME, SCALE, DPI, self.limits_size and self.limits_size[0]  # ширина - от панели лимитов
        return "limits", LANG, THEME, SCALE, DPI, tuple((n, tuple(l for l, *_ in w), p[0], bool(p[1]), isinstance(a, str))
                                     for n, w, a, p in data)

    def refresh(self):
        if getattr(self, "_job", None):  # ручное обновление не должно плодить таймеры
            self.root.after_cancel(self._job)
        self._check_dpi()
        data = [(name, *load(reader, name), load_plan(plan)) for name, reader, plan in self.PROVIDERS]
        self.statuses = {name: st for name, _, st, _ in data}
        self.statuses["freshest"] = max((st for _, _, st, _ in data if isinstance(st, (int, float))), default=None)
        key = self._layout_key(data)
        if key != self.layout_key:
            self._show_screen(key, data)
            self.layout_key = key
        self._update(data)
        self.refs["age"].configure(text=fmt_age(self.statuses["freshest"]))
        for name, _, st, _ in data:
            if name in self.refs:
                self.refs[name]["age"].configure(text=age_text(st))
        if self.view == "limits":
            self.root.update_idletasks()
            self.limits_size = (self.card.winfo_reqwidth(), self.card.winfo_reqheight())
        # трей: иконка по максимальному окну каждого провайдера + подсказка с цифрами - только при изменениях
        pcts = [max((p for _, p, *_ in w), default=None) for _, w, _, _ in data]
        tip = "\n".join(f"{n}: " + (", ".join(f"{win_name(l)} {p}%" for l, p, *_ in w) or T("no_data")) for n, w, _, _ in data)
        if (pcts, tip) != self.tray_state:
            self.icon.icon = tray_image(pcts)
            self.icon.title = tip[:127]  # лимит длины подсказки в Windows
            self.tray_state = (pcts, tip)
        if self.visible:  # размер мог измениться - заново прижать к углу
            self._place()
        self._job = self.root.after(REFRESH_MS, self.refresh)
        if self.view == "limits":  # в простое собрать настройки заранее - первое открытие тоже без ожидания
            self.root.after_idle(self._prebuild_settings)
        self._check_alerts(data)  # последним: может открыть панель и вложенно вызвать refresh, который переставит таймер


def single_instance():
    """Второй экземпляр (автозапуск + ручной запуск) сразу выходит."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    single_instance.mutex = k32.CreateMutexW(None, False, "tollgate-single-instance")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


if __name__ == "__main__":
    if "--print" in sys.argv:  # режим проверки: вывести данные в консоль без окна
        sys.stdout.reconfigure(encoding="utf-8")
        for name, reader, plan in Widget.PROVIDERS:
            windows, status = load(reader)
            print(name, windows, age_text(status), load_plan(plan))
    elif single_instance():
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # чёткий текст на экранах с масштабом >100%
        Widget().root.mainloop()
