"""Tollgate - лимиты Claude Code и Codex для Windows: иконка в трее + всплывающая панель.

Данные читаются только локально, в сеть ничего не отправляется:
- Claude: ~/.tollgate/claude-usage.json (пишет statusline.py) или кэш ~/.claude.json - что свежее
- Codex: последний ~/.codex/sessions/**/*.jsonl -> последнее событие с rate_limits
"""
__version__ = "0.1.1"

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
from datetime import datetime
from pathlib import Path

import pystray
from PIL import Image, ImageDraw, ImageFont, ImageTk

HOME = Path.home()
CLAUDE_JSON = HOME / ".claude.json"
CODEX_SESSIONS = HOME / ".codex" / "sessions"
TOLLGATE_DIR = HOME / ".tollgate"
CLAUDE_STATUSLINE = TOLLGATE_DIR / "claude-usage.json"  # пишет statusline.py
STATE_FILE = TOLLGATE_DIR / "state.json"  # состояние виджета (пин)
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
BADGES = {"Claude": ("#fbeee8", "#b4583a"), "Codex": ("#e3f4ee", "#0b7d61")}  # плашка тарифа: фон, текст


def window_label(minutes):
    """Человекочитаемое имя окна лимита по его длине в минутах."""
    if minutes == 10080:
        return "неделя"
    return f"{minutes // 60}ч" if minutes else "?"


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
    for key, label in (("five_hour", "5ч"), ("seven_day", "неделя")):
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
    for key, label in (("five_hour", "5ч"), ("seven_day", "неделя")):
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


def fmt_left(reset_ts):
    """Сколько осталось до сброса: '2д 3ч', '1ч 12м', '5м'."""
    sec = int(reset_ts - time.time())
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    if d:
        return f"{d}д {h}ч"
    return f"{h}ч {m}м" if h else f"{m}м"


def fmt_age(ts):
    """Возраст данных: 'сейчас', '5 мин назад', '3 ч назад'."""
    if not ts:
        return "нет данных"
    mins = int((time.time() - ts) // 60)
    if mins < 1:
        return "сейчас"
    return f"{mins} мин назад" if mins < 60 else f"{mins // 60} ч назад"




def bar_color(p):
    return GREEN if p < 50 else AMBER if p < 80 else RED


def effective(windows):
    """Окна -> [(имя, % 0..100, текст до сброса)] с учётом уже сбросившихся окон."""
    out = []
    for label, pct, reset in windows:
        if reset and reset < time.time():  # окно уже сбросилось, а свежих данных ещё нет
            out.append((label, 0, "сброшен"))
        else:
            out.append((label, max(0, min(100, round(pct))), fmt_left(reset) if reset else ""))
    return out


def load(reader):
    """Безопасное чтение провайдера: (окна, подпись возраста данных)."""
    try:
        windows, ts = reader()
        return effective(windows), fmt_age(ts)
    except Exception as ex:  # битый/отсутствующий файл не должен ронять виджет
        return [], f"ошибка: {type(ex).__name__}"


def load_plan(reader):
    """Безопасное чтение тарифа: (название, текст про срок, цвет текста) или Nones."""
    try:
        plan, until, exact = reader()
    except Exception:  # нет файла/полей - просто не показываем тариф
        return None, None, None
    if not until:
        return plan, None, None
    days = math.ceil((until - time.time()) / 86400)
    date = datetime.fromtimestamp(until).strftime("%d.%m")
    if days <= 0:
        return plan, f"подписка истекла {date}", RED
    text = f"подписка до {date} · {days} дн" if exact else f"≈ продление {date} · {days} дн"
    return plan, text, AMBER if days <= 3 else DIM


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


def pin_image(pinned, size, bg=BG):
    """Значок пина: откреплён - контур с наклоном 45°, закреплён - заливка, игла вертикально вниз."""
    k = 4  # рисуем крупно и уменьшаем - гладкие края после поворота
    img = Image.new("RGBA", (size * k, size * k), (0, 0, 0, 0))
    for path, outline, filled, needle in PIN_FONTS:
        try:
            font = ImageFont.truetype(path, int(size * k * 0.8))
        except OSError:
            continue
        d = ImageDraw.Draw(img)
        # залитый глиф - только «головка» без иглы, поэтому кладём его поверх контура
        for glyph in (outline, filled) if pinned else (outline,):
            d.text((size * k / 2, size * k / 2), glyph, font=font, anchor="mm", fill=FG if pinned else DIM)
        # 225° = игла влево-вниз (наклон), 270° = вниз (воткнут); rotate() крутит против часовой
        img = img.rotate((270 if pinned else 225) - needle, resample=Image.BICUBIC)
        break
    out = Image.new("RGB", img.size, bg)
    out.paste(img, mask=img)
    return out.resize((size, size), Image.LANCZOS)


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


class Widget:
    PROVIDERS = (("Claude", read_claude, read_claude_plan), ("Codex", read_codex, read_codex_plan))

    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()  # на старте показываем только иконку в трее
        self.root.overrideredirect(True)  # без рамки и заголовка
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG)
        self.s = self.root.winfo_fpixels("1i") / 96  # коэффициент масштабирования экрана
        self.card = tk.Frame(self.root, bg=BG, padx=self.px(18), pady=self.px(14))
        self.card.pack()
        self.images = []  # держим ссылки на PhotoImage, иначе tk их выбросит
        self.visible = False
        self.hidden_at = 0.0
        self.rounded = False
        self.auto_shown = False  # панель открыта уведомлением, а не пользователем
        self.alerted = {}  # (провайдер, окно) -> последний порог, о котором уже уведомили
        try:
            self.pinned = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("pinned", False)
        except (OSError, ValueError):
            self.pinned = False
        self.root.bind("<Escape>", lambda e: self.hide())
        self.root.bind("<FocusOut>", self._on_focus_out)

        # трей живёт в своём потоке, команды в tk передаются через очередь
        self.cmds = queue.Queue()
        self.icon = pystray.Icon("tollgate", tray_image([None, None]), "Tollgate", pystray.Menu(
            pystray.MenuItem(f"Tollgate {__version__}", None, enabled=False),  # версия - видно, у кого какая сборка
            pystray.MenuItem("Показать", lambda: self.cmds.put(self.toggle), default=True),
            pystray.MenuItem("Обновить", lambda: self.cmds.put(self.refresh)),
            pystray.MenuItem("Выход", lambda: self.cmds.put(self.quit)),
        ))
        threading.Thread(target=self.icon.run, daemon=True).start()
        self._poll()
        self.refresh()
        if self.pinned:  # закреплённая панель видна сразу после запуска
            self.show(focus=False)

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
        """Прижать панель к правому нижнему углу рабочей области с отступом MARGIN."""
        self.root.update_idletasks()
        right, bottom = work_area()
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        self.root.geometry(f"+{right - w - self.px(MARGIN)}+{bottom - h - self.px(MARGIN)}")

    def show(self, focus=True):
        self.visible = True
        self.auto_shown = not focus
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

    def toggle_pin(self):
        self.pinned = not self.pinned
        self.auto_shown = False
        try:
            TOLLGATE_DIR.mkdir(exist_ok=True)
            STATE_FILE.write_text(json.dumps({"pinned": self.pinned}), encoding="utf-8")
        except OSError:
            pass  # не сохранилось - пин работает до перезапуска
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
                    hot.append(f"{name} {label}: {pct}%" + (f", сброс через {left}" if left and left != "сброшен" else ""))
                self.alerted[key] = level  # после сброса окна уровень падает и уведомление сработает снова
        if hot:
            try:
                self.icon.notify("\n".join(hot), "Tollgate: лимит заканчивается")
            except Exception:
                pass  # уведомления могут быть отключены в Windows - панель всё равно откроется
            if not self.visible:
                self.show(focus=False)
                self.root.after(10_000, self._auto_hide)

    def quit(self):
        self.icon.stop()
        self.root.destroy()

    def _titlebar(self):
        """Шапка: название и версия слева, время обновления и пин справа."""
        bar = tk.Frame(self.card, bg=BG)
        bar.grid(row=0, column=0, columnspan=4, sticky="ew")
        tk.Label(bar, text="Tollgate", bg=BG, fg=FG, font=("Segoe UI Semibold", 10)).pack(side="left")
        tk.Label(bar, text=f"v{__version__}", bg=BG, fg=DIM, font=("Segoe UI", 8)).pack(side="left", padx=(self.px(5), 0), pady=(self.px(2), 0))
        img = ImageTk.PhotoImage(pin_image(self.pinned, self.px(16)))
        self.images.append(img)
        pin = tk.Label(bar, image=img, bg=BG, bd=0, cursor="hand2")
        pin.pack(side="right", padx=(self.px(8), 0))
        pin.bind("<Button-1>", lambda e: self.toggle_pin())
        tk.Label(bar, text=f"обновлено {datetime.now():%H:%M}", bg=BG, fg=DIM, font=("Segoe UI", 8)).pack(side="right")
        tk.Frame(self.card, bg=TRACK, height=1).grid(row=1, column=0, columnspan=4, sticky="ew", pady=(self.px(8), self.px(10)))
        return 2  # следующая свободная строка грида

    def _section(self, row, title, windows, age, plan):
        """Заголовок провайдера и его полоски, возвращает следующую строку грида."""
        head = tk.Frame(self.card, bg=BG)
        head.grid(row=row, column=0, columnspan=4, sticky="ew", pady=(0, self.px(6)))
        tk.Label(head, text="●", bg=BG, fg=DOTS[title], font=("Segoe UI", 9)).pack(side="left")
        tk.Label(head, text=title, bg=BG, fg=FG, font=("Segoe UI Semibold", 11)).pack(side="left", padx=(self.px(4), 0))
        name, until_text, until_color = plan
        if name:
            bg, fg = BADGES[title]
            tk.Label(head, text=name, bg=bg, fg=fg, font=("Segoe UI Semibold", 8), padx=self.px(6)).pack(side="left", padx=(self.px(8), 0))
        tk.Label(head, text=age, bg=BG, fg=DIM, font=("Segoe UI", 8)).pack(side="right")
        row += 1
        if not windows:
            tk.Label(self.card, text="нет данных о лимитах", bg=BG, fg=DIM, font=("Segoe UI", 9)).grid(row=row, column=0, columnspan=4, sticky="w")
            row += 1
        for label, pct, left in windows:
            color = bar_color(pct)
            hot = pct >= THRESHOLDS[0]
            bg = ALERT_BG[color] if hot else BG
            img = ImageTk.PhotoImage(rounded_bar(pct, self.px(150), self.px(8), color, bg=bg))
            self.images.append(img)
            pad, ip = (0, self.px(4)), self.px(4)  # внешний отступ между строками, внутренний - для фона подсветки
            tk.Label(self.card, text=label, bg=bg, fg=color if hot else DIM, font=("Segoe UI Semibold" if hot else "Segoe UI", 9),
                     anchor="w", padx=ip, pady=ip).grid(row=row, column=0, sticky="nsew", pady=pad)
            tk.Label(self.card, image=img, bg=bg, bd=0, padx=self.px(10)).grid(row=row, column=1, sticky="nsew", pady=pad)
            tk.Label(self.card, text=f"{pct}%", bg=bg, fg=color if hot else FG, font=("Segoe UI Semibold", 9), width=4,
                     anchor="e").grid(row=row, column=2, sticky="nsew", pady=pad)
            tk.Label(self.card, text=left, bg=bg, fg=color if hot else DIM, font=("Segoe UI", 8), width=7, anchor="e",
                     padx=ip).grid(row=row, column=3, sticky="nsew", pady=pad)
            row += 1
        if until_text:
            tk.Label(self.card, text=until_text, bg=BG, fg=until_color, font=("Segoe UI", 8)).grid(row=row, column=0, columnspan=4, sticky="w")
            row += 1
        return row

    def refresh(self):
        if getattr(self, "_job", None):  # ручное обновление не должно плодить таймеры
            self.root.after_cancel(self._job)
        data = [(name, *load(reader), load_plan(plan)) for name, reader, plan in self.PROVIDERS]
        # панель
        for w in self.card.winfo_children():
            w.destroy()
        self.images.clear()
        row = self._titlebar()
        for i, (name, windows, age, plan) in enumerate(data):
            if i:
                tk.Frame(self.card, bg=TRACK, height=1).grid(row=row, column=0, columnspan=4, sticky="ew", pady=(self.px(4), self.px(10)))
                row += 1
            row = self._section(row, name, windows, age, plan)
        # трей: иконка по максимальному окну каждого провайдера + подсказка с цифрами
        self.icon.icon = tray_image([max((p for _, p, _ in w), default=None) for _, w, _, _ in data])
        tip = "\n".join(f"{n}: " + (", ".join(f"{l} {p}%" for l, p, _ in w) or "нет данных") for n, w, _, _ in data)
        self.icon.title = tip[:127]  # лимит длины подсказки в Windows
        if self.visible:  # высота могла измениться - заново прижать к углу
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
            print(name, *load(reader), load_plan(plan))
    elif single_instance():
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # чёткий текст на экранах с масштабом >100%
        Widget().root.mainloop()
