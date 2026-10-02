"""Tollgate - лимиты Claude Code и Codex для Windows: иконка в трее + всплывающая панель.

Данные читаются только локально, в сеть ничего не отправляется:
- Claude: ~/.tollgate/claude-usage.json (пишет statusline.py) или кэш ~/.claude.json - что свежее
- Codex: последний ~/.codex/sessions/**/*.jsonl -> последнее событие с rate_limits
"""
__version__ = "0.2.3"

import base64
import ctypes
import ctypes.wintypes
import json
import math
import queue
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
STATE_FILE = TOLLGATE_DIR / "state.json"  # состояние виджета: пин, язык
REFRESH_MS = 30_000  # период обновления данных
TAIL_BYTES = 512 * 1024  # сколько читать с конца лог-файла Codex
MARGIN = 25  # отступ панели от краёв рабочей области, логические px

# светлая палитра
BG, FG, DIM, TRACK = "#ffffff", "#1d1d1f", "#86868b", "#ececf0"
GREEN, AMBER, RED = "#34c759", "#ff9f0a", "#ff3b30"
DOTS = {"Claude": "#d97757", "Codex": "#10a37f"}  # цветные точки у имени провайдера
THRESHOLDS = (80, 95)  # при каких % заполнения окна показывать уведомление
ALERT_BG = {AMBER: "#fff4e0", RED: "#ffeceb"}  # фон подсвеченной строки по цвету полоски
# шрифт значков Windows: (файл, контурный пин, залитый пин, угол иглы в глифе - градусы против часовой от «вправо»)
PIN_FONTS = ((r"C:\Windows\Fonts\SegoeIcons.ttf", "\ue840", "\ue842", 225),  # Windows 11: пин наклонён
             (r"C:\Windows\Fonts\segmdl2.ttf", "\ue718", "\ue841", 180))  # Windows 10: пин лежит горизонтально
GEAR, REFRESH, CLOCK = "\ue713", "\ue72c", "\ue823"  # шестерёнка, обновление, часы - одинаковые в обоих шрифтах
BADGES = {"Claude": ("#fbeee8", "#b4583a"), "Codex": ("#e3f4ee", "#0b7d61")}  # плашка тарифа: фон, текст

# --- локализация -------------------------------------------------------------------------------
LANGS = {"ru": "Русский", "en": "English", "zh": "中文"}
STRINGS = {
    "ru": {"no_data": "нет данных", "ago_s": "{n}с", "ago_m": "{n}м",
           "ago_h": "{n}ч", "ago_d": "{n}д", "error": "ошибка: {e}", "reset": "сброшен", "left_dh": "{d}д {h}ч",
           "left_hm": "{h}ч {m}м", "left_m": "{m}м", "expired": "истекла {date}", "days": "{date} · {n}д",
           "no_limits": "нет данных о лимитах", "show": "Показать", "refresh": "Обновить", "quit": "Выход",
           "alert_title": "Tollgate: лимит заканчивается", "alert_reset": ", сброс через {left}",
           "language": "Язык", "win_5h": "5ч", "win_1d": "1д", "win_1w": "1н", "h": "ч", "date": "%d.%m"},
    "en": {"no_data": "no data", "ago_s": "{n}s", "ago_m": "{n}m",
           "ago_h": "{n}h", "ago_d": "{n}d", "error": "error: {e}", "reset": "reset", "left_dh": "{d}d {h}h",
           "left_hm": "{h}h {m}m", "left_m": "{m}m", "expired": "expired {date}", "days": "{date} · {n}d",
           "no_limits": "no limit data", "show": "Show", "refresh": "Refresh", "quit": "Quit",
           "alert_title": "Tollgate: limit running out", "alert_reset": ", resets in {left}",
           "language": "Language", "win_5h": "5h", "win_1d": "1d", "win_1w": "1w", "h": "h", "date": "%b %d"},
    "zh": {"no_data": "无数据", "ago_s": "{n}秒", "ago_m": "{n}分",
           "ago_h": "{n}时", "ago_d": "{n}天", "error": "错误: {e}", "reset": "已重置", "left_dh": "{d}天{h}小时",
           "left_hm": "{h}小时{m}分", "left_m": "{m}分", "expired": "已于 {date} 到期", "days": "{date} · {n}天",
           "no_limits": "无额度数据", "show": "显示", "refresh": "刷新", "quit": "退出",
           "alert_title": "Tollgate: 额度即将用完", "alert_reset": "，{left}后重置",
           "language": "语言", "win_5h": "5时", "win_1d": "1天", "win_1w": "1周", "h": "时", "date": "%m月%d日"},
}
LANG = "ru"  # текущий язык, задаётся из state.json и в настройках


def T(key, **kw):
    """Строка интерфейса на текущем языке."""
    return STRINGS[LANG][key].format(**kw)


def win_name(key):
    """Каноническое имя окна ('5h', '1w', '3h') -> подпись на текущем языке."""
    return STRINGS[LANG].get(f"win_{key}") or key.replace("h", T("h"))


def F(size, bold=False, lang=None):
    """Шрифт интерфейса: для китайского - Microsoft YaHei UI (в Segoe UI нет иероглифов)."""
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


def read_codex():
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
            windows = []
            for key in ("primary", "secondary"):
                w = rl.get(key)
                if w:
                    windows.append((window_label(w.get("window_minutes")), w.get("used_percent") or 0, w.get("resets_at")))
            ts = parse_iso(rec["timestamp"].replace("Z", "+00:00")) if rec.get("timestamp") else f.stat().st_mtime
            return windows, ts
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


def effective(windows):
    """Окна -> [(имя, % 0..100, текст до сброса или None, если окно уже сбросилось)]."""
    out = []
    for label, pct, reset in windows:
        if reset and reset < time.time():  # окно уже сбросилось, а свежих данных ещё нет
            out.append((label, 0, None))
        else:
            out.append((label, max(0, min(100, round(pct))), fmt_left(reset) if reset else ""))
    return out


def load(reader):
    """Безопасное чтение провайдера: (окна, время данных или текст ошибки)."""
    try:
        windows, ts = reader()
        return effective(windows), ts
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
def rounded_bar(pct, w, h, color, bg=BG):
    """Гладкая полоска со скруглёнными концами: рисуем в 4x и уменьшаем (антиалиасинг)."""
    k = 4
    img = Image.new("RGB", (w * k, h * k), bg)
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
    out = Image.new("RGB", img.size, bg)
    out.paste(img, mask=img)
    return out.resize((size, size), Image.LANCZOS)


def pin_image(pinned, size, bg=BG):
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


def glyph_image(glyph, color, size, bg=BG):
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
            top = 60 - max(16, 56 * p / 100)  # минимум - кружок, чтобы цвет был виден и при 0%
            d.rounded_rectangle([x0, top, x0 + 20, 60], radius=8, fill=bar_color(p))
    return img


def work_area():
    """Рабочая область главного монитора (без панели задач), физические px."""
    r = ctypes.wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA
    return r.right, r.bottom


# --- окно --------------------------------------------------------------------------------------
def row_style(pct):
    """Цвет полоски, подсвечена ли строка, фон строки - по проценту заполнения окна."""
    color = bar_color(pct)
    hot = pct >= THRESHOLDS[0]
    return color, hot, ALERT_BG[color] if hot else BG


class Widget:
    PROVIDERS = (("Claude", read_claude, read_claude_plan), ("Codex", read_codex, read_codex_plan))
    ANIM_FRAMES, ANIM_MS = 12, 25  # анимация полоски: ~0.3 с

    def __init__(self):
        global LANG
        self.root = tk.Tk()
        self.root.withdraw()  # на старте показываем только иконку в трее
        self.root.overrideredirect(True)  # без рамки и заголовка
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG)
        self.s = self.root.winfo_fpixels("1i") / 96  # коэффициент масштабирования экрана
        self.card = None  # рамка с содержимым: строится один раз, дальше обновляется на месте
        self.layout_key = None  # структура панели - пересборка только при её изменении
        self.refs = {}  # ссылки на элементы, которые меняются при обновлении
        self.rows = []  # строки полосок (для отмены анимаций при пересборке)
        self.statuses = {}  # время данных по провайдерам - для ежесекундного тика возраста
        self.tray_state = None  # что сейчас нарисовано в трее - не дёргать иконку без изменений
        self.placed = None  # размер, под который окно уже прижато к углу
        self.visible = False
        self.hidden_at = 0.0
        self.rounded = False
        self.auto_shown = False  # панель открыта уведомлением, а не пользователем
        self.alerted = {}  # (провайдер, окно) -> последний порог, о котором уже уведомили
        self.view = "limits"  # что показывает панель: limits или settings
        self.limits_size = None  # размер панели лимитов - настройки открываются в том же размере
        self.ip = self.px(4)  # общий внутренний отступ слева и справа: по нему выровнены все строки
        self.bar_w = self.px(150)  # ширина полоски, подгоняется под ширину заголовков
        self.heads = []  # строки-заголовки (шапка, провайдеры) - по ним считается ширина панели
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        self.pinned = state.get("pinned", False)
        LANG = state.get("lang") if state.get("lang") in LANGS else "ru"
        self.root.bind("<Escape>", lambda e: self.hide())
        self.root.bind("<FocusOut>", self._on_focus_out)

        # трей живёт в своём потоке, команды в tk передаются через очередь
        self.cmds = queue.Queue()
        self.icon = pystray.Icon("tollgate", tray_image([None, None]), "Tollgate", self._menu())
        threading.Thread(target=self.icon.run, daemon=True).start()
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
            pystray.MenuItem(T("refresh"), lambda: self.cmds.put(self.refresh)),
            pystray.MenuItem(T("quit"), lambda: self.cmds.put(self.quit)),
        )

    def px(self, v):
        return int(v * self.s)

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
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        if self.placed == (w, h):
            return
        right, bottom = work_area()
        self.root.geometry(f"{w}x{h}+{right - w - self.px(MARGIN)}+{bottom - h - self.px(MARGIN)}")
        self.placed = (w, h)

    def show(self, focus=True):
        self.visible = True
        self.auto_shown = not focus
        self.view = "limits"  # открытая заново панель всегда начинается с лимитов
        self.placed = None  # рабочая область могла измениться - прижать заново
        self.refresh()
        self.root.deiconify()
        if not self.rounded:  # скругление окна средствами Windows 11 (DWMWA_WINDOW_CORNER_PREFERENCE = ROUND)
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            pref = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
            self.rounded = True
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
            STATE_FILE.write_text(json.dumps({"pinned": self.pinned, "lang": LANG}), encoding="utf-8")
        except OSError:
            pass  # не сохранилось - настройки работают до перезапуска

    def toggle_pin(self):
        self.pinned = not self.pinned
        self.auto_shown = False
        self._save_state()
        self._set_image(self.refs["pin"], pin_image(self.pinned, self.px(16)))  # только значок, без пересборки

    def toggle_settings(self):
        self.view = "limits" if self.view == "settings" else "settings"
        self.auto_shown = False
        self.refresh()

    def set_lang(self, code):
        global LANG
        LANG = code
        self._save_state()
        self.icon.menu = self._menu()
        self.icon.update_menu()
        self.refresh()

    def _auto_hide(self):
        if self.auto_shown and not self.pinned:
            self.hide()

    def _check_alerts(self, data):
        """Уведомление Windows и показ панели, когда окно лимита переходит порог 80% / 95%."""
        if not self.icon.visible:  # иконка трея ещё не поднялась - уведомить нечем, проверим в следующий раз
            return
        hot = []
        for name, windows, _, _ in data:
            for label, pct, left in windows:
                key = (name, label)
                level = max((t for t in THRESHOLDS if pct >= t), default=0)
                if level > self.alerted.get(key, 0):
                    hot.append(f"{name} {win_name(label)}: {pct}%" + (T("alert_reset", left=left) if left else ""))
                self.alerted[key] = level  # после сброса окна уровень падает и уведомление сработает снова
        if hot:
            try:
                self.icon.notify("\n".join(hot), T("alert_title"))
            except Exception:
                pass  # уведомления могут быть отключены в Windows - панель всё равно откроется
            if not self.visible:
                self.show(focus=False)
                self.root.after(10_000, self._auto_hide)

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
        self._icon_button(bar, glyph_image(REFRESH, DIM, self.px(13)), self.refresh, gap=0)  # клик - обновить вручную
        tk.Frame(self.card, bg=TRACK, height=1).grid(row=1, column=0, columnspan=4, sticky="ew", pady=(self.px(8), self.px(10)))
        return 2  # следующая свободная строка грида

    def _section(self, row, title, windows, age, plan):
        """Заголовок провайдера и его полоски; ссылки на изменяемые элементы - в self.refs[title]."""
        ref = self.refs[title] = {"rows": {}}
        head = tk.Frame(self.card, bg=BG)
        head.grid(row=row, column=0, columnspan=4, sticky="ew", padx=self.ip, pady=(0, self.px(6)))
        self.heads.append(head)
        tk.Label(head, text="●", bg=BG, fg=DOTS[title], font=F(9, lang="en")).pack(side="left")
        tk.Label(head, text=title, bg=BG, fg=FG, font=F(11, bold=True, lang="en")).pack(side="left", padx=(self.px(4), 0))
        name, until_text, _ = plan
        if name:
            bg, fg = BADGES[title]
            ref["badge"] = tk.Label(head, bg=bg, fg=fg, font=F(8, bold=True, lang="en"), padx=self.px(6))
            ref["badge"].pack(side="left", padx=(self.px(8), 0))
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
        for label, pct, _ in windows:
            pad, ip = (0, self.px(4)), self.ip  # внешний отступ между строками, внутренний - для фона подсветки
            r = {"value": pct, "left_text": None, "anim": None,
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

    def _settings(self, row):
        """Настройки в том же окне: пока - язык интерфейса."""
        tk.Label(self.card, text=T("language"), bg=BG, fg=DIM, font=F(8)).grid(row=row, column=0, columnspan=4, sticky="w", padx=self.ip)
        langs = tk.Frame(self.card, bg=BG)
        langs.grid(row=row + 1, column=0, columnspan=4, sticky="w", padx=self.ip, pady=(self.px(6), 0))
        for code, title in LANGS.items():
            on = code == LANG
            b = tk.Label(langs, text=title, bg=FG if on else TRACK, fg=BG if on else FG, font=F(9, lang=code),
                         padx=self.px(12), pady=self.px(4), cursor="hand2")
            b.pack(side="left", padx=(0, self.px(6)))
            b.bind("<Button-1>", lambda e, c=code: self.set_lang(c))

    def _rebuild(self, data):
        """Полная сборка панели - только при смене структуры (вид, язык, набор окон)."""
        for r in self.rows:  # анимации старых строк больше некуда рисовать
            if r["anim"]:
                self.root.after_cancel(r["anim"])
        self.refs, self.rows, self.heads = {}, [], []
        self.bar_w = self.px(150)
        # собираем новую рамку, пока старая на экране, и подменяем одним шагом
        old_card = self.card
        self.card = tk.Frame(self.root, bg=BG, padx=self.px(12), pady=self.px(14))
        row = self._titlebar()
        if self.view == "settings":
            self._settings(row)
            self.card.grid_columnconfigure(3, weight=1)  # шапка тянется на всю ширину фиксированного окна
            if self.limits_size:  # тот же размер, что у панели лимитов - окно не прыгает
                self.card.configure(width=self.limits_size[0], height=self.limits_size[1])
                self.card.grid_propagate(False)
        else:
            self._grid_columns()
            for i, (name, windows, age, plan) in enumerate(data):
                if i:
                    tk.Frame(self.card, bg=TRACK, height=1).grid(row=row, column=0, columnspan=4, sticky="ew", pady=(self.px(4), self.px(10)))
                    row += 1
                row = self._section(row, name, windows, age, plan)
            self._fit_bars()
        if old_card:
            old_card.pack_forget()
            old_card.destroy()
        self.card.pack()

    def _grid_columns(self):
        """Ширина колонок по самому длинному возможному тексту - при обновлениях сетка не гуляет."""
        def width(font, *texts):
            f = tkfont.Font(root=self.root, font=font)
            return max(f.measure(t) for t in texts) + 2 * self.ip
        self.colmin = [
            width(F(9, bold=True), *(win_name(k) for k in ("5h", "1d", "1w"))),  # подпись окна (жирная при подсветке)
            0,  # полоска - своей картинкой
            width(F(9, bold=True, lang="en"), "100%"),
            width(F(8), T("left_dh", d=6, h=23), T("left_hm", h=23, m=59), T("reset")),  # время до сброса
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
        r["left"].configure(bg=bg, fg=color if hot else DIM)

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
            if "badge" in ref:
                self._set_text(ref["badge"], plan_name)
            if "until" in ref:
                self._set_text(ref["until"], until_text, fg=until_color)
            for label, pct, left in windows:
                r = ref["rows"][label]
                left = T("reset") if left is None else left
                if left != r["left_text"]:
                    r["left"].configure(text=left)
                    r["left_text"] = left
                if pct != r["value"]:
                    if r["anim"]:
                        self.root.after_cancel(r["anim"])
                    start, r["value"] = r["value"], pct
                    self._animate(r, start, pct)

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
            return "settings", LANG
        return "limits", LANG, tuple((n, tuple(l for l, _, _ in w), bool(p[0]), bool(p[1]), isinstance(a, str))
                                     for n, w, a, p in data)

    def refresh(self):
        if getattr(self, "_job", None):  # ручное обновление не должно плодить таймеры
            self.root.after_cancel(self._job)
        data = [(name, *load(reader), load_plan(plan)) for name, reader, plan in self.PROVIDERS]
        self.statuses = {name: st for name, _, st, _ in data}
        self.statuses["freshest"] = max((st for _, _, st, _ in data if isinstance(st, (int, float))), default=None)
        key = self._layout_key(data)
        if key != self.layout_key:
            self._rebuild(data)
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
        pcts = [max((p for _, p, _ in w), default=None) for _, w, _, _ in data]
        tip = "\n".join(f"{n}: " + (", ".join(f"{win_name(l)} {p}%" for l, p, _ in w) or T("no_data")) for n, w, _, _ in data)
        if (pcts, tip) != self.tray_state:
            self.icon.icon = tray_image(pcts)
            self.icon.title = tip[:127]  # лимит длины подсказки в Windows
            self.tray_state = (pcts, tip)
        if self.visible:  # размер мог измениться - заново прижать к углу
            self._place()
        self._job = self.root.after(REFRESH_MS, self.refresh)
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
