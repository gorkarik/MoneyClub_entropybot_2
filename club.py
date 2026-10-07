#!/usr/bin/env python3
"""MoneyClub_entropybot_2 — меню управления ботом.

Запуск: команда `moneyclub` (ставится install.sh) или `venv/bin/python club.py`.

Entropy — всегда одна нога; вторая — Lighter RH (Robinhood chain) или Lighter
(core), выбирается при Старте вместе с тикером. У каждой пары «биржа + тикер»
свои настройки в tickers/<биржа>/<ТИКЕР>.yaml
(пороги, комиссии, лимиты позиции и сделки, файл минутных данных).
Бот работает в фоновой tmux-сессии, поэтому его не останавливает ни закрытие
терминала, ни обрыв SSH. Пользователь tmux не видит: всё через пункты меню.
"""
from __future__ import annotations

import getpass
import math
import os
import re
import shlex
import signal
import subprocess
import sys
import time

APP_DIR = os.path.dirname(os.path.realpath(__file__))
os.chdir(APP_DIR)
sys.path.insert(0, APP_DIR)

try:
    import yaml
    from rich.console import Console
    from rich.text import Text
except ImportError:
    print("Не найдены библиотеки меню (rich / PyYAML).\n"
          "Запустите установку ещё раз той же командой, которой ставили бота.")
    sys.exit(1)

# ---------------------------------------------------------------- константы

# Биржи хеджа (вторая нога; Entropy — всегда первая) и тикеры каждой.
# Lighter RH и Lighter core — разные биржи: свои аккаунты, ключи, стаканы
# и цены. Поэтому настройки — у каждой пары «биржа + тикер» свои.
VENUES = ["lighter-rh", "lighter", "tradexyz"]
VENUE_TITLES = {"lighter-rh": "Lighter RH", "lighter": "Lighter",
                "tradexyz": "trade.xyz"}
VENUE_TICKERS = {
    "lighter-rh": ["SNDK", "ANTH", "OAI"],
    "lighter": ["SNDK", "ANTH", "OAI", "NBIS", "DRAM", "EWY"],
    # trade.xyz — тоже Hyperliquid (dex xyz). Какие из тикеров там есть,
    # меню проверяет по живому списку рынков перед выбором.
    "tradexyz": ["SNDK", "ANTH", "OAI", "NBIS", "DRAM", "EWY"],
}
VENUE_BOOKS_URL = {
    "lighter-rh": "https://api.rh.lighter.xyz/api/v1/orderBooks",
    "lighter": "https://mainnet.zklighter.elliot.ai/api/v1/orderBooks",
}
HL_DEX = {"tradexyz": "xyz"}          # биржи хеджа на Hyperliquid: имя dex
# комиссия тейкера биржи хеджа для новых пар: у Lighter 0 (документация),
# у trade.xyz — стандартное значение, на живой сделке не измерено
DEFAULT_HEDGE_FEE_BPS = {"tradexyz": 1.0}

# Начальные варианты имени рынка (Entropy, Lighter). Это только заготовка:
# при первом выборе пары она записывается в tickers/<биржа>/<ТИКЕР>.yaml и
# дальше правится там. Lighter называет часть активов иначе, поэтому для
# него — варианты через запятую: бот берёт первый, который есть на бирже.
TICKER_SEED = {
    "SNDK": ("SNDK", "SNDK"),
    "ANTH": ("ANTH", "ANTHROPIC, ANTH"),
    "OAI": ("OAI", "OPENAI, OAI"),
    "NBIS": ("NBIS", "NBIS"),
    "DRAM": ("DRAM", "DRAM"),
    "EWY": ("EWY", "EWY"),
}
# имена на trade.xyz: тикер, затем полное имя — бот берёт первое найденное
XYZ_SEED = {"ANTH": "ANTH, ANTHROPIC", "OAI": "OAI, OPENAI"}
LEGACY_PAIR = ("lighter-rh", "SNDK")   # до выбора пар бот торговал только ею
TICKERS_DIR = "tickers"
CURRENT_PAIR_FILE = os.path.join(TICKERS_DIR, ".current")
HL_INFO_URL = "https://api.hyperliquid.xyz/info"

VENV_PY = os.path.join(APP_DIR, "venv", "bin", "python")
PY = VENV_PY if os.path.exists(VENV_PY) else sys.executable

CONFIG = "config.yaml"
CONFIG_EXAMPLE = "config.example.yaml"
ENV_FILE = ".env"
ENV_EXAMPLE = ".env.example"
LOG_DIR = "logs"
STDERR_LOG = os.path.join(LOG_DIR, "stderr.log")

APP_NAME = "MoneyClub_entropybot_2"
SUBTITLE = "entropy bot 2"        # мелкая подпись рядом с заставкой


def _tmux_name() -> str:
    """Имя tmux-сокета: у обычной установки (папка money-club) прежнее,
    у второй копии на том же сервере — своё, чтобы Старт одного бота не
    закрывал сессию другого."""
    base = os.path.basename(APP_DIR.rstrip("/")) or "moneyclub"
    if base in ("money-club", "moneyclub"):
        return "moneyclub"
    return "moneyclub-" + re.sub(r"[^A-Za-z0-9_-]", "", base).lower()


TMUX_SOCKET = TMUX_SESSION = _tmux_name()
TMUX_CONF = os.path.join(APP_DIR, ".moneyclub-tmux.conf")

DEFAULT_THRESHOLDS = {"midline_bps": -7.0, "upper_bps": 4.0, "lower_bps": 4.5}
DEFAULT_POSITION_USD = 25.0
DEFAULT_ORDER_USD = 20.0
DEFAULT_MAX_LOSS_PCT = 0.0        # стоп по убытку по умолчанию выключен
DEFAULT_ENTROPY_FEE_BPS = 0.0     # по умолчанию комиссия в расчёт не входит
FEE_FULL_BPS = 0.86               # списывается при сделке (измерено, Tier 1)
FEE_T1_NET_BPS = 0.17             # остаток после возврата 80% (уровень 1)
# варианты комиссии Entropy в расчёте: (bps, название, пояснение)
FEE_CHOICES = [
    (0.0, "Не учитывать (по умолчанию)",
     "больше сделок; комиссия списывается, но потом возвращается"),
    (FEE_T1_NET_BPS, "Учитывать остаток после возврата",
     "уровень 1: возвращается 80%, в расчёте остаются 20%"),
    (FEE_FULL_BPS, "Учитывать полностью",
     "меньше сделок; каждая с полным запасом на комиссию"),
]
TRADES_CSV = os.path.join(LOG_DIR, "trades.csv")

# Параметры исполнения — общие для всех тикеров (config.yaml). Для каждого:
# секция, ключ, значение по умолчанию, единица, короткое пояснение
# (в списке), подробное (при изменении), допустимый диапазон (мин, макс,
# включая ли мин).
EXEC_PARAMS = [
    ("execution", "premium_persist_sec", 0.3, "с",
     "сколько секунд сигнал держится перед входом",
     "Выгодная премия должна продержаться столько секунд, прежде чем бот "
     "войдёт. Отсекает случайные скачки цены, которые исчезают раньше, чем "
     "исполнится вторая нога. Больше — реже входы, но меньше проскальзывание.",
     (0.0, 60.0, True)),
    ("execution", "cooldown_sec", 0.0, "с",
     "минимальная пауза между сделками",
     "После сделки бот ждёт столько секунд, прежде чем искать следующую. "
     "0 — без паузы.",
     (0.0, 3600.0, True)),
    ("sizing", "take_fraction", 0.5, "",
     "какую долю выгодного стакана брать за сделку",
     "Какую часть объёма стакана, на которой есть выгода, забирать одной "
     "сделкой: 0.5 — половину, 1 — весь. Чем больше, тем глубже бот лезет в "
     "стакан и тем хуже средняя цена.",
     (0.0, 1.0, False)),
    ("inventory", "scale_bps", 10.0, "bps",
     "надбавка к порогу при наборе позиции",
     "Когда позиция растёт в одну сторону, бот требует для следующего входа "
     "в ту же сторону больше премии. Столько bps добавится при позиции на "
     "весь лимит. Выход из позиции надбавкой не штрафуется. 0 — выключить.",
     (0.0, 1000.0, True)),
    ("inventory", "floor_frac", 0.5, "",
     "с какой доли лимита начинается надбавка",
     "С какой доли лимита позиции начинает действовать надбавка: 0.5 — после "
     "половины лимита, 0 — с первого доллара позиции (строже).",
     (0.0, 1.0, True)),
    ("execution", "leg_slippage_bps", 50.0, "bps",
     "защита цены каждой ноги",
     "Ордер каждой ноги не исполнится хуже плановой цены больше чем на столько. "
     "Слишком мало — ноги чаще не исполняются, и бот выравнивает позицию по "
     "рынку.",
     (0.0, 1000.0, False)),
    ("execution", "hedge_slippage_bps", 20.0, "bps",
     "защита цены при выравнивании позиции",
     "То же, но для выравнивания, когда ноги разошлись (одна исполнилась "
     "больше другой).",
     (0.0, 1000.0, False)),
    ("execution", "staleness_sec", 10.0, "с",
     "через сколько секунд данные биржи устарели",
     "Если стакан биржи не обновлялся дольше — данные считаются устаревшими "
     "и бот по ним не торгует.",
     (0.0, 600.0, False)),
]
STOP_WAIT_LIVE_SEC = 90           # закрытие позиций при Стопе + итог сессии

BANNER = r"""
 ███╗   ███╗ ██████╗ ███╗   ██╗███████╗██╗   ██╗
 ████╗ ████║██╔═══██╗████╗  ██║██╔════╝╚██╗ ██╔╝
 ██╔████╔██║██║   ██║██╔██╗ ██║█████╗   ╚████╔╝
 ██║╚██╔╝██║██║   ██║██║╚██╗██║██╔══╝    ╚██╔╝
 ██║ ╚═╝ ██║╚██████╔╝██║ ╚████║███████╗   ██║
 ╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═══╝╚══════╝   ╚═╝
        ██████╗██╗     ██╗   ██╗██████╗
       ██╔════╝██║     ██║   ██║██╔══██╗
       ██║     ██║     ██║   ██║██████╔╝
       ██║     ██║     ██║   ██║██╔══██╗
       ╚██████╗███████╗╚██████╔╝██████╔╝
        ╚═════╝╚══════╝ ╚═════╝ ╚═════╝
"""
BANNER_COMPACT = "\n  M O N E Y   C L U B"
BANNER_STYLE = "bold bright_blue"     # заставка синяя

TMUX_CONF_TEXT = """\
# Создаётся автоматически меню MoneyClub_entropybot_2 — вручную не редактировать.
set -g default-terminal "screen-256color"
set -g escape-time 0
set -g mouse off
set -g history-limit 2000
set -g status on
set -g status-style "bg=blue,fg=white,bold"
set -g status-left-length 120
set -g status-right ""
set -g status-left " ДАШБОРД · чтобы выйти в меню нажмите Q (бот продолжит работать) "
set -g window-status-format ""
set -g window-status-current-format ""
# выход из дашборда одной клавишей, на любой раскладке
bind-key -n q detach-client
bind-key -n Q detach-client
bind-key -n й detach-client
bind-key -n Й detach-client
bind-key -n C-c detach-client
bind-key -n C-d detach-client
"""

console = Console(highlight=False)


class Back(Exception):
    """Возврат на уровень выше (Ctrl+C или «0» внутри подменю)."""


# ------------------------------------------------------------------ ввод

def ask(prompt: str = "  › ") -> str:
    try:
        return input(prompt).strip()
    except KeyboardInterrupt:
        print()
        raise Back
    except EOFError:
        print()
        raise SystemExit(0)


def confirm(question: str, default: bool = True) -> bool:
    hint = "Enter — да, 0 — нет" if default else "1 — да, Enter — нет"
    while True:
        a = ask(f"  {question} [{hint}]: ").lower()
        if a == "":
            return default
        if a in ("1", "д", "да", "y", "yes"):
            return True
        if a in ("0", "н", "нет", "n", "no"):
            return False
        console.print("  [dim]Введите 1 (да) или 0 (нет).[/dim]")


_FLASH: list = []          # сообщения, которые покажет следующий экран меню
_SKIP_PAUSE = [False]      # после сохранения лишний Enter не нужен


def flash(text: str) -> None:
    """Короткое сообщение («✔ Сохранено»), которое появится наверху
    следующего экрана — вместо отдельного «Нажмите Enter»."""
    _FLASH.append(text)
    _SKIP_PAUSE[0] = True


def pause() -> None:
    if _SKIP_PAUSE[0]:
        _SKIP_PAUSE[0] = False
        return
    ask("\n  Нажмите Enter, чтобы вернуться в меню… ")


def ask_number(label: str, current: float, money: bool = False) -> float:
    """Число с клавиатуры; Enter — оставить текущее. Запятая тоже подходит."""
    cur = fmt(current, money)
    while True:
        a = ask(f"  {label}: сейчас {cur}.\n    Введите новое число или "
                f"нажмите Enter, чтобы оставить {cur}: ")
        if a == "":
            return current
        try:
            return float(a.replace(",", ".").replace(" ", ""))
        except ValueError:
            console.print("  [dim]Нужно число, например 4.5 или -7[/dim]")


def fmt(v, whole_as_int: bool = False) -> str:
    v = float(v)
    if v == 0:
        v = 0.0
    if whole_as_int and v.is_integer():
        return str(int(v))
    s = f"{v:.4f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def usd(v) -> str:
    return "$" + fmt(v, whole_as_int=True)


# ------------------------------------------------------------ экран

def clear() -> None:
    console.clear()


def print_banner() -> None:
    """Синяя заставка MONEY CLUB; мелкая подпись «entropy bot 2» — справа от
    последней строки букв."""
    wide = console.width >= 52
    lines = (BANNER if wide else BANNER_COMPACT).rstrip("\n").split("\n")
    for i, line in enumerate(lines):
        t = Text(line, style=BANNER_STYLE)
        if i == len(lines) - 1:
            t.append("  " + SUBTITLE, style="dim cyan")
        console.print(t)
    console.print()


def header(title: str = "") -> None:
    clear()
    print_banner()
    if title:
        console.print(f"  {title}\n", style="bold")
    _SKIP_PAUSE[0] = False
    while _FLASH:
        console.print("  " + _FLASH.pop(0))
        if not _FLASH:
            console.print()


def menu(items, back_label: str = "Назад") -> str:
    for key, label in items:
        console.print(f"  {key}  {label}", markup=False)
    console.print(f"  0  {back_label}", markup=False)
    console.print()
    return ask()


# ------------------------------------------------------------- файлы

def ensure_files() -> bool:
    """Создаёт недостающие файлы. Возвращает True при самом первом запуске."""
    os.makedirs(LOG_DIR, exist_ok=True)
    if not os.path.exists(CONFIG):
        with open(CONFIG_EXAMPLE, encoding="utf-8") as src, \
                open(CONFIG, "w", encoding="utf-8") as dst:
            dst.write(src.read())
    first = not os.path.exists(ENV_FILE)
    if first:
        _atomic_write(ENV_FILE, _env_template(), mode=0o600)
    try:
        os.chmod(ENV_FILE, 0o600)
    except OSError:
        pass
    ensure_ticker_files()
    old = ""
    if os.path.exists(TMUX_CONF):
        with open(TMUX_CONF, encoding="utf-8") as fh:
            old = fh.read()
    if old != TMUX_CONF_TEXT:
        _atomic_write(TMUX_CONF, TMUX_CONF_TEXT)
    return first


def _atomic_write(path: str, text: str, mode: int = None) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


# -------------------------------------------------------------- config.yaml

def read_config() -> dict:
    with open(CONFIG, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def pair_path(pair) -> str:
    venue, ticker = pair
    return os.path.join(TICKERS_DIR, venue, f"{ticker}.yaml")


def pair_list(venue: str) -> list:
    """Тикеры биржи: сначала заготовленные по порядку, затем добавленные
    вручную файлы tickers/<биржа>/*.yaml."""
    d = os.path.join(TICKERS_DIR, venue)
    seeded = VENUE_TICKERS.get(venue, [])
    out = [t for t in seeded if os.path.exists(pair_path((venue, t)))]
    try:
        extra = sorted(f[:-5] for f in os.listdir(d)
                       if f.endswith(".yaml") and not f.startswith("."))
    except FileNotFoundError:
        extra = []
    return out + [t for t in extra if t not in out]


def current_pair():
    """Пара, с которой работают меню, настройки и анализ: у запущенного из
    меню бота — его пара, иначе последняя выбранная."""
    for b in find_bots():
        if b.ours and b.ticker:
            return (b.venue, b.ticker)
    try:
        with open(CURRENT_PAIR_FILE, encoding="utf-8") as fh:
            raw = fh.read().strip()
        venue, ticker = raw.split("/", 1) if "/" in raw else (LEGACY_PAIR[0], raw)
        if os.path.exists(pair_path((venue, ticker))):
            return (venue, ticker)
    except (FileNotFoundError, ValueError):
        pass
    if os.path.exists(pair_path(LEGACY_PAIR)):
        return LEGACY_PAIR
    for venue in VENUES:
        lst = pair_list(venue)
        if lst:
            return (venue, lst[0])
    return LEGACY_PAIR


def set_current_pair(pair) -> None:
    os.makedirs(TICKERS_DIR, exist_ok=True)
    _atomic_write(CURRENT_PAIR_FILE, f"{pair[0]}/{pair[1]}\n")


def pair_label(pair) -> str:
    venue, ticker = pair
    return f"{ticker} · Entropy ↔ {VENUE_TITLES.get(venue, venue)}"


def _pair_text(pair, entropy_name: str, hedge_names: str, base: dict,
               legacy: bool) -> str:
    """Текст нового tickers/<биржа>/<ТИКЕР>.yaml."""
    venue, ticker = pair
    sec = lambda name: base.get(name) or {}  # noqa: E731
    ent, hed, siz = sec("entropy"), sec("hedge"), sec("sizing")
    fee = float(ent.get("taker_fee_bps", DEFAULT_ENTROPY_FEE_BPS))
    if legacy and base.get("thresholds"):
        thr = base["thresholds"]
        mid = float(thr.get("midline_bps", 0.0))
        up = float(thr.get("upper_bps", DEFAULT_THRESHOLDS["upper_bps"]))
        lo = float(thr.get("lower_bps", DEFAULT_THRESHOLDS["lower_bps"]))
        csv_path = (base.get("recorder") or {}).get("csv", "logs/minutes.csv")
    else:
        mid = 0.0
        up = DEFAULT_THRESHOLDS["upper_bps"]
        lo = DEFAULT_THRESHOLDS["lower_bps"]
        csv_path = f"logs/minutes_{venue}_{ticker}.csv"
    pos_e = float(ent.get("max_position_usd", DEFAULT_POSITION_USD))
    pos_h = float(hed.get("max_position_usd", pos_e))
    order = float(siz.get("max_order_notional_usd", DEFAULT_ORDER_USD))
    min_order = float(siz.get("min_order_notional_usd", 10.0))
    if venue in DEFAULT_HEDGE_FEE_BPS:
        # не берём hedge.taker_fee_bps из config.yaml: там 0 — это Lighter
        hfee = DEFAULT_HEDGE_FEE_BPS[venue]
        hfee_note = "комиссия trade.xyz, bps — стандартное значение, не измерена"
        hfee_line = ("  hedge_fee_checked: false   # комиссия trade.xyz "
                     "проверена на живой сделке по этой паре\n")
    else:
        hfee = float(hed.get("taker_fee_bps", 0.0))
        hfee_note = f"комиссия {VENUE_TITLES.get(venue, venue)}, bps"
        hfee_line = ""
    title = VENUE_TITLES.get(venue, venue)
    return f"""\
# MONEY CLUB · настройки пары {ticker} · Entropy ↔ {title}
# Меню меняет этот файл само. Пороги относятся только к этой паре: у разных
# тикеров и у разных бирж разная премия, переносить их нельзя.

ticker:
  entropy_symbol: "{entropy_name}"        # имя рынка на Entropy
  hedge_symbols: "{hedge_names}"        # имя на {title}; варианты через запятую, берётся первый найденный
{hfee_line}
thresholds:
  midline_bps: {fmt(mid)}         # центр премии; 0 — пара не откалибрована, торговля запрещена
  upper_bps: {fmt(up)}           # на сколько выше центра — продаём Entropy
  lower_bps: {fmt(lo)}           # на сколько ниже центра — покупаем Entropy

entropy:
  taker_fee_bps: {fmt(fee)}        # комиссия Entropy в расчёте входа, bps (0 — не учитывать; меню: Настройки → Комиссии)
  max_position_usd: {fmt(pos_e, True)}      # лимит позиции, $

hedge:
  taker_fee_bps: {fmt(hfee)}         # {hfee_note}
  max_position_usd: {fmt(pos_h, True)}      # лимит позиции, $ (меню держит равным Entropy)

sizing:
  max_order_notional_usd: {fmt(order, True)}   # максимум на одну сделку, $
  min_order_notional_usd: {fmt(min_order, True)}   # сделки меньше этой суммы не отправляются

recorder:
  csv: {csv_path}       # минутные данные этой пары (только её рынок)
"""


def _migrate_flat_ticker_files() -> None:
    """Предыдущая версия хранила tickers/<ТИКЕР>.yaml — только для Lighter RH.
    Переносим их в tickers/lighter-rh/ без изменений. Файлы тикеров, которых
    на RH нет (NBIS, DRAM), откладываем в tickers/_old/ — не удаляем."""
    try:
        names = [f for f in os.listdir(TICKERS_DIR)
                 if f.endswith(".yaml") and not f.startswith(".")]
    except FileNotFoundError:
        return
    for name in names:
        src = os.path.join(TICKERS_DIR, name)
        if not os.path.isfile(src):
            continue
        t = name[:-5]
        if t in VENUE_TICKERS["lighter-rh"]:
            dst_dir = os.path.join(TICKERS_DIR, "lighter-rh")
        else:
            dst_dir = os.path.join(TICKERS_DIR, "_old")
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, name)
        if os.path.exists(dst):
            continue
        os.replace(src, dst)


def ensure_ticker_files() -> None:
    """Создаёт недостающие tickers/<биржа>/<ТИКЕР>.yaml. Самая первая пара
    (SNDK на Lighter RH) получает пороги и файл данных из config.yaml — как
    бот торговал до сих пор; остальные — центр 0 (не откалиброваны)."""
    os.makedirs(TICKERS_DIR, exist_ok=True)
    _migrate_flat_ticker_files()
    for root, _dirs, files in os.walk(TICKERS_DIR):
        for name in files:
            # take_fraction стал общим параметром (config.yaml) — убрать его
            # из файлов, созданных ранней версией меню
            if not name.endswith(".yaml"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            cleaned = re.sub(r"(?m)^[ \t]+take_fraction\s*:.*\n", "", text)
            if cleaned != text:
                _atomic_write(path, cleaned)
    try:
        base = read_config()
    except Exception:
        base = {}
    for venue in VENUES:
        os.makedirs(os.path.join(TICKERS_DIR, venue), exist_ok=True)
        for t in VENUE_TICKERS[venue]:
            path = pair_path((venue, t))
            if os.path.exists(path):
                continue
            ent_name, hedge_names = TICKER_SEED[t]
            if venue in HL_DEX:
                hedge_names = XYZ_SEED.get(t, t)
            _atomic_write(path, _pair_text((venue, t), ent_name, hedge_names,
                                           base, legacy=((venue, t) ==
                                                         LEGACY_PAIR)))


def pair_raw(pair) -> dict:
    """config.yaml с настройками пары поверх (как их видит бот)."""
    from entropy_arb.config import merge_ticker_profile, read_ticker_profile
    prof = read_ticker_profile(pair_path(pair))
    merged = merge_ticker_profile(read_config(), prof)
    merged["ticker"] = prof.get("ticker") or {}
    return merged


def cfg_values(pair=None) -> dict:
    venue, t = pair or current_pair()
    c = pair_raw((venue, t))
    thr = c.get("thresholds") or {}
    info = c.get("ticker") or {}
    return {
        "ticker": t,
        "venue": venue,
        "pair": (venue, t),
        "entropy_symbol": str(info.get("entropy_symbol") or t),
        "hedge_symbols": str(info.get("hedge_symbols") or t),
        "fee_checked": bool(info.get("fee_checked", False)),
        # центр, поставленный вручную (автокалибровка ходит вокруг него)
        "midline_anchor": (float(info["midline_anchor_bps"])
                           if info.get("midline_anchor_bps") is not None
                           else None),
        "autocalib_last_ts": (float(info["autocalib_last_ts"])
                              if info.get("autocalib_last_ts") is not None
                              else None),
        "autocalib": bool((c.get("autocalib") or {}).get("enabled", False)),
        "autocalib_window": float((c.get("autocalib") or {}).get(
            "window_hours", 72.0)),
        "flatten_on_halt": bool((c.get("risk") or {}).get(
            "flatten_on_halt", False)),
        "exec_mode": str((c.get("execution") or {}).get("mode",
                                                        "simultaneous")),
        "ef_slip": float((c.get("execution") or {}).get(
            "entropy_first_slip_bps", 5.0)),
        "max_excess": float((c.get("execution") or {}).get(
            "max_excess_bps", 0.0)),
        "vol_narrow": float((c.get("execution") or {}).get(
            "volume_narrow_bps", 1.0)),
        "vol_cost": float((c.get("execution") or {}).get(
            "volume_max_cost_usd", 2.0)),
        "leg_slip": float((c.get("execution") or {}).get(
            "leg_slippage_bps", 50.0)),
        "slipgate": bool((c.get("slipgate") or {}).get("enabled", False)),
        # комиссия биржи хеджа проверена: у Lighter 0% по документации
        "hedge_fee_checked": (bool(info.get("hedge_fee_checked", False))
                              if venue in DEFAULT_HEDGE_FEE_BPS else True),
        "midline_bps": float(thr.get("midline_bps", 0.0)),
        "upper_bps": float(thr.get("upper_bps", 0.0)),
        "lower_bps": float(thr.get("lower_bps", 0.0)),
        "pos_entropy": float((c.get("entropy") or {}).get("max_position_usd", 0)),
        "pos_hedge": float((c.get("hedge") or {}).get("max_position_usd", 0)),
        "order": float((c.get("sizing") or {}).get("max_order_notional_usd", 0)),
        "min_order": float((c.get("sizing") or {}).get("min_order_notional_usd", 10)),
        "csv": (c.get("recorder") or {}).get("csv",
                                             f"logs/minutes_{venue}_{t}.csv"),
        # секции risk может не быть (config.yaml старше этой версии)
        "max_loss_pct": float((c.get("risk") or {}).get(
            "max_loss_pct", DEFAULT_MAX_LOSS_PCT)),
        "fee_entropy": float((c.get("entropy") or {}).get("taker_fee_bps", 0.0)),
        "fee_hedge": float((c.get("hedge") or {}).get("taker_fee_bps", 0.0)),
        "trades_csv": (c.get("logging") or {}).get("trades_csv", TRADES_CSV),
    }


def is_calibrated(v: dict) -> bool:
    return v["midline_bps"] != 0


# правка YAML с сохранением комментариев — общая с ботом (автокалибровка)
from entropy_arb.yamledit import set_value as _set_yaml_value  # noqa: E402


def _is_ticker_key(section: str, key: str) -> bool:
    from entropy_arb.config import _TICKER_SCHEMA
    return key in (_TICKER_SCHEMA.get(section) or {})


def save_config(changes, pair=None) -> None:
    """changes: [(section, key, value_str)]. Настройки рынка (пороги,
    комиссии, лимиты позиции и сделки) пишутся в файл тикера, общие (лимит
    убытка и т.п.) — в config.yaml. Проверяет так же, как бот при старте;
    если что-то не так — откатывает оба файла."""
    venue, t = pair or current_pair()
    changes = list(changes)
    mids = [val for sec, key, val in changes
            if sec == "thresholds" and key == "midline_bps"]
    if mids:
        # центр задан вручную: он же — «якорь» автокалибровки, и её таймер
        # начинается заново (не перебивать ручное решение в ближайшие часы)
        changes += [("ticker", "midline_anchor_bps", mids[-1]),
                    ("ticker", "autocalib_last_ts", f"{time.time():.0f}")]
    paths = {"base": CONFIG, "ticker": pair_path((venue, t))}
    original = {}
    for name, path in paths.items():
        with open(path, encoding="utf-8") as fh:
            original[name] = fh.read()
    text = dict(original)
    for section, key, value in changes:
        name = "ticker" if _is_ticker_key(section, key) else "base"
        text[name] = _set_yaml_value(text[name], section, key, value)
    for name, path in paths.items():
        if text[name] != original[name]:
            _atomic_write(path, text[name])
    try:
        from entropy_arb.config import load_config
        # record_only=True: проверяем формат и значения; что тикер ещё не
        # откалиброван — не ошибка для сохранения
        load_config(CONFIG, "/nonexistent-env", symbol=t, hedge_venue=venue,
                    record_only=True)
    except Exception as e:
        for name, path in paths.items():
            _atomic_write(path, original[name])
        raise RuntimeError(f"настройки не сохранены: {e}") from e


# --------------------------------------------------------------------- .env

# Ключи Hyperliquid — общие; у каждой биржи Lighter свои: Lighter RH и
# Lighter core — разные биржи, их ключи и номера аккаунтов не взаимозаменяемы.
COMMON_KEYS = ["HL_PRIVATE_KEY", "HL_ACCOUNT_ADDRESS"]
VENUE_KEYS = {
    "lighter-rh": ["LIGHTER_ACCOUNT_INDEX", "LIGHTER_API_KEY_INDEX",
                   "LIGHTER_API_PRIVATE_KEY"],
    "lighter": ["LIGHTER_CORE_ACCOUNT_INDEX", "LIGHTER_CORE_API_KEY_INDEX",
                "LIGHTER_CORE_API_PRIVATE_KEY"],
}
# trade.xyz — тоже Hyperliquid: без своих ключей бот торгует ногой trade.xyz
# с ключами Entropy (один кошелёк, общий лимит запросов). Отдельный кошелёк
# — по желанию: ключ и адрес задаются только парой.
XYZ_KEYS = ["HL_PRIVATE_KEY_XYZ", "HL_ACCOUNT_ADDRESS_XYZ"]
OPTIONAL_KEYS = set(XYZ_KEYS)
VENUE_KEYS["tradexyz"] = XYZ_KEYS
ENV_KEYS = (COMMON_KEYS + VENUE_KEYS["lighter-rh"] + VENUE_KEYS["lighter"]
            + XYZ_KEYS)
SECRET_KEYS = {"HL_PRIVATE_KEY", "LIGHTER_API_PRIVATE_KEY",
               "LIGHTER_CORE_API_PRIVATE_KEY", "HL_PRIVATE_KEY_XYZ"}

KEY_INFO = {
    "HL_PRIVATE_KEY": (
        "Приватный ключ API (agent) кошелька Hyperliquid",
        "app.hyperliquid.xyz/API → ключ, который показали один раз при создании. "
        "Формат: 0x + 64 символа."),
    "HL_ACCOUNT_ADDRESS": (
        "Адрес ОСНОВНОГО кошелька (не агента)",
        "Адрес кошелька, на котором лежат деньги на Hyperliquid. "
        "Формат: 0x + 40 символов."),
    "LIGHTER_ACCOUNT_INDEX": (
        "Номер аккаунта на Lighter RH (Robinhood chain)",
        "Число — номер вашего аккаунта на бирже."),
    "LIGHTER_API_KEY_INDEX": (
        "Индекс API-ключа Lighter RH",
        "Короткое число, которое вы выбрали при создании ключа "
        "(например 4). Это НЕ Public Key."),
    "LIGHTER_API_PRIVATE_KEY": (
        "Приватный ключ API Lighter RH",
        "robinhoodchain.lighter.xyz/apikeys → Private Key (обычно 80 символов)."),
    "LIGHTER_CORE_ACCOUNT_INDEX": (
        "Номер аккаунта на Lighter (core)",
        "Число — номер вашего аккаунта на app.lighter.xyz. Это ДРУГОЙ аккаунт, "
        "не тот, что на Lighter RH."),
    "LIGHTER_CORE_API_KEY_INDEX": (
        "Индекс API-ключа Lighter (core)",
        "Короткое число, которое вы выбрали при создании ключа на "
        "app.lighter.xyz (например 4). Это НЕ Public Key."),
    "LIGHTER_CORE_API_PRIVATE_KEY": (
        "Приватный ключ API Lighter (core)",
        "app.lighter.xyz → API keys → Private Key (обычно 80 символов). "
        "Ключ от Lighter RH сюда не подойдёт."),
    "HL_PRIVATE_KEY_XYZ": (
        "Приватный ключ API (agent) ОТДЕЛЬНОГО кошелька для trade.xyz — "
        "необязательно",
        "Не заполняйте — нога trade.xyz пойдёт с ключами Entropy (это тот же "
        "Hyperliquid). Отдельный кошелёк нужен, чтобы у него был свой лимит "
        "запросов Hyperliquid. Ключ создаётся на app.hyperliquid.xyz/API "
        "ВТОРОГО кошелька. Формат: 0x + 64 символа. Заполняется вместе с "
        "адресом ниже."),
    "HL_ACCOUNT_ADDRESS_XYZ": (
        "Адрес ОСНОВНОГО отдельного кошелька для trade.xyz — необязательно",
        "Адрес второго кошелька, на котором лежат деньги для trade.xyz (не "
        "адрес агента). Формат: 0x + 40 символов. Заполняется только вместе "
        "с ключом выше."),
}

HEX = re.compile(r"^[0-9a-fA-F]+$")


def read_env() -> dict:
    out = {}
    if not os.path.exists(ENV_FILE):
        return out
    with open(ENV_FILE, encoding="utf-8") as fh:
        for line in fh:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[k.strip()] = v
    return out


def _env_template() -> str:
    """Шаблон .env; если .env.example потерялся — минимальный встроенный."""
    if os.path.exists(ENV_EXAMPLE):
        with open(ENV_EXAMPLE, encoding="utf-8") as fh:
            return fh.read()
    return "# MONEY CLUB — ключи. Заполняются через меню.\n" + \
        "".join(f"{k}=\n" for k in ENV_KEYS)


def write_env(updates: dict) -> None:
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = _env_template()
    lines = text.split("\n")
    done = set()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*([A-Z_][A-Z0-9_]*)\s*=", line)
        if m and m.group(1) in updates:
            lines[i] = f"{m.group(1)}={updates[m.group(1)]}"
            done.add(m.group(1))
    for k, v in updates.items():
        if k not in done:
            lines.append(f"{k}={v}")
    _atomic_write(ENV_FILE, "\n".join(lines).rstrip("\n") + "\n", mode=0o600)


def validate_key(name: str, raw: str):
    """Возвращает (нормализованное_значение, ошибка_или_None, мягкое_предупреждение)."""
    v = raw.strip().strip("\"'").replace(" ", "")
    # ключи Lighter core проверяются по тем же правилам, что и RH, ключи
    # trade.xyz — по правилам Hyperliquid
    name = name.replace("LIGHTER_CORE_", "LIGHTER_")
    if name.endswith("_XYZ"):
        name = name[:-4]
    if name == "HL_PRIVATE_KEY":
        if HEX.match(v) and len(v) == 64:
            v = "0x" + v
        if not (v.startswith("0x") and len(v) == 66 and HEX.match(v[2:])):
            return v, (f"Приватный ключ должен быть 0x + 64 символа, а у вас "
                       f"{len(v)} символов."), None
        return v, None, None
    if name == "HL_ACCOUNT_ADDRESS":
        if HEX.match(v) and len(v) == 40:
            v = "0x" + v
        if not (v.startswith("0x") and len(v) == 42 and HEX.match(v[2:])):
            hint = (" Похоже, вы вставили приватный ключ вместо адреса."
                    if len(v) in (64, 66) else "")
            return v, (f"Адрес должен быть 0x + 40 символов, а у вас "
                       f"{len(v)} символов.{hint}"), None
        return v, None, None
    if name == "LIGHTER_ACCOUNT_INDEX":
        if not v.isdigit():
            return v, "Номер аккаунта — это только цифры.", None
        return str(int(v)), None, None
    if name == "LIGHTER_API_KEY_INDEX":
        if not v.isdigit():
            hint = (" Похоже, это Public Key. Нужен индекс — короткое число, "
                    "которое вы выбрали при создании ключа (например 4)."
                    if len(v) > 10 else "")
            return v, "Индекс API-ключа — это короткое число." + hint, None
        n = int(v)
        if n > 255:
            return v, ("Индекс API-ключа не бывает больше 255. Возможно, это "
                       "номер аккаунта, а не индекс ключа."), None
        warn = (f"Индексы 0–2 у Lighter заняты сайтом и приложением; "
                f"обычно для бота берут 3 и выше." if n < 3 else None)
        return str(n), None, warn
    if name == "LIGHTER_API_PRIVATE_KEY":
        body = v[2:] if v.startswith("0x") else v
        if not body or not HEX.match(body):
            return v, "Приватный ключ Lighter состоит из символов 0-9 и a-f.", None
        warn = (None if len(body) == 80 else
                f"Обычно ключ Lighter — 80 символов, а у вас {len(body)}. "
                f"Проверьте, что это Private Key, а не Public Key.")
        return v, None, warn
    return v, None, None


def xyz_keys_mode(env: dict = None):
    """Ключи ноги trade.xyz: "shared" — оба пусты (ключи Entropy), "own" —
    заполнены оба и корректны, "broken" — один без другого или с ошибкой."""
    env = read_env() if env is None else env
    vals = [env.get(k, "") for k in XYZ_KEYS]
    if not any(vals):
        return "shared"
    if all(vals) and all(validate_key(k, env[k])[1] is None
                         for k in XYZ_KEYS):
        return "own"
    return "broken"


def keys_state(venue: str = None):
    """{имя: (заполнен, корректен)}, all_ok — для биржи venue (ключи
    Hyperliquid + ключи этой биржи хеджа); без venue — все ключи.
    Необязательные ключи trade.xyz: пустые — это не ошибка, но заполнять
    их можно только парой."""
    env = read_env()
    state = {}
    xyz = xyz_keys_mode(env)
    for k in (COMMON_KEYS + VENUE_KEYS[venue] if venue else ENV_KEYS):
        v = env.get(k, "")
        if k in OPTIONAL_KEYS:
            state[k] = (bool(v), xyz != "broken")
            continue
        if not v:
            state[k] = (False, False)
            continue
        _, err, _ = validate_key(k, v)
        state[k] = (True, err is None)
    return state, all(ok for _, ok in state.values())


def mask(name: str, value: str) -> str:
    if not value:
        return "не заполнен"
    if name in SECRET_KEYS:
        return f"••••••••{value[-4:]}  ({len(value)} симв.)"
    if name.startswith("HL_ACCOUNT_ADDRESS") and len(value) > 12:
        return f"{value[:6]}…{value[-4:]}"
    return value


def agent_address(private_key: str):
    try:
        from eth_account import Account
        return Account.from_key(private_key).address
    except Exception:
        return None


# ------------------------------------------------------------ процесс бота

class Bot:
    def __init__(self, pid: int, args: list, cwd: str):
        self.pid, self.args, self.cwd = pid, args, cwd
        self.record = "--record-only" in args
        self.ours = os.path.realpath(cwd) == APP_DIR
        self.ticker = (args[args.index("--symbol") + 1]
                       if "--symbol" in args[:-1] else "")
        self.venue = (args[args.index("--hedge") + 1]
                      if "--hedge" in args[:-1] else LEGACY_PAIR[0])

    @property
    def mode_label(self) -> str:
        return "ТЕСТОВАЯ ЗАПИСЬ" if self.record else "РЕЖИМ ТОРГОВЛИ"


def find_bots():
    """Все запущенные main.py — и наши, и запущенные вручную из другой папки
    (иначе можно случайно запустить второй бот на тех же ключах)."""
    bots = []
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) == os.getpid():
            continue
        try:
            with open(f"/proc/{d}/cmdline", "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        args = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        if len(args) < 2 or not os.path.basename(args[0]).startswith("python"):
            continue
        if not any(os.path.basename(a) == "main.py" for a in args[1:]):
            continue
        if "--symbol" not in args:
            continue
        try:
            cwd = os.readlink(f"/proc/{d}/cwd")
        except OSError:
            cwd = "?"
        bots.append(Bot(int(d), args, cwd))
    return bots


def halt_kind(bot: Bot):
    """None — торгует; "loss" — стоп по убытку; "errors" — аварийный стоп
    (бот жив, но не торгует). Смотрим только текущий запуск в логе."""
    path = os.path.join(bot.cwd, "logs", "engine.log")
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 512 * 1024))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return False
    start = max(tail.rfind("RECORD-ONLY —"), tail.rfind("LIVE — real orders"))
    if start < 0:
        return False
    rest = tail[start:]
    if "СТОП ПО УБЫТКУ" in rest or "HALTED (loss limit)" in rest:
        return "loss"
    if "HALTED after" in rest or "HALTED (" in rest:
        return "errors"
    return None


def is_halted(bot: Bot) -> bool:
    return halt_kind(bot) is not None


def tmux_env() -> dict:
    env = dict(os.environ)
    env.pop("TMUX", None)          # чтобы работало и изнутри другого tmux
    loc = env.get("LC_ALL") or env.get("LC_CTYPE") or env.get("LANG") or ""
    if "utf" not in loc.lower():
        env["LC_ALL"] = "C.UTF-8"  # иначе tmux покажет кириллицу как «___»
    env["PYTHONUTF8"] = "1"
    return env


def tmux(*args, **kw):
    return subprocess.run(["tmux", "-u", "-L", TMUX_SOCKET, "-f", TMUX_CONF,
                           *args], env=tmux_env(), **kw)


def tmux_has_session() -> bool:
    return tmux("has-session", "-t", TMUX_SESSION,
                capture_output=True).returncode == 0


def live_deps_ok() -> bool:
    r = subprocess.run([PY, "-c", "import lighter, hyperliquid, eth_account"],
                       capture_output=True)
    return r.returncode == 0


def explain_error(text: str) -> str:
    t = text.lower()
    if "credentials" in t:
        return "Не хватает ключей. Откройте Настройки → Ключи."
    if "no module named" in t or "requirements-live" in t:
        return ("Не установлены библиотеки для торговли. Запустите установку "
                "ещё раз той же командой.")
    if "not calibrated" in t:
        return ("Пара не откалибрована (центр = 0). Сначала тестовая запись, "
                "затем Анализ → Калибровка.")
    if "not found on" in t or ("not found" in t and ("io:" in t
                                                      or "xyz:" in t)):
        return ("Тикер не найден на одной из бирж. Имена рынков — в файле "
                "tickers/<биржа>/<ТИКЕР>.yaml.")
    if "delisted" in t:
        return "Рынок снят с торгов на Entropy или trade.xyz."
    if "hl_account_address_xyz" in t:
        return ("Адрес trade.xyz задан без ключа. Настройки → Ключи: "
                "заполните оба или очистите оба.")
    if "config error" in t:
        return "Ошибка в настройках (config.yaml)."
    if any(x in t for x in ("clientresponseerror", "clientconnectorerror",
                            "cannot connect", "timeout", "connection",
                            "forbidden", "service unavailable", "bad gateway")):
        return ("Нет связи с биржей или биржа отклонила запрос. "
                "Попробуйте ещё раз через минуту.")
    if "private key" in t or "invalid" in t or "signature" in t:
        return "Похоже, неверный ключ. Проверьте Настройки → Ключи."
    return "Бот завершился с ошибкой."


STDERR_MAX_BYTES = 10 * 1024 * 1024   # stderr.log: при запуске обрезается
STDERR_KEEP_BYTES = 1024 * 1024       # до последнего 1 МБ, если вырос больше


def trim_log(path: str, max_bytes: int = STDERR_MAX_BYTES,
             keep_bytes: int = STDERR_KEEP_BYTES) -> None:
    """Файл больше max_bytes — оставить только последние keep_bytes."""
    try:
        if os.path.getsize(path) <= max_bytes:
            return
        with open(path, "rb") as fh:
            fh.seek(-keep_bytes, os.SEEK_END)
            tail = fh.read()
        nl = tail.find(b"\n")
        _atomic_write(path, tail[nl + 1:].decode("utf-8", "replace"))
    except OSError:
        pass


def launch_bot(record: bool, pair=None) -> bool:
    """Запускает бота в фоновой tmux-сессии и проверяет, что он поднялся."""
    venue, ticker = pair or current_pair()
    if tmux_has_session():
        tmux("kill-session", "-t", TMUX_SESSION, capture_output=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    trim_log(STDERR_LOG)
    offset = os.path.getsize(STDERR_LOG) if os.path.exists(STDERR_LOG) else 0
    flags = (f"--symbol {shlex.quote(ticker)} --hedge {shlex.quote(venue)} "
             f"--ru")
    if record:
        flags += " --record-only"
    cmd = (f"cd {shlex.quote(APP_DIR)} && exec {shlex.quote(PY)} main.py "
           f"{flags} 2>>{shlex.quote(STDERR_LOG)}")
    r = tmux("new-session", "-d", "-s", TMUX_SESSION, "-x", "200", "-y", "50",
             cmd, capture_output=True, text=True)
    if r.returncode != 0:
        console.print(f"\n  [red]✘ Не удалось запустить tmux:[/red] "
                      f"{r.stderr.strip()}")
        return False
    alive = False
    with console.status("  Запускаю бота…"):
        for _ in range(20):             # ~10 секунд: загрузка рынков и ключей
            time.sleep(0.5)
            alive = any(b.ours for b in find_bots())
            if not alive:
                break
    if alive:
        console.print("\n  [green]✔ Бот запущен[/green] — "
                      f"{'тестовая запись' if record else 'режим торговли'}, "
                      f"{pair_label((venue, ticker))}.")
        return True
    err = ""
    if os.path.exists(STDERR_LOG):
        with open(STDERR_LOG, "rb") as fh:
            fh.seek(offset)
            err = fh.read().decode("utf-8", "replace").strip()
    console.print(f"\n  [red]✘ Бот не запустился.[/red] {explain_error(err)}")
    if err:
        # из трейсбека показываем только суть — последнюю строку с ошибкой
        lines = [l.strip() for l in err.splitlines() if l.strip()]
        errs = [l for l in lines if re.match(r"^[\w.]*(Error|Exception)\b", l)
                or l.startswith(("config error", "startup error"))]
        console.print("\n  Сообщение бота:", style="dim")
        console.print(f"    {(errs or lines)[-1]}", style="dim", markup=False,
                      soft_wrap=True)
        console.print(f"  [dim]Подробности: {STDERR_LOG}[/dim]")
    return False


def stop_bots(bots) -> bool:
    live = any(not b.record for b in bots)
    since = time.time()
    if live:
        console.print("  [yellow]Бот закрывает позиции на обеих биржах, "
                      "подождите — это может занять до минуты.\n  Не "
                      "закрывайте меню.[/yellow]\n")
    for b in bots:
        try:
            os.kill(b.pid, signal.SIGINT)   # мягкая остановка, как Ctrl+C
        except ProcessLookupError:
            pass
        except PermissionError:
            console.print(f"  [red]Нет прав остановить процесс {b.pid}.[/red]")
    pids = {b.pid for b in bots}

    def alive():
        return {p for p in pids if os.path.exists(f"/proc/{p}")}

    status = ("  Закрываю позиции и останавливаю бота…" if live
              else "  Останавливаю бота…")
    with console.status(status):
        deadline = time.time() + (STOP_WAIT_LIVE_SEC if live else 30)
        while alive() and time.time() < deadline:
            time.sleep(0.5)
        for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 2)):
            if not alive():
                break
            for p in alive():
                try:
                    os.kill(p, sig)
                except OSError:
                    pass
            deadline = time.time() + wait
            while alive() and time.time() < deadline:
                time.sleep(0.3)
    if tmux_has_session():
        tmux("kill-session", "-t", TMUX_SESSION, capture_output=True)
    if alive():
        console.print("  [red]✘ Не удалось остановить бота.[/red]")
        return False
    console.print("  [green]✔ Бот остановлен.[/green]")
    if live:
        show_session_report(since)
    return True


def _hms(sec) -> str:
    sec = int(float(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _f(row, key):
    try:
        return float(row.get(key) or "")
    except ValueError:
        return None


STOP_REASONS = {"manual": "вручную (Стоп)", "loss_limit": "СТОП ПО УБЫТКУ",
                "errors": "аварийный стоп (ошибки подряд)"}
HEDGE_TITLES = {"lighter": "Lighter", "lighter-rh": "Lighter RH",
                "tradexyz": "trade.xyz"}


def session_report_lines(row) -> list:
    pnl, pct = _f(row, "pnl_usd"), _f(row, "pnl_pct")
    s_eq, e_eq = _f(row, "start_equity"), _f(row, "end_equity")
    turn, fees = _f(row, "turnover_usd") or 0.0, _f(row, "fees_est_usd")
    bps = _f(row, "pnl_bps_of_turnover")
    hedge = HEDGE_TITLES.get(row.get("hedge_venue"), row.get("hedge_venue"))
    closed = {"1": "[green]закрыты[/green]",
              "0": "[red]НЕ ЗАКРЫЛИСЬ — проверьте биржи[/red]"}.get(
                  row.get("positions_closed"), "—")
    if pnl is None:
        res = "[dim]неизвестен (баланс не прочитан)[/dim]"
    else:
        col = "green" if pnl > 0 else ("red" if pnl < 0 else "white")
        res = f"[bold {col}]${pnl:+.4f}[/bold {col}]"
        if pct is not None:
            res += f" ({pct:+.2f}%)"
    reason = row.get("stop_reason") or ""
    extra = []
    unh, left = _f(row, "max_unhedged_usd"), _f(row, "dust_left_usd")
    if unh:
        extra.append(f"Остаток без хеджа во время сессии: до ${unh:.2f} "
                     f"[dim](меньше минимальной сделки)[/dim]")
    if left:
        extra.append(f"[yellow]Не закрыт остаток ${left:.2f} — меньше "
                     f"минимального ордера биржи. Закройте вручную на сайте "
                     f"биржи (кнопка Close у позиции).[/yellow]")
    exp, got = _f(row, "exp_edge_usd"), _f(row, "fill_edge_usd")
    if exp is not None and got is not None:
        from entropy_arb.telegram import money
        gap = got - exp
        extra.append(f"По сделкам: ожидалось {money(exp)}, получилось "
                     f"[{'green' if got >= 0 else 'red'}]{money(got)}[/], "
                     f"разница {money(gap)} [dim](ожидалось — расчёт бота в "
                     f"момент решения; разница — проскальзывание и "
                     f"задержка)[/dim]")
    f_ent, f_hed = _f(row, "funding_entropy_usd"), _f(row, "funding_hedge_usd")
    fund = lambda x: "[dim]нет данных[/dim]" if x is None else f"${x:+.4f}"  # noqa: E731
    if f_ent is not None or f_hed is not None:
        extra.append(
            f"Funding за сессию: Entropy {fund(f_ent)} · {hedge} {fund(f_hed)}"
            f" [dim](уже входит в результат; начисляется раз в час, поэтому в "
            f"коротких сессиях часто 0)[/dim]")
        if row.get("hedge_venue") in ("lighter", "lighter-rh"):
            extra.append("[dim]Знак funding у Lighter на живой бирже ещё не "
                         "проверен — сверьте с историей на сайте.[/dim]")
    return [
        f"Тикер: {row.get('symbol')} · биржи: Entropy ↔ {hedge}",
        f"Работал: {_hms(row.get('duration_sec') or 0)}",
        "Баланс бирж: " + (f"${s_eq:.2f} → ${e_eq:.2f}"
                           if s_eq is not None and e_eq is not None else "—"),
        f"Результат (PnL): {res}",
        f"Оборот: ${turn:,.2f} · сделок: {row.get('trades') or 0}",
        "Комиссии (примерно): " + (f"${fees:.4f}" if fees is not None
                                   else "—"),
        "Результат на оборот: " + (f"{bps:+.2f} bps" if bps is not None
                                   else "—"),
        "Остановка: " + STOP_REASONS.get(reason, reason or "—"),
        f"Позиции: {closed}",
    ] + extra


def show_session_report(since: float) -> None:
    """Итог сессии, которую бот записал при остановке."""
    from entropy_arb import journal
    try:
        row = journal.last_session(cfg_values()["trades_csv"])
    except Exception:
        row = None
    if not row or (_f(row, "end_ts") or 0) < since - 5:
        console.print("  [yellow]Итог сессии не записан — бот завершился "
                      "аварийно. Проверьте позиции на обеих биржах.[/yellow]")
        return
    console.print("\n  [bold]Итог сессии[/bold]")
    for line in session_report_lines(row):
        console.print("  " + line)
    console.print("  [dim]Комиссия — расчёт по ставкам из настроек, не "
                  "данные биржи.[/dim]")


# --------------------------------------------------------------- статус

def status_lines():
    bots = find_bots()
    if not bots:
        run = "[dim]○ ОСТАНОВЛЕН[/dim]"
    elif len(bots) > 1:
        run = f"[red]⚠ ЗАПУЩЕНО НЕСКОЛЬКО БОТОВ ({len(bots)}) — нажмите Стоп[/red]"
    else:
        b = bots[0]
        kind = halt_kind(b)
        if kind == "loss":
            run = ("[red]⚠ СТОП ПО УБЫТКУ — бот не торгует; нажмите Стоп, "
                   "чтобы увидеть итог[/red]")
        elif kind == "errors":
            run = ("[red]⚠ АВАРИЙНЫЙ СТОП — проверьте позиции, затем "
                   "Стоп и Старт[/red]")
        elif not b.ours:
            run = "[yellow]● РАБОТАЕТ · запущен не из меню[/yellow]"
        else:
            run = f"[green]● РАБОТАЕТ[/green] · {b.mode_label}"
    _, keys_ok = keys_state(current_pair()[0])
    try:
        v = cfg_values()
        loss_lim = (f"стоп-лосс {fmt(v['max_loss_pct'])}%"
                    if v["max_loss_pct"] > 0 else "[yellow]стоп-лосс выкл[/yellow]")
        calib = (f"центр {fmt(v['midline_bps'])} "
                 f"(+{fmt(v['upper_bps'])} / −{fmt(v['lower_bps'])})"
                 if is_calibrated(v) else "[yellow]не откалиброван[/yellow]")
        info = (f"ключи {'✔' if keys_ok else '✘ не настроены'} · "
                f"лимит {usd(v['pos_entropy'])} · {loss_lim} · {calib}\n  "
                f"{strategy_label(v['exec_mode'])}")
    except Exception:
        info = ("[red]настройки не читаются (config.yaml или файл пары в "
                "tickers/) — проверьте файл[/red]")
    return run, info


def main_screen() -> None:
    header()
    run, info = status_lines()
    console.print(f"  {pair_label(current_pair())}    {run}")
    console.print(f"  [dim]{info}[/dim]\n")


# ================================================================ ДЕЙСТВИЯ

# ------------------------------------------------- выбор тикера и рынки

def _http_json(url: str, payload: dict = None, timeout: float = 8.0):
    import json
    import urllib.request
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json",
                                 "User-Agent": "moneyclub"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def hl_dex_markets(dex: str) -> dict:
    """Рынки одного dex Hyperliquid: {имя без префикса: снят_с_торгов}."""
    meta = _http_json(HL_INFO_URL, {"type": "meta", "dex": dex})
    out = {}
    for a in meta.get("universe") or []:
        name = str(a.get("name", ""))
        out[name.split(":", 1)[-1]] = bool(a.get("isDelisted"))
    return out


def fetch_markets(venue: str):
    """(рынки Entropy {имя: снят_с_торгов}, активные рынки биржи хеджа,
    ошибка). Публичные данные бирж, ключи не нужны."""
    try:
        ent = hl_dex_markets("io")
        if venue in HL_DEX:
            hed = {n for n, gone in hl_dex_markets(HL_DEX[venue]).items()
                   if not gone}
        else:
            books = _http_json(VENUE_BOOKS_URL[venue]).get("order_books") or []
            hed = {str(b.get("symbol")) for b in books
                   if b.get("status") == "active"}
        return ent, hed, None
    except Exception as e:
        return None, None, e


def market_status(pair, ent, hed):
    """(можно_торговать, причина, имя_на_Entropy, имя_на_бирже_хеджа)."""
    from entropy_arb.config import split_names
    venue, ticker = pair
    v = cfg_values(pair)
    ent_names = split_names(v["entropy_symbol"]) or (ticker,)
    hed_names = split_names(v["hedge_symbols"]) or (ticker,)
    e_name = next((n for n in ent_names if n in ent and not ent[n]), None)
    h_name = next((n for n in hed_names if n in hed), None)
    if e_name is None:
        gone = any(n in ent for n in ent_names)
        return False, ("снят с торгов на Entropy" if gone
                       else "нет на Entropy"), None, h_name
    if h_name is None:
        return False, f"нет на {VENUE_TITLES.get(venue, venue)}", e_name, None
    return True, "", e_name, h_name


def choose_venue(title: str):
    """Нумерованный выбор биржи хеджа (вторая нога к Entropy)."""
    while True:
        header(title)
        for i, venue in enumerate(VENUES, 1):
            tick = ", ".join(VENUE_TICKERS[venue])
            console.print(f"  {i}  Entropy ↔ {VENUE_TITLES[venue]}  "
                          f"[dim]({tick})[/dim]")
        console.print("  0  Назад\n", markup=False)
        a = ask()
        if a == "0":
            return None
        if a.isdigit() and 1 <= int(a) <= len(VENUES):
            return VENUES[int(a) - 1]


def choose_pair(title: str, live: bool = False, check: bool = True):
    """Биржа, затем тикер этой биржи. Возвращает (пара, имя_Entropy,
    имя_на_бирже_хеджа) или None. check — проверить по биржам, что рынок
    есть на обеих."""
    while True:
        venue = choose_venue(f"{title} · биржа")
        if venue is None:
            return None
        picked = choose_ticker(f"{title} · {VENUE_TITLES[venue]} · тикер",
                               venue, live=live, check=check)
        if picked is not None:
            return picked


def choose_ticker(title: str, venue: str, live: bool = False,
                  check: bool = True):
    """Нумерованный выбор тикера одной биржи. None — назад к выбору биржи."""
    tickers = pair_list(venue)
    ent = hed = err = None
    if check:
        with console.status("  Проверяю рынки на биржах…"):
            ent, hed, err = fetch_markets(venue)
    while True:
        header(title)
        if check and err is not None:
            console.print("  [yellow]Не удалось проверить рынки — нет связи с "
                          "биржами. Есть ли тикер на обеих,\n  станет ясно "
                          "при запуске.[/yellow]\n")
        rows = {}
        for i, t in enumerate(tickers, 1):
            pair = (venue, t)
            note = ""
            ok, why, e_name, h_name = True, "", t, t
            try:
                if check and err is None:
                    ok, why, e_name, h_name = market_status(pair, ent, hed)
                calibrated = is_calibrated(cfg_values(pair))
            except Exception:
                ok, why, calibrated = False, "файл настроек с ошибкой", False
            if not ok:
                note = f"[dim]— {why}[/dim]"
                label = f"[dim]{i}  {t}[/dim]"
            else:
                label = f"{i}  {t}"
                if live and not calibrated:
                    note = "[yellow]— не откалиброван[/yellow]"
            rows[str(i)] = (pair, ok, why, e_name, h_name)
            console.print(f"  {label}  {note}")
        console.print("  0  Назад\n", markup=False)
        a = ask()
        if a == "0":
            return None
        if a not in rows:
            continue
        pair, ok, why, e_name, h_name = rows[a]
        if not ok:
            console.print(f"\n  [red]{pair[1]}: {why} — запустить нельзя."
                          f"[/red]")
            console.print(f"  [dim]Имена рынков на биржах — в файле "
                          f"{pair_path(pair)}.[/dim]")
            pause()
            continue
        return pair, e_name, h_name


def pair_line(pair, e_name: str = None, h_name: str = None) -> str:
    venue, t = pair
    return (f"Пара: [bold]ENTROPY {e_name or t} ↔ "
            f"{VENUE_TITLES.get(venue, venue).upper()} {h_name or t}[/bold]")


def action_start() -> None:
    bots = find_bots()
    if bots:
        header("Старт")
        where = "" if bots[0].ours else f" (запущен вручную из {bots[0].cwd})"
        console.print(f"  Бот уже работает{where}. Второй запускать нельзя — "
                      f"он торговал бы теми же ключами.\n"
                      f"  Сначала остановите его: пункт 2 — Стоп.")
        pause()
        return
    header("Старт")
    choice = menu([("1", "Режим торговли — реальные сделки"),
                   ("2", "Тестовая запись — без денег и без ключей")])
    if choice == "1":
        picked = choose_pair("Старт · торговля", live=True)
        if picked is None:
            return
        pair = picked[0]
        set_current_pair(pair)
        if not is_calibrated(cfg_values(pair)):
            header("Старт · режим торговли")
            console.print(f"  [yellow]{pair_label(pair)} ещё не откалибрована"
                          f"[/yellow] (центр = 0) —\n  торговать по ней "
                          f"нельзя.\n\n  Пороги привязаны к паре: у каждого "
                          f"рынка своя премия между биржами.\n  Сначала "
                          f"тестовая запись (без денег) несколько часов, "
                          f"затем\n  Анализ → Калибровка порогов.\n")
            if confirm("Запустить тестовую запись этой пары?"):
                if launch_bot(record=True, pair=pair):
                    offer_dashboard()
                else:
                    pause()
            return
        start_live(pair, picked[1], picked[2])
    elif choice == "2":
        picked = choose_pair("Старт · запись")
        if picked is None:
            return
        pair = picked[0]
        set_current_pair(pair)
        header(f"Старт · тестовая запись · {pair_label(pair)}")
        console.print("  Бот будет только записывать стаканы обеих бирж раз в "
                      "минуту — это данные для анализа.\n  Сделок нет, ключи "
                      "не нужны.\n")
        console.print(f"  [dim]Данные пишутся в {cfg_values(pair)['csv']}."
                      f"[/dim]\n")
        console.print("  " + pair_line(*picked))
        if confirm("Запустить?"):
            if launch_bot(record=True, pair=pair):
                offer_dashboard()
            else:
                pause()


def start_checklist(pair):
    """Печатает проверку перед стартом торговли, возвращает (ключи_ок, либы_ок)."""
    venue, ticker = pair
    state, keys_ok = keys_state(venue)
    v = cfg_values(pair)
    with console.status("  Проверяю…"):
        deps_ok = live_deps_ok()
    console.print(f"  Проверка перед стартом · {pair_label(pair)}:\n")
    if keys_ok:
        console.print("   [green]✔[/green] Ключи заполнены")
    else:
        bad = [k for k, (_, ok) in state.items() if not ok]
        console.print(f"   [red]✘[/red] Ключи не заполнены или с ошибкой: "
                      f"{', '.join(bad)}")
        if venue == "lighter":
            console.print("     [dim]Lighter core — отдельная биржа: нужен свой "
                          "аккаунт и API-ключ с app.lighter.xyz\n     (ключи "
                          "Lighter RH там не работают).[/dim]")
        if venue == "tradexyz" and xyz_keys_mode() == "broken":
            console.print("     [dim]Ключи trade.xyz: нужны оба — ключ и "
                          "адрес, или очистите оба (Настройки → Ключи).[/dim]")
    if venue == "tradexyz":
        mode = xyz_keys_mode()
        if mode == "own":
            console.print("   [green]✔[/green] trade.xyz — отдельный кошелёк, "
                          "свой лимит запросов Hyperliquid")
        elif mode == "shared":
            console.print("   [yellow]![/yellow] trade.xyz торгует с ключами "
                          "Entropy: лимит запросов Hyperliquid общий для\n     "
                          "обеих ног. Если он исчерпается, арбитраж встанет. "
                          "Отдельный кошелёк — Настройки → Ключи.")
        console.print("   [yellow]![/yellow] Деньги (USDC) для trade.xyz должны "
                      "лежать на dex trade.xyz — проверьте\n     в интерфейсе "
                      "Hyperliquid до первой сделки.")
    if v["midline_bps"] != 0:
        console.print(f"   [green]✔[/green] Калибровка: центр "
                      f"{fmt(v['midline_bps'])}, +{fmt(v['upper_bps'])} / "
                      f"−{fmt(v['lower_bps'])}")
    else:
        console.print("   [yellow]![/yellow] Калибровка не выполнена (центр = 0) "
                      "— рекомендуем пункт 4, Анализ")
    same = v["pos_entropy"] == v["pos_hedge"]
    console.print(f"   [green]✔[/green] Лимит позиции {usd(v['pos_entropy'])}"
                  f"{'' if same else ' / ' + usd(v['pos_hedge']) + ' (разные!)'}"
                  f", одна сделка до {usd(v['order'])}")
    console.print(f"   [green]✔[/green] Комиссия Entropy в расчёте "
                  f"{fmt(v['fee_entropy'])} bps ({fee_mode_label(v['fee_entropy'])})"
                  f" · {VENUE_TITLES.get(venue, venue)} {fmt(v['fee_hedge'])} bps")
    console.print(f"   [green]✔[/green] {strategy_label(v['exec_mode'])} · "
                  f"защита: {protection_summary(v)}")
    if not v["hedge_fee_checked"]:
        console.print(f"   [yellow]![/yellow] Комиссия {VENUE_TITLES[venue]} "
                      f"{fmt(v['fee_hedge'])} bps — стандартное значение, не "
                      f"измерена. Проверьте по\n     первой живой сделке и "
                      f"отметьте: Настройки → Комиссии")
    if v["max_loss_pct"] > 0:
        console.print(f"   [green]✔[/green] Стоп по убытку: "
                      f"{fmt(v['max_loss_pct'])}% от баланса на старте сессии")
    else:
        console.print("   [yellow]![/yellow] Стоп по убытку выключен — "
                      "Настройки → Стоп по убытку")
    if deps_ok:
        console.print("   [green]✔[/green] Библиотеки для торговли установлены")
    else:
        console.print("   [red]✘[/red] Не установлены библиотеки для торговли")
    console.print()
    return keys_ok, deps_ok


def start_live(pair=None, e_name: str = None, h_name: str = None) -> None:
    pair = pair or current_pair()
    while True:
        header(f"Старт · режим торговли · {pair_label(pair)}")
        keys_ok, deps_ok = start_checklist(pair)
        if not deps_ok:
            if confirm("Установить библиотеки сейчас? (1–3 минуты)"):
                install_live_deps()
                continue
            return
        if not keys_ok:
            if confirm("Перейти к настройке ключей?"):
                keys_menu()
                continue
            return
        if not previous_session_ok():
            return
        console.print("  " + pair_line(pair, e_name, h_name))
        if confirm("Запустить режим торговли?"):
            if launch_bot(record=False, pair=pair):
                offer_dashboard()
            else:
                pause()
        return


def install_live_deps() -> None:
    console.print()
    subprocess.call([PY, "-m", "pip", "install", "-r", "requirements-live.txt"])
    console.print()


def offer_dashboard() -> None:
    console.print()
    if confirm("Открыть дашборд? (выйти из него — клавиша Q)"):
        open_dashboard(skip_intro=True)


def action_stop() -> None:
    header("Стоп")
    bots = find_bots()
    if not bots:
        console.print("  Бот не запущен.")
        pause()
        return
    for b in bots:
        where = "" if b.ours else f" — запущен вручную из {b.cwd}"
        console.print(f"  Сейчас работает: {b.mode_label} · {b.ticker}{where}")
    if any(not b.record for b in bots):
        console.print("\n  [dim]Перед выключением бот закроет позиции на "
                      "обеих биржах и покажет итог сессии.[/dim]\n")
    else:
        console.print()
    if confirm("Остановить бота?"):
        stop_bots(bots)
        pause()


def real_tty():
    """Настоящий терминал (/dev/pts/N). Если меню запущено сразу после
    установки (curl … | bash), stdin — это /dev/tty, и tmux отказывается к
    нему подключаться («can't use /dev/tty»): дашборд молча не открывался."""
    for fd in (1, 2, 0):
        try:
            name = os.ttyname(fd)
        except OSError:
            continue
        if name != "/dev/tty":
            return name
    return None


def attach_dashboard():
    """Подключает терминал к дашборду. (успех, текст_ошибки)."""
    # привязка клавиши Q живёт в самом tmux-сервере: если сервер запущен
    # давно или без нашего конфига, Q не сработает — подгружаем конфиг заново
    tmux("source-file", TMUX_CONF, capture_output=True)
    path = real_tty()
    if path is None and not sys.stdin.isatty():
        return False, "нет подключённого терминала"
    tty = None
    try:
        tty = open(path, "r+b", buffering=0) if path else None
        r = tmux("attach-session", "-r", "-t", TMUX_SESSION,
                 stdin=tty if tty else None, stderr=subprocess.PIPE)
    finally:
        if tty:
            tty.close()
    err = (r.stderr or b"").decode("utf-8", "replace").strip()
    return r.returncode == 0, err


def open_dashboard(skip_intro: bool = False) -> None:
    bots = find_bots()
    if not bots:
        header("Дашборд")
        console.print("  Бот не запущен — смотреть пока нечего.\n"
                      "  Запустите его: пункт 1 — Старт.")
        pause()
        return
    if not tmux_has_session() or not any(b.ours for b in bots):
        header("Дашборд")
        console.print("  Бот запущен не из меню, поэтому его дашборд открывается "
                      "только там,\n  где его запускали. Чтобы видеть дашборд "
                      "отсюда: пункт 2 — Стоп, затем пункт 1 — Старт.")
        pause()
        return
    if not skip_intro:
        # без лишнего Enter: подсказка и секундная пауза, чтобы её прочитать
        header("Дашборд")
        console.print("  Открываю дашборд…  [bold]Выйти обратно в меню — "
                      "клавиша Q[/bold] (бот продолжит работать).")
        time.sleep(1.5)
    ok, err = attach_dashboard()
    if not ok:
        header("Дашборд")
        console.print("  [red]✘ Не удалось открыть дашборд.[/red]")
        if err:
            console.print(f"  [dim]{err}[/dim]")
        console.print("  Бот при этом работает. Попробуйте ещё раз; если не "
                      "выходит — закройте окно терминала\n  и зайдите на "
                      "сервер заново (команда moneyclub).")
        pause()


# ------------------------------------------------------------ анализ

ANALYSIS_PERIODS = {"1": (24, "последние 24 часа"),
                    "2": (48, "последние 48 часов"),
                    "3": (168, "последнюю неделю"), "4": (0, "всё время")}


def action_analysis() -> None:
    while True:
        pair = current_pair()
        header("Анализ")
        console.print(f"  Пара: [bold]{pair_label(pair)}[/bold]")
        console.print("  [dim]Калибровка и отчёт считаются по одной паре: у "
                      "каждой пары своя премия и свои сделки.[/dim]\n")
        choice = menu([("1", "Калибровка порогов — рынок и сделки бота, "
                             "рекомендация порогов"),
                       ("2", "Подробный отчёт для ИИ — всё, чтобы понять, "
                             "откуда убыток или прибыль"),
                       ("3", "Итоги по периодам — объём, заработок, убыток "
                             "за неделю, месяц, всё время"),
                       ("4", "Связь с биржами — замер задержки с этого "
                             "сервера"),
                       ("5", "Сменить пару — другая пара для Анализа, "
                             "Настроек и Старта")])
        if choice == "1":
            action_calibration()
        elif choice == "2":
            action_full_report()
        elif choice == "3":
            action_totals()
            continue
        elif choice == "4":
            action_latency()
            continue
        elif choice == "5":
            pick_pair_for_menus("Анализ · пара")
            continue
        return


TOTALS_PERIODS = {"1": (7, "неделя"), "2": (30, "месяц"),
                  "3": (0, "всё время")}


def action_totals() -> None:
    """Итоги по периодам: tools/totals.py по журналам бота."""
    v = cfg_values()
    header("Анализ · итоги по периодам")
    choice = menu([("1", f"Только эта пара — {pair_label(v['pair'])}"),
                   ("2", "Все пары вместе и по отдельности")])
    if choice not in ("1", "2"):
        return
    args = [PY, "tools/totals.py", "--dir",
            os.path.dirname(v["trades_csv"]) or LOG_DIR,
            "--legacy-symbol", LEGACY_PAIR[1],
            "--legacy-venue", LEGACY_PAIR[0]]
    if choice == "1":
        args += ["--symbol", v["ticker"], "--hedge-venue", v["venue"]]
    with console.status("  Считаю…"):
        r = subprocess.run(args, capture_output=True, text=True, timeout=300)
    out = (r.stdout + ("\n" + r.stderr if r.stderr.strip() else "")).strip()
    header("Анализ · итоги по периодам")
    print(out)
    print()
    pause()


def action_latency() -> None:
    """Замер связи: tools/latency.py — время ответа API бирж отсюда."""
    header("Анализ · связь с биржами")
    console.print("  [dim]Меряю, сколько миллисекунд идёт запрос с этого "
                  "сервера до каждой биржи и обратно\n  (по 5 замеров, "
                  "берётся медиана). Ключи не нужны, работающему боту не "
                  "мешает.[/dim]\n")
    with console.status("  Замеряю (до 30 секунд)…"):
        try:
            r = subprocess.run([PY, "tools/latency.py"], capture_output=True,
                               text=True, timeout=90)
            out = (r.stdout + ("\n" + r.stderr if r.stderr.strip()
                               else "")).strip()
        except subprocess.TimeoutExpired:
            out = "Замер не уложился в 90 секунд — сеть отвечает очень медленно."
    print(out)
    print()
    pause()


def pick_pair_for_menus(title: str) -> None:
    """Смена пары для Настроек и Анализа (биржи не опрашиваем)."""
    bots = [b for b in find_bots() if b.ours]
    if bots:
        header(title)
        console.print(f"  Бот работает с {pair_label((bots[0].venue, bots[0].ticker))}"
                      f" — настройки и анализ\n  показывают её. Другую пару "
                      f"можно выбрать после Стопа.")
        pause()
        return
    picked = choose_pair(title, check=False)
    if picked:
        set_current_pair(picked[0])


def report_args(v: dict) -> list:
    """Аргументы tools/report.py для выбранной пары."""
    return ["--symbol", v["ticker"], "--hedge-venue", v["venue"],
            "--legacy-symbol", LEGACY_PAIR[1],
            "--legacy-venue", LEGACY_PAIR[0],
            "--ticker-file", pair_path(v["pair"]), "--minutes", v["csv"]]


def action_full_report() -> None:
    v = cfg_values()
    header(f"Анализ · подробный отчёт · {pair_label(v['pair'])}")
    console.print("  За какой период?\n")
    choice = menu([(k, p[1].capitalize()) for k, p in ANALYSIS_PERIODS.items()])
    if choice not in ANALYSIS_PERIODS:
        return
    hours, label = ANALYSIS_PERIODS[choice]
    log_dir = os.path.dirname(v["trades_csv"]) or LOG_DIR
    with console.status("  Собираю отчёт…"):
        r = subprocess.run([PY, "tools/report.py", "--dir", log_dir,
                            "--config", CONFIG, "--hours", str(hours),
                            *report_args(v)],
                           capture_output=True, text=True, timeout=300)
    out = (r.stdout + ("\n" + r.stderr if r.stderr.strip() else "")).strip()
    header("Анализ · подробный отчёт")
    line = "─" * 22
    print(f"  {line} скопируйте отсюда {line}\n")
    print(f"Пара: {pair_label(v['pair'])} · период: {label}")
    print(out)
    print(f"\n  {line}──── до сюда ────{line}\n")
    console.print("  [bold]Выделите блок выше, скопируйте и отправьте любому "
                  "ИИ[/bold] — вопрос для него уже в конце отчёта.\n")
    pause()


def action_calibration() -> None:
    v = cfg_values()
    csv_path = v["csv"]
    t = pair_label(v["pair"])
    header(f"Анализ / калибровка · {t}")
    if not os.path.exists(csv_path):
        console.print(f"  Данных по {t} пока нет ({csv_path}). Бот записывает "
                      f"их в любом режиме —\n  запустите тестовую запись этой "
                      f"пары и вернитесь через несколько часов.")
        pause()
        return
    console.print("  За какой период посчитать?\n")
    periods = ANALYSIS_PERIODS
    choice = menu([(k, p[1].capitalize()) for k, p in periods.items()])
    if choice not in periods:
        return
    hours, label = periods[choice]
    with console.status("  Считаю…"):
        # комиссии тикера: рекомендация сразу учитывает их (как и бот)
        fees = v["fee_entropy"] + v["fee_hedge"]
        r = subprocess.run([PY, "tools/analyze.py", "--csv", csv_path,
                            "--hours", str(hours), "--fees-bps", fmt(fees)],
                           capture_output=True, text=True, timeout=300)
    out = (r.stdout + ("\n" + r.stderr if r.stderr.strip() else "")).strip()
    try:
        rs = subprocess.run([PY, "tools/report.py", "--dir",
                             os.path.dirname(v["trades_csv"]) or LOG_DIR,
                             "--config", CONFIG, "--hours", str(hours),
                             "--summary", *report_args(v)],
                            capture_output=True, text=True, timeout=120)
        trades_block = rs.stdout.strip()
    except Exception:
        trades_block = ""
    header(f"Анализ / калибровка · {t}")
    line = "─" * 22
    print(f"  {line} скопируйте отсюда {line}\n")
    print(f"MONEY CLUB · анализ {t} · период: {label}")
    print(f"Текущие настройки: midline {fmt(v['midline_bps'])} | upper "
          f"{fmt(v['upper_bps'])} | lower {fmt(v['lower_bps'])} | лимит позиции "
          f"{usd(v['pos_entropy'])} | сделка до {usd(v['order'])}")
    if trades_block:
        print(trades_block)
    print(out)
    print(f"\n  {line}──── до сюда ────{line}\n")
    console.print("  [bold]Выделите блок выше, скопируйте и отправьте Клоду "
                  "(claude.ai)[/bold]\n  с вопросом: «стоит ли обновить "
                  "калибровку бота?»\n")
    m = re.search(r"thresholds:\s*\n\s*midline_bps:\s*([-+\d.]+)\s*\n\s*"
                  r"upper_bps:\s*([-+\d.]+)\s*\n\s*lower_bps:\s*([-+\d.]+)", out)
    if not m or r.returncode != 0:
        pause()
        return
    hint = midline_hint(csv_path)
    sug = {"midline_bps": float(m.group(1)), "upper_bps": float(m.group(2)),
           "lower_bps": float(m.group(3))}
    cur = {k: v[k] for k in sug}
    loss, n_tr = exec_loss(v, hours)
    adj = exec_adjusted(sug, loss)
    console.print(f"  Сейчас:                 центр {fmt(cur['midline_bps'])}, "
                  f"+{fmt(cur['upper_bps'])} / −{fmt(cur['lower_bps'])}")
    console.print(f"  По рынку:               центр {fmt(sug['midline_bps'])}, "
                  f"+{fmt(sug['upper_bps'])} / −{fmt(sug['lower_bps'])}")
    if loss is None:
        console.print("\n  [yellow]Сделок по этой паре за период нет — "
                      "рекомендация не проверена на исполнении.[/yellow]\n"
                      "  [dim]Реальные потери на проскальзывании станут видны "
                      "после первых сделок.[/dim]\n")
        choices = {"2": sug}
    else:
        rough = " [yellow](мало сделок — оценка грубая)[/yellow]" \
            if n_tr < 20 else ""
        console.print(f"  С учётом исполнения:    центр {fmt(adj['midline_bps'])}"
                      f", +{fmt(adj['upper_bps'])} / −{fmt(adj['lower_bps'])}\n")
        console.print(f"  Потеря на исполнении: {fmt(round(loss, 2))} bps на "
                      f"сделку по {n_tr} сделкам этой пары{rough}.")
        width = cur["upper_bps"] + cur["lower_bps"]
        need = 2 * max(loss, 0.0)
        ok = width >= need
        console.print(f"  Ширина полосы сейчас: upper + lower = "
                      f"{fmt(round(width, 2))} bps; рекомендуемая — не меньше "
                      f"{fmt(round(need, 2))} bps "
                      + ("[green](хватает)[/green]" if ok else
                         "[yellow](уже — исполнение съедает выгоду круга)"
                         "[/yellow]"))
        if adj == sug:
            console.print("  [dim]Полоса по рынку уже шире этих потерь — "
                          "поправка не нужна.[/dim]\n")
            choices = {"1": sug}
        else:
            console.print(f"  [dim]Круг «вход + выход» — две сделки, значит "
                          f"полоса upper + lower должна быть\n  не уже "
                          f"{fmt(round(2 * loss, 2))} bps, иначе прибыль круга "
                          f"съедает исполнение. Сделок станет\n  меньше, "
                          f"но каждая — с запасом.[/dim]\n")
            choices = {"1": adj, "2": sug}
    # подсказка центра: только центр, upper и lower не меняются
    show_midline_hint(hint, cur["midline_bps"])
    for hours, med, _n in hint:
        key = {24: "3", 168: "4"}.get(hours)
        if key and med is not None and abs(med - cur["midline_bps"]) >= 0.05:
            choices[key] = {"midline_bps": med}
    items = []
    if "1" in choices:
        items.append(("1", "Применить с учётом исполнения (рекомендуется)"
                      if "2" in choices else "Применить рекомендацию"))
    if "2" in choices:
        items.append(("2", "Применить по рынку"))
    for key, hours in (("3", 24), ("4", 168)):
        if key in choices:
            label = "за 24 ч" if hours == 24 else "за 7 дней"
            items.append((key, f"Только центр {label}: "
                               f"{fmt(choices[key]['midline_bps'])} "
                               f"(upper и lower без изменений)"))
    if all(all(abs(c[k] - cur[k]) < 1e-9 for k in c)
           for c in choices.values()):
        console.print("  Рекомендованные пороги совпадают с текущими — менять "
                      "нечего.")
        pause()
        return
    pick = menu(items, back_label="Не менять")
    if pick in choices:
        apply_changes([("thresholds", k, fmt(choices[pick][k]))
                       for k in choices[pick]])
    pause()


def midline_hint(csv_path: str) -> list:
    """[(часы, центр или None, минут данных)] за 24 ч и за 7 дней."""
    try:
        r = subprocess.run([PY, "tools/analyze.py", "--csv", csv_path,
                            "--hint"], capture_output=True, text=True,
                           timeout=120)
    except Exception:
        return []
    out = []
    for m in re.finditer(r"MIDLINE_HINT (\d+) (\S+) (\d+)", r.stdout):
        med = None if m.group(2) == "none" else float(m.group(2))
        out.append((int(m.group(1)), med, int(m.group(3))))
    return out


def show_midline_hint(hint: list, current: float) -> None:
    """Подсказка центра: где премия была за сутки и за неделю. Решает
    пользователь, бот сам ничего не меняет."""
    if not hint:
        return
    console.print("  [bold]Подсказка центра[/bold] [dim](медиана премии; "
                  "применяете вы, бот сам не меняет)[/dim]")
    meds = {}
    for hours, med, n in hint:
        label = "за 24 ч:  " if hours == 24 else "за 7 дней:"
        if med is None:
            console.print(f"    {label} [dim]мало данных ({n} мин)[/dim]")
            continue
        meds[hours] = med
        diff = med - current
        mark = ("" if abs(diff) < 0.05 else
                f" [dim]({'+' if diff > 0 else '−'}{fmt(round(abs(diff), 1))} "
                f"от текущего)[/dim]")
        console.print(f"    {label} центр {fmt(med)}{mark} [dim]· {n} мин "
                      f"данных[/dim]")
    if 24 in meds and 168 in meds and abs(meds[24] - meds[168]) >= 1.5:
        console.print("    [yellow]За сутки центр ушёл от недельного на "
                      f"{fmt(round(meds[24] - meds[168], 1))} bps — премия "
                      "сдвинулась.[/yellow]\n    [dim]Сутки быстрее ловят "
                      "сдвиг, неделя устойчивее к шуму одного дня.[/dim]")
    console.print()


def exec_loss(v: dict, hours: float):
    """(потеря на исполнении, bps на сделку; число сделок) по сделкам
    выбранной пары за период, или (None, 0) если сделок нет."""
    try:
        r = subprocess.run([PY, "tools/report.py", "--dir",
                            os.path.dirname(v["trades_csv"]) or LOG_DIR,
                            "--config", CONFIG, "--hours", str(hours),
                            "--exec-loss", *report_args(v)],
                           capture_output=True, text=True, timeout=120)
        m = re.search(r"EXEC_LOSS (\S+) (\d+)", r.stdout)
        if not m or m.group(1) == "none":
            return None, 0
        return float(m.group(1)), int(m.group(2))
    except Exception:
        return None, 0


def exec_adjusted(sug: dict, loss) -> dict:
    """Пороги по рынку, расширенные так, чтобы круг «вход + выход» (две
    сделки) был не уже потери на исполнении двух сделок. Центр не меняется;
    upper и lower растут пропорционально, с округлением вверх до 0.5."""
    if loss is None or loss <= 0:
        return dict(sug)
    band = sug["upper_bps"] + sug["lower_bps"]
    need = 2 * loss
    if band >= need or band <= 0:
        return dict(sug)
    k = need / band
    up = math.ceil(sug["upper_bps"] * k * 2) / 2
    lo = math.ceil(sug["lower_bps"] * k * 2) / 2
    return {"midline_bps": sug["midline_bps"], "upper_bps": up,
            "lower_bps": lo}


# ------------------------------------------------------------ настройки

def apply_changes(changes) -> None:
    """Сохраняет изменения. Если бот работает — останавливает его, сохраняет и
    запускает снова в том же режиме (настройки на ходу бот не подхватывает)."""
    pair = current_pair()
    bots = find_bots()
    restart = None
    if bots:
        console.print("  Бот работает — новые настройки вступят в силу только "
                      "после перезапуска.\n  [dim]При остановке бот закроет "
                      "позиции; после запуска начнётся новая сессия.[/dim]")
        if not confirm("Остановить бота, сохранить и запустить снова?"):
            console.print("  Изменения не сохранены.")
            return
        restart = bots[0].record if all(b.ours for b in bots) else None
        if not stop_bots(bots):
            console.print("  Изменения не сохранены.")
            return
    try:
        save_config(changes, pair)
        per_pair = any(_is_ticker_key(sec, key) for sec, key, _ in changes)
        msg = ("[green]✔ Сохранено[/green]"
               + (f" ({pair_label(pair)})." if per_pair
                  else " (для всех пар)."))
        if restart is None:
            flash(msg)             # покажется наверху следующего экрана
        else:
            console.print("  " + msg)
    except Exception as e:
        console.print(f"  [red]✘ {e}[/red]")
    if restart is not None:
        if restart is False and not is_calibrated(cfg_values(pair)):
            console.print("  [yellow]Пара не откалибрована — режим торговли "
                          "не запускаю.[/yellow]")
            return
        launch_bot(record=restart, pair=pair)


def action_settings() -> None:
    bots = find_bots()
    resume_mode = None
    resume_pair = None
    if bots:
        header("Настройки")
        console.print("  Настройки меняются только при остановленном боте.\n"
                      "  [dim]При остановке бот закроет позиции и покажет "
                      "итог сессии.[/dim]\n")
        if not confirm("Остановить бота сейчас?"):
            return
        if all(b.ours for b in bots):
            resume_mode = bots[0].record
            resume_pair = (bots[0].venue, bots[0].ticker)
        if not stop_bots(bots):
            pause()
            return
    try:
        while True:
            pair = current_pair()
            try:
                v = cfg_values(pair)
            except Exception as e:
                # файл настроек испорчен: меню всё равно открывается, чтобы
                # можно было исправить ключи или сменить пару
                v = None
                from rich.markup import escape
                flash(f"[red]✘ Настройки пары не читаются: {escape(str(e))}"
                      f"[/red]")
            header("Настройки")
            console.print(f"  Пара: [bold]{pair_label(pair)}[/bold]\n")
            console.print("  [bold]Для этой пары[/bold] [dim]— у каждой пары "
                          "«биржа + тикер» свои[/dim]")
            items = [
                ("1", "Пороги входа — центр, выше, ниже"),
                ("2", "Лимиты позиции и сделки" + (
                    f" — сейчас {usd(v['pos_entropy'])} / {usd(v['order'])}"
                    if v else "")),
                ("3", "Комиссии" + (f" — Entropy в расчёте "
                                    f"{fmt(v['fee_entropy'])} bps" if v else "")),
                ("4", "Сменить пару — другая пара для Настроек, Анализа и "
                      "Старта"),
            ]
            for key, label in items:
                console.print(f"  {key}  {label}", markup=False)
            lim = v["max_loss_pct"] if v else 0
            common = [
                ("5", "Стратегия" + (f" — сейчас {strategy_label(v['exec_mode'])}"
                                     if v else "")),
                ("6", "Стоп по убытку" + ((" — " + (f"{fmt(lim)}% за сессию"
                                                    if lim > 0 else "выключен"))
                                          if v else "")),
                ("7", "Исполнение — параметры входа и цены"),
                ("8", "Ключи бирж (.env)"),
                ("9", "Telegram — уведомления и /status"),
                ("10", "Запись данных — частый срез стаканов"),
                ("11", "Автокалибровка центра" + (
                    (" — " + ("включена" if v["autocalib"] else "выключена"))
                    if v else "")),
                ("12", "Запросы Hyperliquid — проверить запас и докупить"),
            ]
            console.print("\n  [bold]Общие для всех пар[/bold]")
            choice = menu(common)
            actions = {"1": thresholds_menu, "2": limits_menu,
                       "3": fee_menu,
                       "4": lambda: pick_pair_for_menus("Настройки · пара"),
                       "5": strategy_menu, "6": loss_menu,
                       "7": execution_menu, "8": keys_menu,
                       "9": telegram_menu, "10": ticks_menu,
                       "11": autocalib_menu, "12": hl_requests_menu}
            try:
                if choice == "0":
                    break
                fn = actions.get(choice)
                if fn is not None:
                    fn()
            except Back:
                continue
    except Back:
        pass
    if resume_mode is not None:
        header("Настройки")
        mode = "тестовой записи" if resume_mode else "режиме торговли"
        console.print(f"  До входа в настройки бот работал в {mode} "
                      f"({pair_label(resume_pair)}).\n")
        if confirm("Запустить его снова?"):
            set_current_pair(resume_pair)
            if resume_mode:
                launch_bot(record=True, pair=resume_pair)
                pause()
            else:
                start_live(resume_pair)


def thresholds_menu() -> None:
    while True:
        v = cfg_values()
        header(f"Настройки · пороги входа · {pair_label(v['pair'])}")
        mid, up, lo = v["midline_bps"], v["upper_bps"], v["lower_bps"]
        console.print(f"  Центр (midline):      {fmt(mid)} bps — обычный "
                      f"уровень премии")
        console.print(f"  Выше центра (upper):  +{fmt(up)} bps → продажа Entropy "
                      f"при премии ≥ {fmt(mid + up)}")
        console.print(f"  Ниже центра (lower):  −{fmt(lo)} bps → покупка Entropy "
                      f"при премии ≤ {fmt(mid - lo)}\n")
        anchor = v["midline_anchor"]
        moved = anchor is not None and abs(anchor - mid) >= 0.05
        if moved:
            console.print(f"  [dim]Центр сдвинут автокалибровкой; вручную "
                          f"было {fmt(anchor)}.[/dim]\n")
        if not is_calibrated(v):
            console.print("  [yellow]Центр = 0: пара не откалибрована, режим "
                          "торговли для неё закрыт.\n  Тестовая запись, "
                          "затем Анализ → Калибровка порогов.[/yellow]\n")
        if v["pair"] == LEGACY_PAIR:
            d = DEFAULT_THRESHOLDS
            reset = (f"Вернуть по умолчанию ({fmt(d['midline_bps'])} / "
                     f"{fmt(d['upper_bps'])} / {fmt(d['lower_bps'])})")
        else:
            # значения по умолчанию измерены на SNDK — другому тикеру их не
            # даём: «сброс» = снова не откалиброван
            d = {"midline_bps": 0.0, "upper_bps": up, "lower_bps": lo}
            reset = "Сбросить калибровку (центр 0 — торговля закрыта)"
        items = [("1", "Изменить"), ("2", reset)]
        if moved:
            items.append(("3", f"Вернуть центр к ручному значению "
                               f"({fmt(anchor)})"))
        choice = menu(items)
        if choice == "0":
            return
        if choice == "3" and moved:
            if confirm(f"Центр {fmt(mid)} → {fmt(anchor)}?"):
                apply_changes([("thresholds", "midline_bps", fmt(anchor))])
                pause()
            continue
        if choice == "1":
            console.print("\n  [dim]Бот спросит три числа по очереди. Чтобы "
                          "поменять — введите новое число;\n  чтобы оставить "
                          "как есть — просто нажмите Enter. Числа в bps, "
                          "минус пишется\n  как -7.[/dim]\n")
            new_mid = ask_number("Центр midline_bps", mid)
            while True:
                new_up = ask_number("Выше центра upper_bps", up)
                if new_up > 0:
                    break
                console.print("  [dim]Должно быть больше 0.[/dim]")
            while True:
                new_lo = ask_number("Ниже центра lower_bps", lo)
                if new_lo > 0:
                    break
                console.print("  [dim]Должно быть больше 0.[/dim]")
            new = {"midline_bps": new_mid, "upper_bps": new_up,
                   "lower_bps": new_lo}
        elif choice == "2":
            new = dict(d)
        else:
            continue
        console.print(f"\n  Будет: центр {fmt(new['midline_bps'])}, "
                      f"+{fmt(new['upper_bps'])} / −{fmt(new['lower_bps'])}")
        if confirm("Сохранить?"):
            apply_changes([("thresholds", k, fmt(val)) for k, val in new.items()])
            pause()


LIMITS_HELP = """  [dim][bold]Лимит позиции[/bold] — больше этой суммы бот не держит открытой на каждой
  бирже. Пример: при $25 бот может купить на Entropy на $25 и одновременно
  продать на другой бирже на $25. Дальше в ту же сторону не входит, пока
  позиция не уменьшится. Депозит на каждой бирже должен быть больше лимита —
  с запасом на движение цены.

  [bold]Одна сделка[/bold] — одна сделка не больше этой суммы. Бот набирает позицию
  несколькими сделками. Мелкие сделки меньше двигают тонкий стакан (меньше
  проскальзывание), но дают меньше объёма за раз. Сделки меньше минимума
  не отправляются: биржи не принимают слишком маленькие ордера.[/dim]
"""


def limits_menu() -> None:
    while True:
        v = cfg_values()
        header(f"Настройки · лимиты · {pair_label(v['pair'])}")
        console.print(f"  Лимит позиции:  [bold]{usd(v['pos_entropy'])}[/bold] "
                      f"на каждой бирже")
        if v["pos_entropy"] != v["pos_hedge"]:
            console.print(f"  [yellow]! На хедже сейчас {usd(v['pos_hedge'])} — "
                          f"лимиты разные, сохраните заново, чтобы выровнять[/yellow]")
        console.print(f"  Одна сделка:    до [bold]{usd(v['order'])}[/bold] "
                      f"(минимум {usd(v['min_order'])})\n")
        console.print(LIMITS_HELP)
        choice = menu([("1", "Лимит позиции"), ("2", "Размер одной сделки"),
                       ("3", f"Вернуть по умолчанию ({usd(DEFAULT_POSITION_USD)} / "
                             f"{usd(DEFAULT_ORDER_USD)})")])
        if choice == "0":
            return
        if choice == "1":
            console.print("\n  [dim]Лимит ставится сразу на обе биржи.[/dim]")
            while True:
                pos = ask_number("Лимит позиции, $", v["pos_entropy"], money=True)
                if pos > 0:
                    break
                console.print("  [dim]Должно быть больше 0.[/dim]")
            if pos < v["min_order"] and not confirm(
                    f"Лимит меньше минимальной сделки {usd(v['min_order'])} — "
                    f"бот не сможет торговать. Всё равно сохранить?",
                    default=False):
                continue
            changes = [("entropy", "max_position_usd", fmt(pos, True)),
                       ("hedge", "max_position_usd", fmt(pos, True))]
            if v["order"] > pos:
                console.print(f"  [dim]Сделка ({usd(v['order'])}) больше нового "
                              f"лимита — уменьшаю её до {usd(pos)}.[/dim]")
                changes.append(("sizing", "max_order_notional_usd",
                                fmt(pos, True)))
        elif choice == "2":
            console.print()
            while True:
                order = ask_number("Одна сделка до, $", v["order"], money=True)
                if order > 0:
                    break
                console.print("  [dim]Должно быть больше 0.[/dim]")
            if order < v["min_order"] and not confirm(
                    f"Это меньше минимальной сделки {usd(v['min_order'])} — "
                    f"бот не сможет торговать. Всё равно сохранить?",
                    default=False):
                continue
            changes = [("sizing", "max_order_notional_usd", fmt(order, True))]
        elif choice == "3":
            changes = [
                ("entropy", "max_position_usd", fmt(DEFAULT_POSITION_USD, True)),
                ("hedge", "max_position_usd", fmt(DEFAULT_POSITION_USD, True)),
                ("sizing", "max_order_notional_usd", fmt(DEFAULT_ORDER_USD, True))]
        else:
            continue
        apply_changes(changes)
        pause()


def loss_menu() -> None:
    """Стоп по убытку и закрытие при аварийном стопе — общие для всех пар."""
    while True:
        v = cfg_values()
        header("Настройки · стоп по убытку")
        lim = v["max_loss_pct"]
        console.print("  Стоп по убытку:  "
                      + (f"[bold]{fmt(lim)}%[/bold] за сессию"
                         if lim > 0 else "[yellow]выключен[/yellow]")
                      + " [dim](общий для всех пар)[/dim]")
        fl = v["flatten_on_halt"]
        console.print("  Закрывать позиции при аварийном стопе: "
                      + ("[bold]да[/bold]" if fl else "нет [dim](по умолчанию)"
                                                       "[/dim]") + "\n")
        console.print("  [dim]Сессия — от Старта до Стопа. Убыток сессии "
                      "дошёл до лимита — бот закрывает\n  обе ноги и "
                      "перестаёт торговать.[/dim]\n")
        choice = menu([("1", "Изменить стоп по убытку"),
                       ("2", "Закрытие позиций при аварийном стопе "
                             f"({'выключить' if fl else 'включить'})")])
        if choice == "0":
            return
        if choice == "1":
            changes = loss_limit_changes(v)
        elif choice == "2":
            changes = flatten_on_halt_changes(fl)
        else:
            continue
        if not changes:
            continue
        apply_changes(changes)
        pause()


def loss_limit_changes(v):
    """Спрашивает лимит убытка за сессию в процентах. [] — ничего не менять."""
    console.print("\n  [dim]Лимит — в ПРОЦЕНТАХ от общего баланса обеих бирж "
                  "на момент Старта: 2 — это 2%, 0.05 — это 0.05%.\n  Считается "
                  "только текущая сессия (от Старт до Стоп). Убыток дошёл до "
                  "лимита — бот закроет обе ноги и перестанет торговать.\n  "
                  "0 — выключить.[/dim]")
    while True:
        pct = ask_number("Лимит убытка за сессию, %", v["max_loss_pct"])
        if 0 <= pct < 100:
            break
        console.print("  [dim]От 0 до 100.[/dim]")
    if pct == 0 and v["max_loss_pct"] > 0 and not confirm(
            "Выключить стоп по убытку?", default=False):
        return []
    if 0 < pct < 0.5:
        console.print("  [yellow]Очень маленький лимит — подходит для "
                      "проверки, что стоп и закрытие позиций работают. Для "
                      "обычной работы поставьте 1–3%.[/yellow]")
    return [("risk", "max_loss_pct", fmt(pct))]


def flatten_on_halt_changes(current: bool):
    """Переключатель flatten_on_halt. [] — ничего не менять."""
    console.print("\n  [dim]Аварийный стоп — это когда несколько ордеров подряд "
                  "не прошли. Бот перестаёт\n  открывать сделки, но "
                  "продолжает выравнивать ноги, поэтому позиция остаётся\n  "
                  "хеджированной (лонг на одной бирже, шорт на другой) и почти "
                  "не зависит от цены.\n\n  Выключено (по "
                  "умолчанию): позиция остаётся, вы закрываете её сами,\n  "
                  "разобравшись в причине ошибок. Включено: бот сразу пробует "
                  "закрыть обе ноги —\n  но в момент, когда биржа только что "
                  "отклоняла ордера, закрытие может тоже\n  не пройти или "
                  "пройти по худшей цене, и оно стоит спреда на обеих "
                  "биржах.[/dim]\n")
    if current:
        return ([("risk", "flatten_on_halt", "false")]
                if confirm("Выключить закрытие при аварийном стопе?")
                else [])
    return ([("risk", "flatten_on_halt", "true")]
            if confirm("Включить закрытие при аварийном стопе?",
                       default=False) else [])


def exec_values() -> dict:
    c = read_config()
    return {(sec, key): float((c.get(sec) or {}).get(key, dflt))
            for sec, key, dflt, *_ in EXEC_PARAMS}


def _unit(v: float, unit: str) -> str:
    return f"{fmt(v)} {unit}".strip()


def execution_menu() -> None:
    while True:
        vals = exec_values()
        header("Настройки · исполнение")
        console.print("  [dim]Общие для всех пар. Под каждым пунктом — значение по "
                      "умолчанию.[/dim]\n")
        items = []
        for i, (sec, key, dflt, unit, short, *_r) in enumerate(EXEC_PARAMS, 1):
            cur = vals[(sec, key)]
            mark = "" if cur == dflt else " [yellow]≠ по умолчанию[/yellow]"
            console.print(f"  {i}  {key} [dim]({short})[/dim]")
            console.print(f"       сейчас [bold]{_unit(cur, unit)}[/bold] · по "
                          f"умолчанию {_unit(dflt, unit)}{mark}")
            items.append(str(i))
        console.print(f"\n  9  Вернуть все настройки по умолчанию", markup=False)
        console.print("  0  Назад\n", markup=False)
        a = ask()
        if a == "0":
            return
        if a == "9":
            diff = [(sec, key, fmt(dflt)) for sec, key, dflt, *_ in EXEC_PARAMS
                    if vals[(sec, key)] != dflt]
            if not diff:
                console.print("  Всё уже по умолчанию.")
                pause()
                continue
            for sec, key, val in diff:
                console.print(f"  {key}: {fmt(vals[(sec, key)])} → {val}")
            if confirm("Вернуть настройки по умолчанию?"):
                apply_changes(diff)
                pause()
            continue
        if a not in items:
            continue
        sec, key, dflt, unit, short, long_, (lo, hi, lo_ok) = \
            EXEC_PARAMS[int(a) - 1]
        header(f"Настройки · исполнение · {key}")
        console.print(f"  [bold]{key}[/bold] ({short})\n")
        console.print(f"  [dim]{long_}[/dim]\n")
        console.print(f"  По умолчанию: {_unit(dflt, unit)}\n")
        rng = (f"от {fmt(lo)}{'' if lo_ok else ' (не включая)'} до "
               f"{fmt(hi)}")
        while True:
            new = ask_number(key, vals[(sec, key)])
            if (new >= lo if lo_ok else new > lo) and \
                    (new < hi if key == "floor_frac" else new <= hi):
                break
            console.print(f"  [dim]Допустимо {rng}"
                          f"{' (не включая 1)' if key == 'floor_frac' else ''}."
                          f"[/dim]")
        if new == vals[(sec, key)]:
            continue
        apply_changes([(sec, key, fmt(new))])
        pause()


DEFAULT_TICKS_SEC = 2.0


def ticks_values():
    rec = read_config().get("recorder") or {}
    return (float(rec.get("ticks_sec", DEFAULT_TICKS_SEC)),
            str(rec.get("ticks_dir", "logs/ticks")))


def ticks_disk_usage(directory: str):
    """(файлов, байт) в папке частого среза."""
    n = size = 0
    try:
        for name in os.listdir(directory):
            p = os.path.join(directory, name)
            if os.path.isfile(p):
                n += 1
                size += os.path.getsize(p)
    except FileNotFoundError:
        pass
    return n, size


def ticks_menu() -> None:
    while True:
        sec, directory = ticks_values()
        header("Настройки · запись данных")
        console.print("  [dim]Кроме минутной записи (для калибровки) бот может "
                      "раз в несколько секунд\n  записывать стаканы обеих бирж: "
                      "лучшие цены, объёмы и цену сделки на\n  максимальный "
                      "размер. Это данные для будущего бэктестера — проверки "
                      "порогов\n  на истории. Пишется в любом режиме, по файлу "
                      "на пару и день; вчерашние\n  файлы сжимаются. Общий "
                      "параметр для всех пар.[/dim]\n")
        state = (f"раз в {fmt(sec)} с" if sec > 0
                 else "[yellow]выключен[/yellow]")
        console.print(f"  ticks_sec [dim](как часто записывать срез)[/dim]: "
                      f"[bold]{state}[/bold]")
        n, size = ticks_disk_usage(directory)
        console.print(f"  [dim]Папка {directory}: файлов {n}, "
                      f"{size / 1e6:.1f} МБ. При 2 с — около 7 МБ в сутки, "
                      f"после сжатия 1–2 МБ.[/dim]\n")
        choice = menu([("1", "Изменить частоту (0 — выключить)"),
                       ("2", f"Вернуть по умолчанию (раз в "
                             f"{fmt(DEFAULT_TICKS_SEC)} с)")])
        if choice == "0":
            return
        if choice == "1":
            console.print()
            while True:
                new = ask_number("Раз в сколько секунд (0 — выключить)", sec)
                if new == 0 or 1 <= new <= 60:
                    break
                console.print("  [dim]0 или от 1 до 60 секунд.[/dim]")
        elif choice == "2":
            new = DEFAULT_TICKS_SEC
        else:
            continue
        if new == sec:
            continue
        apply_changes([("recorder", "ticks_sec", fmt(new))])
        pause()


# --------------------------------------------------------------- стратегия

# Стратегия = как бот входит в сделку (execution.mode). Выбирается одна.
# Защита — дополнительные фильтры к любой стратегии, по умолчанию выключены.
DEFAULT_EF_SLIP_BPS = 5.0
DEFAULT_VOLUME_NARROW_BPS = 1.0
DEFAULT_VOLUME_MAX_COST = 2.0
DEFAULT_MAX_EXCESS_ON = 6.0       # предлагаемое значение при включении


def strategy_texts(venue_title: str, v: dict) -> dict:
    """Описание каждой стратегии: (кратко, подробно)."""
    leg = fmt(v["leg_slip"], True)
    ef = fmt(v["ef_slip"], True)
    return {
        "simultaneous": (
            "оба ордера уходят одновременно — так бот работал всегда",
            f"Бот отправляет оба ордера одновременно: на Entropy и на "
            f"{venue_title}.\n"
            f"Это самый быстрый вход.\n\n"
            f"[bold]Проскальзывание.[/bold] Если цена на Entropy успела уйти, "
            f"ордер всё равно исполнится —\nхуже плана, но не больше чем на "
            f"«защиту цены каждой ноги» (сейчас {leg} bps,\nНастройки → "
            f"Исполнение). Сделать этот предел узким здесь нельзя: если "
            f"Entropy не\nисполнится, а {venue_title} исполнится, позиция "
            f"останется без защиты («голая нога»),\nи боту придётся закрывать "
            f"её по рынку — это тоже потеря.\n\n"
            f"[green]Плюс:[/green] больше всего сделок.\n"
            f"[red]Минус:[/red] потери на цене Entropy могут быть заметными — "
            f"смотрите на дашборде\nстроку «По сделкам: ожидалось … "
            f"получилось»."),
        "entropy_first": (
            "сначала Entropy с жёстким пределом цены, потом хедж",
            f"Сначала уходит только ордер на Entropy, с жёстким пределом "
            f"цены: не хуже плана\nбольше чем на «худшую цену на Entropy» "
            f"(сейчас {ef} bps).\n"
            f" • исполнился — сразу уходит ордер на {venue_title} ровно на "
            f"исполненный объём;\n"
            f" • цена убежала — ордер на Entropy просто не исполняется, и "
            f"больше ничего\n   не отправляется: ни сделки, ни потери, ни "
            f"голой ноги.\n\n"
            f"[bold]Проскальзывание.[/bold] На Entropy — не больше предела. "
            f"Хедж на {venue_title} уходит\nна долю секунды позже, там цена "
            f"может немного сдвинуться.\n\n"
            f"[green]Плюс:[/green] меньше потерь на цене, голой ноги не "
            f"бывает.\n"
            f"[red]Минус:[/red] часть сигналов пропускается — сделок меньше; "
            f"каждый промах тратит\nодин запрос Hyperliquid. На живых биржах "
            f"проверена мало — начните с малого\nобъёма и стопа по убытку."),
        "volume": (
            "больше объёма, но не дороже заданной цены",
            f"Для тех, кому важен объём (поинты бирж, возврат комиссии), но "
            f"не любой ценой.\nИсполнение — как в стратегии 2 (сначала "
            f"Entropy, предел {ef} bps). Отличия:\n"
            f" • полоса входа уже на {fmt(v['vol_narrow'], True)} bps с каждой "
            f"стороны — сделок больше;\n"
            f" • бот сам считает, во сколько ему обходятся $10 000 объёма "
            f"на Entropy\n   (сколько потеряно на этот объём по последним 50 "
            f"сделкам пары).\n   Пока цена выше вашего предела (сейчас "
            f"${fmt(v['vol_cost'], True)}), бот открывает новые\n   позиции только "
            f"при большей премии — ровно на превышение. Закрытие позиций\n"
            f"   не сдерживается никогда.\n\n"
            f"Пример: предел $2, по сделкам выходит $3 за $10 000 → к порогу "
            f"открытия +1 bps,\nслабые сделки отсекаются, цена возвращается к "
            f"$2.\n\n"
            f"[green]Плюс:[/green] больше объёма, и его цена под вашим "
            f"контролем.\n"
            f"[red]Минус:[/red] это покупка объёма, а не заработок. Первые 10 "
            f"сделок цена ещё не\nизмерена и не ограничивается. Объём на "
            f"{venue_title} — такой же, как на Entropy."),
    }


PROTECTION_TEXTS = {
    "slipgate": (
        "Поправка на потери при исполнении",
        "Бот запоминает, сколько реально теряет на исполнении на каждой бирже "
        "(медиана\nпоследних сделок), и повышает порог открытия на 2 × эти "
        "потери — на вход и выход.\nЕсли исполнение съедает всю выгоду, бот "
        "сам перестаёт открывать сделки.\nЗакрытие позиций не ограничивается. "
        "Начинает действовать после 5 сделок.\n\n"
        "[green]Плюс:[/green] бот не торгует систематически в минус.\n"
        "[red]Минус:[/red] сделок станет заметно меньше, а при больших "
        "потерях — почти ноль,\nпока условия не улучшатся."),
    "excess": (
        "Пропуск подозрительно больших сигналов",
        "Если премия обгоняет порог входа больше чем на X bps, это обычно не "
        "настоящая\nвозможность, а стакан биржи, который не успел обновиться. "
        "Пока ордер дойдёт,\nпремии уже нет, а исполнение выходит плохим. "
        "Такой сигнал пропускается.\n\n"
        "[green]Плюс:[/green] меньше сделок с большим проскальзыванием.\n"
        "[red]Минус:[/red] изредка пропускается настоящий большой всплеск."),
}


def strategy_label(mode: str) -> str:
    from entropy_arb.strategy import strategy_title
    return strategy_title(mode)


def protection_summary(v: dict) -> str:
    on = []
    if v["slipgate"]:
        on.append("поправка на потери")
    if v["max_excess"] > 0:
        on.append(f"пропуск сигналов > {fmt(v['max_excess'], True)} bps")
    return ", ".join(on) if on else "выключена"


def slip_summary(v: dict):
    """(строка о реальном проскальзывании пары, надбавка bps) — из файла,
    который бот ведёт сам."""
    from entropy_arb import journal
    from entropy_arb.slipgate import SlipModel, slip_file_name
    path = journal.path_in(v["trades_csv"], slip_file_name(
        v["venue"], v["ticker"], v["exec_mode"]))
    if not os.path.exists(path):
        return ("для этой стратегии данных пока нет — появятся после "
                "сделок"), 0.0
    m = SlipModel(path)
    parts = []
    for key, title in (("entropy", "Entropy"),
                       ("hedge", VENUE_TITLES.get(v["venue"], "хедж"))):
        med, n = m.median(key)
        parts.append(f"{title} {fmt(round(med, 1))} bps" if med is not None
                     else f"{title} — мало сделок ({n})")
    return ("медиана по последним сделкам: " + ", ".join(parts),
            m.charge_bps(["entropy", "hedge"]))


def volume_cost_now(v: dict):
    """(цена $10 000 объёма на Entropy, сделок) по сделкам стратегии 3
    этой пары — так же, как считает бот."""
    from entropy_arb.strategy import VolumeCost
    vc = VolumeCost()
    vc.seed_csv(v["trades_csv"], v["ticker"], v["venue"], LEGACY_PAIR[1],
                LEGACY_PAIR[0])
    return vc.cost_bps()


def strategy_detail(mode: str, v: dict) -> None:
    """Экран одной стратегии: описание и выбор."""
    from entropy_arb.strategy import STRATEGIES
    title = VENUE_TITLES.get(v["venue"], v["venue"])
    _short, long_ = strategy_texts(title, v)[mode]
    header(f"Настройки · {strategy_label(mode)}")
    console.print("  " + long_.replace("\n", "\n  ") + "\n")
    if v["exec_mode"] == mode:
        console.print("  [green]Эта стратегия сейчас включена.[/green]")
        pause()
        return
    num = STRATEGIES[mode][0]
    if confirm(f"Включить стратегию {num} (для всех пар)?", default=False):
        apply_changes([("execution", "mode", mode)])
        pause()


def protection_detail(key: str, v: dict) -> None:
    title, text = PROTECTION_TEXTS[key]
    header(f"Настройки · защита · {title}")
    console.print("  " + text.replace("\n", "\n  ") + "\n")
    if key == "slipgate":
        line, charge = slip_summary(v)
        console.print(f"  [dim]Потери по {pair_label(v['pair'])}: {line}"
                      + (f"\n  сейчас к порогу открытия добавилось бы "
                         f"+{fmt(round(charge, 1))} bps" if charge else "")
                      + "[/dim]\n")
        on = v["slipgate"]
        console.print("  Сейчас: " + ("[bold]включена[/bold]" if on
                                      else "выключена") + "\n")
        if confirm("Выключить?" if on else "Включить?", default=False):
            apply_changes([("slipgate", "enabled", "false" if on else "true")])
            pause()
        return
    cur = v["max_excess"]
    console.print("  Сейчас: " + (f"[bold]включён, X = {fmt(cur)} bps[/bold]"
                                  if cur > 0 else "выключен") + "\n")
    console.print(f"  [dim]Рекомендуемое значение — {fmt(DEFAULT_MAX_EXCESS_ON)}"
                  f" bps. 0 — выключить.[/dim]")
    while True:
        new = ask_number("X, bps", cur)
        if 0 <= new <= 100:
            break
        console.print("  [dim]От 0 до 100 bps.[/dim]")
    if new != cur:
        apply_changes([("execution", "max_excess_bps", fmt(new))])
        pause()


def strategy_help() -> None:
    header("Настройки · стратегия · как выбрать")
    console.print("""  [bold]Стратегия[/bold] — как бот входит в сделку. Включена всегда ровно одна,
  она общая для всех пар. Сигнал (когда входить) у всех трёх один и тот же —
  пороги пары. Разница только в том, как отправляются ордера.

  [bold]Что такое проскальзывание.[/bold] Бот видит цену и отправляет ордер,
  но пока ордер летит до биржи, цена может уйти. Разница между ценой плана
  и ценой исполнения — проскальзывание. Это всегда потеря. Предел цены
  ограничивает её сверху, но не убирает.

  [bold]С чего начать.[/bold]
   • Стратегия 1 — если нужно как раньше.
   • Стратегия 2 — если на дашборде «получилось» заметно меньше
     «ожидалось»: значит, основная потеря — на цене исполнения.
   • Стратегия 3 — если нужен объём и вы готовы за него платить, но не
     больше заданной цены.

  [bold]Защита[/bold] — дополнительные фильтры. Сочетаются с любой стратегией
  и друг с другом, по умолчанию выключены. Каждый уменьшает число сделок.

  Меняйте по одному пункту за раз и смотрите результат за день-два:
  иначе непонятно, что именно помогло или помешало.""")
    pause()


def strategy_menu() -> None:
    from entropy_arb.strategy import STRATEGIES
    while True:
        v = cfg_values()
        mode = v["exec_mode"]
        title = VENUE_TITLES.get(v["venue"], v["venue"])
        texts = strategy_texts(title, v)
        header("Настройки · стратегия")
        console.print(f"  Сейчас: [bold]{strategy_label(mode)}[/bold] "
                      f"[dim](общая для всех пар)[/dim]")
        console.print(f"  Защита: {protection_summary(v)}\n")
        console.print("  [bold]Стратегия[/bold] [dim]— выбирается одна; "
                      "номер — подробное описание[/dim]")
        for m, (num, name) in STRATEGIES.items():
            mark = "  [green]● включена[/green]" if m == mode else ""
            console.print(f"  {num}  {name}{mark}")
            console.print(f"     [dim]{texts[m][0]}[/dim]")
        console.print("\n  [bold]Настройки стратегий[/bold]")
        tight = mode in ("entropy_first", "volume")
        console.print(f"  4  Худшая цена на Entropy: {fmt(v['ef_slip'], True)} bps "
                      f"[dim](стратегии 2 и 3{'' if tight else ' — сейчас не действует'})[/dim]")
        vol = mode == "volume"
        dim = "" if vol else " — сейчас не действует"
        console.print(f"  5  Сужение полосы для объёма: {fmt(v['vol_narrow'], True)} "
                      f"bps [dim](стратегия 3{dim})[/dim]")
        cost, n = volume_cost_now(v)
        measured = (f"по сделкам стратегии 3 сейчас ${fmt(round(cost, 2))}"
                    if cost is not None else
                    f"не измерена: сделок стратегии 3 — {n} из 10")
        console.print(f"  6  Предельная цена объёма: ${fmt(v['vol_cost'], True)} "
                      f"за $10 000 [dim](стратегия 3{dim})[/dim]")
        console.print(f"     [dim]{measured}[/dim]")
        console.print("\n  [bold]Защита[/bold] [dim]— к любой стратегии, по "
                      "умолчанию выключена[/dim]")
        console.print(f"  7  {PROTECTION_TEXTS['slipgate'][0]}: "
                      + ("[bold]вкл[/bold]" if v["slipgate"] else "выкл"))
        console.print(f"  8  {PROTECTION_TEXTS['excess'][0]}: "
                      + (f"[bold]вкл, {fmt(v['max_excess'], True)} bps[/bold]"
                         if v["max_excess"] > 0 else "выкл"))
        console.print("\n  9  Как выбрать — коротко о стратегиях и "
                      "проскальзывании", markup=False)
        console.print("  0  Назад\n", markup=False)
        choice = ask()
        if choice == "0":
            return
        by_num = {str(num): m for m, (num, _) in STRATEGIES.items()}
        if choice in by_num:
            strategy_detail(by_num[choice], v)
            continue
        if choice == "4":
            header("Настройки · худшая цена на Entropy")
            console.print("  [dim]Для стратегий 2 и 3: насколько хуже цены "
                          "сигнала бот согласен купить или\n  продать на "
                          "Entropy. Если цена ушла дальше — ордер не "
                          "исполняется, сделки нет,\n  потерь нет.\n\n"
                          "  Меньше — меньше потерь на цене, но больше "
                          "промахов (меньше сделок).\n  Больше — больше "
                          "сделок, но каждая может исполниться хуже.\n"
                          f"  По умолчанию {fmt(DEFAULT_EF_SLIP_BPS)} bps, "
                          "разумно 3–10.[/dim]\n")
            while True:
                new = ask_number("Худшая цена на Entropy, bps", v["ef_slip"])
                if 0.5 <= new <= 50:
                    break
                console.print("  [dim]От 0.5 до 50 bps.[/dim]")
            if new != v["ef_slip"]:
                apply_changes([("execution", "entropy_first_slip_bps",
                                fmt(new))])
                pause()
        elif choice == "5":
            header("Настройки · сужение полосы для объёма")
            lim = min(v["upper_bps"], v["lower_bps"])
            console.print("  [dim]Для стратегии 3: на сколько bps бот входит "
                          "раньше, чем по порогам пары,\n  с каждой стороны. "
                          "Больше — больше сделок и объёма, но каждая сделка "
                          "с меньшим\n  запасом и дороже. Перерасход сдерживает "
                          "«предельная цена объёма».\n"
                          f"  По умолчанию {fmt(DEFAULT_VOLUME_NARROW_BPS)} "
                          f"bps. Должно быть меньше upper и lower пары "
                          f"(сейчас {fmt(lim)}).[/dim]\n")
            while True:
                new = ask_number("Сужение полосы, bps", v["vol_narrow"])
                if 0 <= new <= 20:
                    break
                console.print("  [dim]От 0 до 20 bps.[/dim]")
            if new != v["vol_narrow"]:
                apply_changes([("execution", "volume_narrow_bps", fmt(new))])
                pause()
        elif choice == "6":
            header("Настройки · предельная цена объёма")
            console.print("  [dim]Для стратегии 3: сколько вы готовы потерять "
                          "на каждые $10 000 объёма на Entropy.\n  $2 за "
                          "$10 000 — это $200 за $1 000 000 (то же самое, что "
                          "2 bps на сделку).\n  Бот считает фактическую цену "
                          "по последним 50 сделкам пары. Выше предела —\n  "
                          "открывает только сделки с большей премией, пока "
                          "цена не вернётся.\n  0 — объём не должен стоить "
                          "ничего (бот откроет только сделки, которые в\n  "
                          "среднем не теряют).\n"
                          f"  По умолчанию ${fmt(DEFAULT_VOLUME_MAX_COST)}."
                          f"[/dim]\n")
            if cost is not None:
                console.print(f"  По сделкам стратегии 3 на "
                              f"{pair_label(v['pair'])} цена сейчас: "
                              f"${fmt(round(cost, 2))} за $10 000 ({n} "
                              f"сделок).\n")
            while True:
                new = ask_number("Предельная цена, $ за $10 000", v["vol_cost"])
                if 0 <= new <= 100:
                    break
                console.print("  [dim]От 0 до 100.[/dim]")
            if new != v["vol_cost"]:
                apply_changes([("execution", "volume_max_cost_usd", fmt(new))])
                pause()
        elif choice == "7":
            protection_detail("slipgate", v)
        elif choice == "8":
            protection_detail("excess", v)
        elif choice == "9":
            strategy_help()


AUTOCALIB_WINDOWS = [(24.0, "24 часа — быстро ловит сдвиг, но шумнее"),
                     (72.0, "3 дня — компромисс (по умолчанию)"),
                     (168.0, "7 дней — устойчиво, но запаздывает")]


def autocalib_menu() -> None:
    from entropy_arb.autocalib import window_label
    while True:
        v = cfg_values()
        on, win = v["autocalib"], v["autocalib_window"]
        header("Настройки · автокалибровка центра")
        console.print("  [dim]Раз в сутки бот сам сдвигает центр (midline) пары, "
                      "на которой работает, к медиане\n  премии за выбранный "
                      "период. upper и lower не меняются. Защиты: не больше\n"
                      "  1 bps за раз; не дальше 3 bps от центра, который вы "
                      "поставили вручную;\n  только когда нет открытой "
                      "позиции; нужно хотя бы 18 часов данных. Каждое\n  "
                      "изменение — в журнале, в logs/autocalib.csv и в "
                      "Telegram. Центр, заданный\n  вручную, становится новой "
                      "точкой отсчёта. Общая настройка для всех пар.[/dim]\n")
        console.print("  Автокалибровка: " + ("[bold green]включена[/bold green]"
                                              if on else "выключена"))
        console.print(f"  Период медианы: {window_label(win)}\n")
        anchor = v["midline_anchor"]
        console.print(f"  [dim]{pair_label(v['pair'])}: центр "
                      f"{fmt(v['midline_bps'])}"
                      + (f", вручную {fmt(anchor)}" if anchor is not None
                         and abs(anchor - v["midline_bps"]) >= 0.05 else "")
                      + "[/dim]")
        last = v["autocalib_last_ts"]
        if last:
            console.print(f"  [dim]Последняя проверка или ручная правка: "
                          f"{time.strftime('%d.%m %H:%M', time.localtime(last))}"
                          f"[/dim]")
        console.print()
        choice = menu([("1", "Выключить" if on else "Включить"),
                       ("2", "Период медианы")])
        if choice == "0":
            return
        if choice == "1":
            if not on and not confirm(
                    "Включить? Бот будет сам менять центр в пределах защит.",
                    default=False):
                continue
            apply_changes([("autocalib", "enabled",
                            "false" if on else "true")])
            pause()
        elif choice == "2":
            header("Настройки · автокалибровка · период")
            items = [(str(i), f"{window_label(h)}: {text.split(' — ', 1)[1]}")
                     for i, (h, text) in enumerate(AUTOCALIB_WINDOWS, 1)]
            pick = menu(items)
            if pick in {str(i) for i in range(1, len(AUTOCALIB_WINDOWS) + 1)}:
                h = AUTOCALIB_WINDOWS[int(pick) - 1][0]
                if h != win:
                    apply_changes([("autocalib", "window_hours", fmt(h))])
                    pause()


# ------------------------------------------------- запросы Hyperliquid

HL_BUY_CHOICES = [(2000, "2 000"), (5000, "5 000"), (10000, "10 000")]
HL_REQUEST_PRICE = 0.0005         # USDC за один запрос (Hyperliquid)


def hl_requests_menu() -> None:
    """Запас запросов Hyperliquid: показать и докупить."""
    xyz = xyz_keys_mode() == "own"
    while True:
        header("Настройки · запросы Hyperliquid")
        console.print("  [dim]Каждый ордер бота на Hyperliquid — исполнился он "
                      "или нет — тратит один запрос.\n  Лимит адреса: 10 000 + "
                      "1 запрос за каждый $1 оборота на нём. Если лимит\n  "
                      "исчерпан, биржа пропускает примерно 1 ордер в 10 секунд "
                      "— бот почти стоит.\n  Докупить можно у самой биржи: "
                      f"${HL_REQUEST_PRICE} за запрос, 2 000 запросов ≈ $1.\n"
                      "  Деньги списываются с баланса USDC этого адреса на "
                      "Hyperliquid.[/dim]\n")
        args = [PY, "tools/hl_requests.py", "status"]
        which = [("main", "")]
        if xyz:
            which.append(("xyz", "--xyz"))
        for name, flag in which:
            if name == "xyz":
                console.print("  [bold]Кошелёк trade.xyz[/bold]")
            with console.status("  Читаю лимит…"):
                try:
                    r = subprocess.run(args + ([flag] if flag else []),
                                       capture_output=True, text=True,
                                       timeout=30)
                    out = (r.stdout + r.stderr).strip()
                except subprocess.TimeoutExpired:
                    out = "Hyperliquid не ответил за 30 секунд."
            console.print("  " + out.replace("\n", "\n  "), markup=False)
            console.print()
        items = [(str(i), f"Купить {label} запросов (≈ ${n * HL_REQUEST_PRICE:g})")
                 for i, (n, label) in enumerate(HL_BUY_CHOICES, 1)]
        items.append(("4", "Своё количество"))
        choice = menu(items)
        if choice == "0":
            return
        if choice in ("1", "2", "3"):
            n = HL_BUY_CHOICES[int(choice) - 1][0]
        elif choice == "4":
            while True:
                n = int(ask_number("Сколько запросов купить", 2000))
                if 1 <= n <= 100000:
                    break
                console.print("  [dim]От 1 до 100 000.[/dim]")
        else:
            continue
        flag = ""
        if xyz:
            w = menu([("1", "Основной кошелёк (Entropy)"),
                      ("2", "Кошелёк trade.xyz")])
            if w not in ("1", "2"):
                continue
            flag = "--xyz" if w == "2" else ""
        if not live_deps_ok():
            console.print("  [red]Не установлены библиотеки для торговли — "
                          "запустите Старт → режим торговли,\n  меню "
                          "предложит установить.[/red]")
            pause()
            continue
        if not confirm(f"Купить {n:,} запросов за ≈ ${n * HL_REQUEST_PRICE:.2f} "
                       f"USDC?", default=False):
            continue
        with console.status("  Покупаю…"):
            r = subprocess.run([PY, "tools/hl_requests.py", "buy", str(n),
                                "--yes"] + ([flag] if flag else []),
                               capture_output=True, text=True, timeout=60)
        console.print("\n  " + (r.stdout + r.stderr).strip()
                      .replace("\n", "\n  "), markup=False)
        pause()


TG_API = "https://api.telegram.org/bot{token}/{method}"
TG_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


def tg_call(token: str, method: str, payload: dict = None) -> dict:
    """Запрос к Telegram Bot API. Ответ с ошибкой — тоже dict (ok=False)."""
    import urllib.error
    try:
        return _http_json(TG_API.format(token=token, method=method),
                          payload, timeout=10.0)
    except urllib.error.HTTPError as e:
        try:
            import json
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "description": f"HTTP {e.code}"}


def telegram_menu() -> None:
    while True:
        env = read_env()
        token = env.get("TELEGRAM_BOT_TOKEN", "")
        chat = env.get("TELEGRAM_CHAT_ID", "")
        header("Настройки · Telegram")
        if token and chat:
            console.print(f"  [green]✔ Подключён[/green] — чат {chat}, токен "
                          f"{mask('HL_PRIVATE_KEY', token)}\n")
        else:
            console.print("  [yellow]Не подключён[/yellow]\n")
        console.print("  [dim]Бот пишет вам в Telegram: при старте — пару и "
                      "режим, при остановке — итог\n  сессии (результат, "
                      "оборот, сделки, причина остановки, позиции). Пока он\n"
                      "  работает, на /status и /pnl отвечает текущими цифрами. "
                      "Управлять ботом\n  из Telegram нельзя; сообщения из "
                      "чужих чатов он игнорирует.[/dim]\n")
        items = [("1", "Подключить бота" if not token else
                  "Подключить заново (другой бот или чат)")]
        if token and chat:
            items += [("2", "Отправить тестовое сообщение"),
                      ("3", "Отключить Telegram")]
        choice = menu(items)
        if choice == "0":
            return
        if choice == "1":
            telegram_connect()
        elif choice == "2" and token and chat:
            r = tg_call(token, "sendMessage", {
                "chat_id": chat, "text": "✅ Тест: MONEY CLUB на связи."})
            console.print("  [green]✔ Отправлено — проверьте Telegram.[/green]"
                          if r.get("ok") else
                          f"  [red]✘ Не отправлено: {r.get('description')}"
                          f"[/red]")
            pause()
        elif choice == "3" and token and chat:
            if confirm("Отключить уведомления Telegram?", default=False):
                write_env({"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""})
                console.print("  Отключено.")
                pause()


def telegram_connect() -> None:
    header("Настройки · Telegram · подключение")
    console.print("  1. В Telegram откройте [bold]@BotFather[/bold] → команда "
                  "/newbot → придумайте имя\n     и адрес бота (должен "
                  "заканчиваться на bot).\n  2. BotFather пришлёт токен вида "
                  "[dim]1234567890:AAH…[/dim] — вставьте его ниже.\n"
                  "  [dim]Символы при вставке не отображаются — так задумано. "
                  "Enter без ввода — отмена.[/dim]\n")
    try:
        raw = getpass.getpass("  › ").strip().strip("\"'")
    except KeyboardInterrupt:
        print()
        return
    if not raw:
        return
    if not TG_TOKEN_RE.match(raw):
        console.print("  [red]✘ Это не похоже на токен Telegram (цифры, "
                      "двоеточие, затем длинная строка).[/red]")
        pause()
        return
    with console.status("  Проверяю токен…"):
        try:
            me = tg_call(raw, "getMe")
        except Exception as e:
            me = {"ok": False, "description": f"нет связи с Telegram ({e})"}
    if not me.get("ok"):
        console.print(f"  [red]✘ Telegram не принял токен: "
                      f"{me.get('description')}[/red]")
        pause()
        return
    name = (me.get("result") or {}).get("username", "")
    while True:
        header("Настройки · Telegram · подключение")
        console.print(f"  ✔ Бот найден: [bold]@{name}[/bold]\n")
        console.print(f"  3. Откройте @{name} в Telegram и нажмите [bold]Start"
                      f"[/bold] (или напишите ему\n     любое сообщение). "
                      f"Потом вернитесь сюда.\n")
        if not confirm("Нажали Start?"):
            return
        with console.status("  Ищу ваш чат…"):
            try:
                up = tg_call(raw, "getUpdates", {"timeout": 0})
            except Exception as e:
                up = {"ok": False, "description": str(e)}
        if not up.get("ok"):
            console.print(f"  [red]✘ Telegram: {up.get('description')}[/red]")
            pause()
            return
        found = None
        for u in up.get("result") or []:
            msg = u.get("message") or {}
            c = msg.get("chat") or {}
            if c.get("type") == "private":
                found = (c.get("id"), c.get("first_name") or
                         c.get("username") or "", u.get("update_id"))
        if found is None:
            console.print("  [yellow]Сообщение от вас не найдено. Нажмите "
                          "Start (или напишите боту)\n  и попробуйте ещё "
                          "раз.[/yellow]")
            if not confirm("Попробовать ещё раз?"):
                return
            continue
        chat_id, who, last = found
        if not confirm(f"Это ваш чат: {who} (id {chat_id})?"):
            return
        write_env({"TELEGRAM_BOT_TOKEN": raw, "TELEGRAM_CHAT_ID": str(chat_id)})
        try:   # отметить прочитанным, чтобы бот не отвечал на старое
            tg_call(raw, "getUpdates", {"offset": int(last) + 1, "timeout": 0})
        except Exception:
            pass
        r = tg_call(raw, "sendMessage", {
            "chat_id": chat_id,
            "text": "✅ MONEY CLUB подключён.\nСюда будут приходить сообщения "
                    "о старте и итоге сессии.\nКоманды (пока бот работает): "
                    "/status, /pnl"})
        console.print("\n  [green]✔ Подключено.[/green] Проверьте Telegram — "
                      "там тестовое сообщение." if r.get("ok") else
                      f"\n  [yellow]Сохранено, но тест не отправился: "
                      f"{r.get('description')}[/yellow]")
        console.print("  [dim]Уведомления начнутся со следующего Старта.[/dim]")
        pause()
        return


def ask_fee(label: str, current: float):
    """Комиссия в bps; None — пользователь передумал (очень большое число)."""
    console.print()
    while True:
        fee = ask_number(label, current)
        if 0 <= fee < 100:
            break
        console.print("  [dim]От 0 до 100 bps.[/dim]")
    if fee >= 10 and not confirm(
            f"{fmt(fee)} bps = {fmt(fee / 100)}% — это очень много. "
            f"Точно bps, а не проценты?", default=False):
        return None
    return fee


def fee_mode_label(fee: float) -> str:
    for val, title, _ in FEE_CHOICES:
        if abs(fee - val) < 1e-9:
            return title
    return "своё значение"


def fee_menu() -> None:
    while True:
        v = cfg_values()
        t = v["ticker"]
        venue = v["venue"]
        title = VENUE_TITLES.get(venue, venue)
        xyz = venue in DEFAULT_HEDGE_FEE_BPS
        header(f"Настройки · комиссии · {pair_label(v['pair'])}")
        fee = v["fee_entropy"]
        console.print(f"  Комиссия Entropy в расчёте: [bold]{fmt(fee)} bps"
                      f"[/bold] — {fee_mode_label(fee)}")
        if xyz:
            h_checked = ("[green]проверена на живой сделке[/green]"
                         if v["hedge_fee_checked"] else
                         "[yellow]стандартное значение, не измерена[/yellow]")
            console.print(f"  Комиссия {title}: {fmt(v['fee_hedge'])} bps — "
                          f"{h_checked}\n")
        else:
            console.print(f"  Комиссия {title}: {fmt(v['fee_hedge'])} bps "
                          f"[dim](у Lighter комиссии нет — менять не нужно)"
                          f"[/dim]\n")
        console.print(f"""  [dim]Это число влияет только на то, [bold]когда[/bold] бот входит: он прибавляет
  комиссию к порогу входа. Больше число — меньше сделок, но у каждой есть запас.
  Сколько биржа реально спишет, от этого числа не зависит.

  Entropy берёт комиссию сразу при сделке: сейчас {fmt(FEE_FULL_BPS)} bps (1 bps = 0.01%).
  Раз в 2 недели часть комиссии возвращается в долларах — чем выше ваш уровень,
  тем больше (по реферальной ссылке из README: уровень 1 — 80%, 2 — 110%,
  3 — 130%). Если Entropy отменит сниженный режим, комиссия может вырасти
  примерно в 10 раз — тогда это число стоит пересмотреть.[/dim]
""")
        items = []
        for i, (val, name, note) in enumerate(FEE_CHOICES, 1):
            mark = "  [green]● выбрано[/green]" if abs(fee - val) < 1e-9 else ""
            console.print(f"  {i}  {name} — {fmt(val)} bps{mark}")
            console.print(f"     [dim]{note}[/dim]")
            items.append(str(i))
        console.print("  4  Своё значение")
        if xyz:
            console.print(f"  5  Изменить комиссию {title}")
            console.print(f"  6  " + (f"Снять отметку «проверена» с комиссии "
                                       f"{title}" if v["hedge_fee_checked"]
                                       else f"Отметить: комиссия {title} "
                                            f"проверена на живой сделке"))
        console.print("  0  Назад\n")
        choice = ask()
        if choice == "0":
            return
        changes = []
        if choice in items:
            val = FEE_CHOICES[int(choice) - 1][0]
            if abs(val - fee) < 1e-9:
                continue
            changes.append(("entropy", "taker_fee_bps", fmt(val)))
        elif choice == "4":
            new = ask_fee("Комиссия Entropy в расчёте, bps", fee)
            if new is None or new == fee:
                continue
            changes.append(("entropy", "taker_fee_bps", fmt(new)))
        elif choice == "5" and xyz:
            new = ask_fee(f"Комиссия {title}, bps", v["fee_hedge"])
            if new is None:
                continue
            changes.append(("hedge", "taker_fee_bps", fmt(new)))
            checked_now = confirm(f"Это значение проверено на живой сделке "
                                  f"по {t}?", default=False)
            changes.append(("ticker", "hedge_fee_checked",
                            "true" if checked_now else "false"))
        elif choice == "6" and xyz:
            changes.append(("ticker", "hedge_fee_checked",
                            "false" if v["hedge_fee_checked"] else "true"))
        else:
            continue
        apply_changes(changes)
        pause()


def previous_session_ok() -> bool:
    """Перед стартом торговли: если прошлый запуск не закрыл позиции или
    завершился аварийно — сказать об этом и спросить подтверждение."""
    from entropy_arb import journal
    path = cfg_values()["trades_csv"]
    marker = journal.read_marker(path)
    last = journal.last_session(path)
    problem = None
    if marker is not None:
        problem = ("Прошлый запуск завершился аварийно (сервер, сбой или "
                   "принудительная остановка) — позиции могли остаться "
                   "открытыми.")
    elif last is not None and last.get("positions_closed") == "0":
        problem = ("В прошлый раз бот не смог закрыть позиции при "
                   "остановке.")
    if problem is None:
        return True
    console.print(f"  [red]⚠ {problem}[/red]\n  Проверьте позиции на обеих "
                  f"биржах. Открытую позицию бот подхватит, а при остановке "
                  f"попробует закрыть.\n")
    if not confirm("Позиции проверили, запускать?", default=False):
        return False
    journal.clear_marker(path)
    return True


def keys_menu() -> None:
    while True:
        env = read_env()
        state, all_ok = keys_state()
        header("Настройки · ключи")
        groups = [("Hyperliquid (Entropy)", COMMON_KEYS),
                  ("Lighter RH", VENUE_KEYS["lighter-rh"]),
                  ("Lighter (core) — нужны только для торговли через core",
                   VENUE_KEYS["lighter"]),
                  ("trade.xyz — необязательно, отдельный кошелёк",
                   XYZ_KEYS)]
        xyz = xyz_keys_mode(env)
        i = 0
        for title, keys in groups:
            console.print(f"  [bold]{title}[/bold]")
            for k in keys:
                i += 1
                filled, ok = state[k]
                if k in OPTIONAL_KEYS and not filled:
                    # пустой необязательный ключ — норма, если пуста и пара
                    mark = "[dim]·[/dim]" if ok else "[red]✘[/red]"
                    shown = "не задан"
                else:
                    mark = "[green]✔[/green]" if ok else (
                        "[red]✘[/red]" if filled else "[dim]·[/dim]")
                    shown = mask(k, env.get(k, ""))
                console.print(f"  {i:<2} {mark} {k:<29} [dim]{shown}[/dim]")
            if keys is XYZ_KEYS:
                note = {"shared": "[dim]не заданы — trade.xyz торгует с "
                                  "ключами Entropy (лимит запросов общий)"
                                  "[/dim]",
                        "own": "[green]отдельный кошелёк для trade.xyz — свой "
                               "лимит запросов[/green]",
                        "broken": "[red]нужны оба: ключ и адрес — или очистите "
                                  "оба[/red]"}[xyz]
                console.print(f"     {note}")
            console.print()
        n = len(ENV_KEYS)
        console.print(f"  {n + 1:<2} Ввести по порядку: Hyperliquid + "
                      f"Lighter RH", markup=False)
        console.print(f"  {n + 2:<2} Ввести по порядку: Lighter (core)",
                      markup=False)
        console.print(f"  {n + 3:<2} Ввести по порядку: отдельный кошелёк "
                      f"trade.xyz", markup=False)
        if xyz != "shared":
            console.print(f"  {n + 4:<2} Очистить ключи trade.xyz (торговать "
                          f"с ключами Entropy)", markup=False)
        console.print("  0  Назад\n", markup=False)
        choice = ask()
        if choice == "0":
            return
        seqs = {str(n + 1): ("lighter-rh", COMMON_KEYS + VENUE_KEYS["lighter-rh"]),
                str(n + 2): ("lighter", VENUE_KEYS["lighter"]),
                str(n + 3): ("tradexyz", XYZ_KEYS)}
        if choice in seqs:
            venue, seq = seqs[choice]
            for k in seq:
                if not edit_key(k):
                    break
            check_agent_address()
            header("Настройки · ключи")
            _, all_ok = keys_state(venue)
            if venue == "tradexyz":
                mode = xyz_keys_mode()
                console.print({
                    "own": "  [green]✔ Отдельный кошелёк для trade.xyz "
                           "задан.[/green]",
                    "shared": "  Ключи trade.xyz не заданы — будут "
                              "использоваться ключи Entropy.",
                    "broken": "  [yellow]Нужны оба значения: ключ и адрес. "
                              "Пока заполнено только одно — торговля через "
                              "trade.xyz не запустится.[/yellow]"}[mode])
            else:
                console.print(f"  [green]✔ Ключи для {VENUE_TITLES[venue]} "
                              f"заполнены.[/green]" if all_ok
                              else "  [yellow]Не все ключи заполнены.[/yellow]")
            pause()
        elif choice == str(n + 4) and xyz != "shared":
            if confirm("Очистить ключи trade.xyz? Нога trade.xyz будет "
                       "торговать с ключами Entropy.", default=False):
                write_env({k: "" for k in XYZ_KEYS})
                console.print("  [green]✔ Очищено.[/green]")
                pause()
        elif choice.isdigit() and 1 <= int(choice) <= n:
            edit_key(ENV_KEYS[int(choice) - 1])
            check_agent_address()


def edit_key(name: str) -> bool:
    """Спрашивает один ключ. False — пользователь прервал ввод."""
    title, help_text = KEY_INFO[name]
    current = read_env().get(name, "")
    header("Настройки · ключи")
    console.print(f"  [bold]{title}[/bold]  ({name})")
    console.print(f"  [dim]{help_text}[/dim]\n")
    if current:
        console.print(f"  Сейчас: {mask(name, current)}")
    secret = name in SECRET_KEYS
    if secret:
        console.print("  [dim]Вставьте ключ и нажмите Enter. Символы на экране "
                      "не отображаются — так задумано.[/dim]")
    console.print("  [dim]Enter без ввода — оставить как есть.[/dim]\n")
    while True:
        try:
            raw = (getpass.getpass("  › ") if secret else ask("  › ")).strip()
        except KeyboardInterrupt:
            print()
            return False
        except EOFError:
            raise SystemExit(0)
        if raw == "":
            return True
        value, err, warn = validate_key(name, raw)
        if err:
            console.print(f"  [red]✘ {err}[/red]\n  Попробуйте ещё раз "
                          f"(Enter — оставить как есть).")
            continue
        console.print(f"  Принято: {mask(name, value)}")
        if warn:
            console.print(f"  [yellow]! {warn}[/yellow]")
            if not confirm("Сохранить всё равно?", default=False):
                continue
        write_env({name: value})
        return True


def check_agent_address() -> None:
    env = read_env()
    for kname, aname in (("HL_PRIVATE_KEY", "HL_ACCOUNT_ADDRESS"),
                         ("HL_PRIVATE_KEY_XYZ", "HL_ACCOUNT_ADDRESS_XYZ")):
        pk, addr = env.get(kname, ""), env.get(aname, "")
        if not pk or not addr or validate_key(kname, pk)[1] or \
                validate_key(aname, addr)[1]:
            continue
        derived = agent_address(pk)
        if derived and derived.lower() == addr.lower():
            header("Настройки · ключи")
            console.print(f"  [yellow]! {aname} совпадает с адресом самого "
                          f"агента.[/yellow]\n  Нужен адрес ОСНОВНОГО "
                          f"кошелька, на котором лежат деньги, а не адрес "
                          f"API-кошелька.\n")
            if confirm("Ввести адрес заново?"):
                edit_key(aname)


# ================================================================== main

def first_run_prompt() -> None:
    header("Добро пожаловать")
    console.print("  Бот установлен. Для торговли нужны ключи Hyperliquid "
                  "и Lighter.\n  [dim]Для тестовой записи ключи не нужны — "
                  "их можно ввести позже: Настройки → Ключи.[/dim]\n")
    try:
        if confirm("Ввести ключи сейчас?"):
            keys_menu()
    except Back:
        pass


def main() -> None:
    if subprocess.run(["which", "tmux"], capture_output=True).returncode != 0:
        print("Не найден tmux. Запустите установку ещё раз той же командой.")
        sys.exit(1)
    if ensure_files():
        first_run_prompt()
    actions = {"1": action_start, "2": action_stop, "3": open_dashboard,
               "4": action_analysis, "5": action_settings}
    while True:
        try:
            main_screen()
            choice = menu([("1", "Старт"), ("2", "Стоп"), ("3", "Дашборд"),
                           ("4", "Анализ"), ("5", "Настройки")],
                          back_label="Выход")
        except Back:
            choice = "0"
        if choice == "0":
            clear()
            console.print("  Бот продолжает работать на сервере, если был "
                          "запущен.\n  Открыть меню снова — команда [bold]"
                          "moneyclub[/bold]\n")
            return
        fn = actions.get(choice)
        if fn is None:
            continue
        try:
            fn()
        except Back:
            continue
        except SystemExit:
            raise
        except Exception as e:
            console.print(f"\n  [red]✘ Ошибка: {e}[/red]", markup=True)
            try:
                pause()
            except Back:
                pass


if __name__ == "__main__":
    main()
