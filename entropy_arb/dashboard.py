"""Terminal dashboard of MoneyClub_entropybot_2 (Russian with --ru).

Deliberately minimal: one look tells how the session goes.

    pair and venues · mode · state in words · session time
    session result, $  — big, green / red  (Σ equity now − at Start)
    turnover and number of trades this session
    each venue: open position (LONG / SHORT, size, $) and balance
    the loss limit line
    alerts — only while something is wrong

Everything else (premium vs thresholds, every execution, the event log)
is still written to the logs and the CSV journals, and is analysed from
the menu (Анализ), not shown here.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import List, Optional

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

log = logging.getLogger("dashboard")

ALERT_ERROR_WINDOW_SEC = 600.0   # show the latest error for 10 minutes

HEDGE_TITLES = {"lighter": "Lighter", "lighter-rh": "Lighter RH",
                "tradexyz": "trade.xyz"}

# Russian UI (keys are the English strings; anything missing stays English)
_RU = {
    "starting — resolving markets…": "запуск — загружаем рынки…",
    "TRADING": "РЕЖИМ ТОРГОВЛИ",
    "TEST RECORDING": "ТЕСТОВАЯ ЗАПИСЬ",
    "RECORDING MARKET": "ИДЁТ ЗАПИСЬ РЫНКА",
    "STOPPING · CLOSING POSITIONS": "ОСТАНАВЛИВАЕТСЯ · ЗАКРЫВАЕТ ПОЗИЦИИ",
    "CLOSING POSITIONS": "ЗАКРЫВАЕТ ПОЗИЦИИ",
    "LOSS STOP": "СТОП ПО УБЫТКУ",
    "EMERGENCY STOP": "АВАРИЙНЫЙ СТОП",
    "PREPARING": "ПОДГОТОВКА",
    "PAUSED: BALANCE UNREADABLE": "ПАУЗА: БАЛАНС НЕ ЧИТАЕТСЯ",
    "VENUE DOWN": "БИРЖА НЕДОСТУПНА",
    "TRADING NOW": "ТОРГУЕТ",
    "WAITING FOR SIGNAL": "ЖДЁТ СИГНАЛА",
    "session result, $": "результат сессии, $",
    "reading balances…": "читаю балансы бирж…",
    "Turnover this session": "Оборот за сессию",
    "Trades": "Сделок",
    "Per trade: expected": "По сделкам: ожидалось",
    "got": "получилось",
    "gap": "разница",
    "venue": "биржа",
    "position": "позиция",
    "balance": "баланс",
    "no position": "нет позиции",
    "LONG": "ЛОНГ",
    "SHORT": "ШОРТ",
    "Total {now} · at start {start}": "Всего {now} · на старте {start}",
    "Loss stop: at −${usd} ({pct}% of ${base})":
        "Стоп по убытку: при −${usd} ({pct}% от ${base})",
    "Loss stop: {pct}% (waiting for the start balance)":
        "Стоп по убытку: {pct}% (ждёт стартовый баланс)",
    "Loss stop: off": "Стоп по убытку: выключен",
    "Q — exit to the menu (the bot keeps running)":
        "Q — выйти в меню (бот продолжит работать)",
    "Minutes recorded": "Записано минут",
    "Premium now": "Премия сейчас",
    "last hour": "за последний час",
    "min": "мин", "median": "медиана", "max": "макс",
    "{m:.0f} min of data": "данных за {m:.0f} мин",
    "waiting for both order books…": "жду стаканы обеих бирж…",
    "stale data: {v}": "нет свежих данных: {v}",
    "venue unreachable: {v}": "биржа недоступна: {v}",
    "request limit: {v}": "лимит запросов: {v}",
    "balance unreadable — trading paused": "баланс не читается — торговля на паузе",
    "legs unbalanced: net {n}": "ноги не сбалансированы: перекос {n}",
    "errors in a row: {n}": "ошибок подряд: {n}",
    "POSITION NOT CLOSED — check the exchanges":
        "ПОЗИЦИЯ НЕ ЗАКРЫЛАСЬ — проверьте биржи",
    "last error: {m}": "последняя ошибка: {m}",
    "stopped": "остановлен",
    " — session result ": " — результат сессии ",
    ", turnover ${t}, trades {n}": ", оборот ${t}, сделок {n}",
    " — full log: {f}": " — полный лог: {f}",
    "Hyperliquid request limit: {v} ({h}) — at most 1 order per 10 s":
        "лимит запросов Hyperliquid: {v} ({h}) — не чаще 1 ордера в 10 с",
    "Hyperliquid request limit ({h}): both legs on one address — no "
    "arbitrage; it only recovers with new volume on this address — use "
    "separate trade.xyz keys":
        "лимит запросов Hyperliquid ({h}): обе ноги на одном адресе — "
        "арбитраж стоит; лимит растёт только от нового оборота на этом "
        "адресе — заведите отдельные ключи trade.xyz",
    "auto-calibration due: waits until the position is closed":
        "автокалибровка: пора, ждёт закрытия позиции",
    "auto-calibration: midline {m} (manual {a}), next check in {h} h":
        "автокалибровка: центр {m} (ручной {a}), следующая проверка через "
        "{h} ч",
    "unhedged remainder {n} ≈ ${u} — below the minimum order, closed at Stop":
        "остаток без хеджа {n} ≈ ${u} — меньше минимальной сделки, "
        "закроется при Стопе",
}

# 3-row block digits for the big session result
_BIG = {
    "0": ["█▀█", "█ █", "█▄█"], "1": ["▀█ ", " █ ", "▄█▄"],
    "2": ["▀▀█", "█▀▀", "█▄▄"], "3": ["▀▀█", " ▀█", "▄▄█"],
    "4": ["█ █", "▀▀█", "  █"], "5": ["█▀▀", "▀▀█", "▄▄█"],
    "6": ["█▀▀", "█▀█", "█▄█"], "7": ["▀▀█", "  █", "  █"],
    "8": ["█▀█", "█▀█", "█▄█"], "9": ["█▀█", "▀▀█", "▄▄█"],
    "+": ["   ", "▄█▄", " ▀ "], "-": ["   ", "▄▄▄", "   "],
    ".": [" ", " ", "▄"], ",": [" ", " ", "▄"],
}


def big_text(s: str) -> List[str]:
    rows = ["", "", ""]
    for ch in s:
        g = _BIG.get(ch)
        if g is None:
            continue
        for i in range(3):
            rows[i] += g[i] + " "
    return [r.rstrip() for r in rows]


class BufferLogHandler(logging.Handler):
    """Ring buffer of recent log lines for the events panel."""

    def __init__(self, maxlen: int = 200) -> None:
        super().__init__()
        self.lines: deque = deque(maxlen=maxlen)
        self.last_error = None   # (created_ts, message) of the latest ERROR+
        self.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            return
        if "[status]" in msg:
            return  # the dashboard already shows everything the status line says
        self.lines.append((record.levelno, msg))
        if record.levelno >= logging.ERROR:
            self.last_error = (record.created, record.getMessage())


def _usd(x: Optional[float], signed: bool = True, decimals: int = 4) -> Text:
    if x is None:
        return Text("—", style="dim")
    style = "bold green" if x > 0 else ("bold red" if x < 0 else "")
    if signed:
        return Text(f"${x:+,.{decimals}f}", style=style)
    return Text(f"${x:,.{decimals}f}")


class Dashboard:
    def __init__(self, eng, log_buffer: BufferLogHandler, log_file: str,
                 force_terminal: bool = False, lang: str = "en") -> None:
        self.eng = eng
        self.log_buffer = log_buffer
        self.log_file = log_file
        self.lang = lang
        self.console = Console(force_terminal=True if force_terminal else None)

    def _t(self, s: str, /, **kw) -> str:
        """Translate a UI string (English key -> current language), then
        fill in any {placeholders}."""
        if self.lang == "ru":
            s = _RU.get(s, s)
        return s.format(**kw) if kw else s

    async def run(self) -> None:
        eng = self.eng
        with Live(self._safe_render(), console=self.console,
                  refresh_per_second=4, screen=True) as live:
            # stay up through the stop-time close of positions: the engine
            # sets `done` only after it has fully shut down
            while not getattr(eng, "done", eng.stop.is_set()):
                live.update(self._safe_render())
                await asyncio.sleep(0.25)
        t = Text()
        t.append("MoneyClub_entropybot_2 " + self._t("stopped"), style="bold")
        r = eng.session_result() if hasattr(eng, "session_result") else None
        if r is not None:
            t.append(self._t(" — session result "))
            t.append(f"${r:+.2f}", style="bold green" if r > 0
                     else ("bold red" if r < 0 else ""))
        if not eng.record_only:
            t.append(self._t(", turnover ${t}, trades {n}",
                             t=f"{eng.turnover_usd():,.0f}", n=eng.trades))
        t.append(self._t(" — full log: {f}", f=self.log_file))
        self.console.print(t)

    def _safe_render(self):
        try:
            return self._render()
        except Exception as e:
            log.exception("dashboard render failed")
            return Panel(Text(f"render error: {e!r}\nsee log file"), style="red")

    # -------------------------------------------------------------- pieces

    def _pair_title(self) -> str:
        eng = self.eng
        hedge = HEDGE_TITLES.get(eng.cfg.hedge_venue, eng.hedge.name)
        title = f"{eng.cfg.symbol} · Entropy ↔ {hedge}"
        ent = getattr(getattr(eng.entropy, "conf", None), "symbol", None) \
            or eng.cfg.symbol
        hed = getattr(getattr(eng.hedge, "conf", None), "symbol", None) \
            or eng.cfg.symbol
        if {ent, hed} != {eng.cfg.symbol}:
            # the venues list this ticker under other names — show them
            title += f" ({ent} / {hed})"
        return title

    def _venue_title(self, v) -> str:
        if v.key == "entropy":
            return "Entropy"
        return HEDGE_TITLES.get(self.eng.cfg.hedge_venue, v.name)

    def state(self):
        """(text, style) — the session state in words."""
        eng = self.eng
        if eng.record_only:
            return self._t("RECORDING MARKET"), "bold white on green"
        if getattr(eng, "stopping", False):
            return self._t("STOPPING · CLOSING POSITIONS"), "black on yellow"
        if getattr(eng, "flattening", False):
            return self._t("CLOSING POSITIONS"), "black on yellow"
        if eng.halted:
            if getattr(eng, "halt_reason", "") == "loss limit":
                return self._t("LOSS STOP"), "bold white on red"
            return self._t("EMERGENCY STOP"), "bold white on red"
        if not getattr(eng, "risk_ready", True):
            return self._t("PREPARING"), "black on yellow"
        if getattr(eng, "risk_blind", False):
            return self._t("PAUSED: BALANCE UNREADABLE"), "bold white on red"
        if eng._venue_down:
            return self._t("VENUE DOWN"), "bold white on red"
        if eng._exec_tasks:
            return self._t("TRADING NOW"), "bold white on green"
        return self._t("WAITING FOR SIGNAL"), "bold white on green"

    def alerts(self) -> List[str]:
        """Problems only; an empty list means all is well."""
        eng, cfg = self.eng, self.eng.cfg
        out = []
        for v in eng.venues.values():
            name = self._venue_title(v)
            if v.key in eng._venue_down:
                out.append(self._t("venue unreachable: {v}", v=name))
            elif not v.book.is_fresh(cfg.staleness_sec):
                out.append(self._t("stale data: {v}", v=name))
            elif eng._venue_limited(v):
                out.append(self._t("request limit: {v}", v=name))
        if eng.record_only:
            return out
        if getattr(eng, "risk_blind", False):
            out.append(self._t("balance unreadable — trading paused"))
        seen = set()
        for v in eng.venues.values():
            limited = getattr(eng, "req_limited", None)
            if limited is None or not limited(v):
                continue
            b = eng.req_budget[v.key]
            if id(b) in seen:
                continue          # one address = one budget: say it once
            seen.add(id(b))
            h = b.get("headroom")
            if getattr(eng, "hl_shared", False):
                out.append(self._t(
                    "Hyperliquid request limit ({h}): both legs on one "
                    "address — no arbitrage; it only recovers with new "
                    "volume on this address — use separate trade.xyz keys",
                    h=f"{h:+d}"))
            else:
                out.append(self._t(
                    "Hyperliquid request limit: {v} ({h}) — at most 1 order "
                    "per 10 s", v=self._venue_title(v), h=f"{h:+d}"))
        net = sum(v.position for v in eng.venues.values())
        if abs(net) > cfg.net_tolerance_base and not eng._exec_tasks \
                and not self._is_remainder():
            out.append(self._t("legs unbalanced: net {n}", n=f"{net:+.6g}"))
        if eng.consec_errors:
            out.append(self._t("errors in a row: {n}", n=eng.consec_errors))
        if getattr(eng, "flatten_failed", False):
            out.append(self._t("POSITION NOT CLOSED — check the exchanges"))
        le = getattr(self.log_buffer, "last_error", None)
        if le and time.time() - le[0] < ALERT_ERROR_WINDOW_SEC:
            msg = le[1] if len(le[1]) <= 110 else le[1][:107] + "…"
            out.append(self._t("last error: {m}", m=msg))
        return out

    def _premium_lines(self):
        """Test recording: what is being collected — the premium now and its
        range over the last hour."""
        eng = self.eng
        out = [Text("")]
        prem = eng.premium_bps()
        if prem is None:
            out.append(Align.center(Text(
                self._t("waiting for both order books…"), style="yellow")))
            return out
        now = Text(self._t("Premium now") + ": ", style="dim")
        now.append(f"{prem:+.2f} bps", style="bold")
        out.append(Align.center(now))
        st = eng.premium_stats() if hasattr(eng, "premium_stats") else None
        if st is not None:
            lo, med, hi, mins = st
            line = Text(self._t("last hour") + ": ", style="dim")
            line.append(f"{self._t('min')} {lo:+.2f} · {self._t('median')} "
                        f"{med:+.2f} · {self._t('max')} {hi:+.2f} bps")
            line.append("  (" + self._t("{m:.0f} min of data", m=mins) + ")",
                        style="dim")
            out.append(Align.center(line))
        return out

    def _is_remainder(self) -> bool:
        """The legs differ by less than a venue's minimum order: a remainder
        the bot cannot trade away — shown as a note, closed at Stop."""
        fn = getattr(self.eng, "unhedged", None)
        u = fn() if fn is not None else None
        return u is not None and u[1] is not None and not u[2]

    def notes(self) -> List[str]:
        """Information worth seeing that is not a problem (yellow)."""
        eng = self.eng
        if eng.record_only if hasattr(eng, "record_only") else False:
            return []
        out = []
        if self._is_remainder():
            net, usd, _ = eng.unhedged()
            out.append(self._t("unhedged remainder {n} ≈ ${u} — below the "
                               "minimum order, closed at Stop",
                               n=f"{net:+.6g} {eng.cfg.symbol}",
                               u=f"{usd:.2f}"))
        if getattr(eng, "autocalib_on", False):
            if getattr(eng, "autocalib_waiting", False):
                out.append(self._t("auto-calibration due: waits until the "
                                   "position is closed"))
            else:
                nxt = eng.autocalib_next_ts()
                h = max(0.0, (nxt - time.time()) / 3600.0) if nxt else 0.0
                out.append(self._t("auto-calibration: midline {m} (manual "
                                   "{a}), next check in {h} h",
                                   m=f"{eng.cfg.midline_bps:+.1f}",
                                   a=f"{eng.autocalib_anchor:+.1f}",
                                   h=f"{h:.0f}"))
        return out

    def _result_block(self):
        eng = self.eng
        r = eng.session_result() if hasattr(eng, "session_result") else None
        if r is None:
            return Align.center(Text(self._t("reading balances…"),
                                     style="dim"))
        style = "bold green" if r > 0 else ("bold red" if r < 0 else "bold")
        num = f"{r:+.2f}"
        lines = Text("\n".join(big_text(num)), style=style)
        cap = Text(self._t("session result, $") + f"   {num}", style="dim")
        base = getattr(eng, "session_base_total", None)
        if base:
            cap.append(f"  ({r / base * 100:+.2f}%)", style="dim")
        return Group(Align.center(lines), Align.center(cap))

    def _edge_line(self) -> Optional[Text]:
        """Planned vs realized edge of the arbitrage trades."""
        c = getattr(self.eng, "edge_compare", lambda: None)()
        if c is None:
            return None
        from .telegram import money
        _n, exp, got = c
        gap = got - exp
        t = Text(self._t("Per trade: expected") + " ", style="dim")
        t.append(money(exp), style="bold")
        t.append("  " + self._t("got") + " ", style="dim")
        t.append(money(got), style="bold green" if got >= 0 else "bold red")
        t.append("  " + self._t("gap") + " ", style="dim")
        t.append(money(gap), style="green" if gap >= 0 else "red")
        return t

    def _strategy_line(self) -> Optional[Text]:
        """Execution mode and what execution has actually been costing."""
        from .telegram import strategy_line
        line = strategy_line(self.eng)
        return Text(line, style="dim") if line else None

    def _positions_table(self):
        eng = self.eng
        t = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 1))
        t.add_column(self._t("venue"), no_wrap=True)
        t.add_column(self._t("position"), no_wrap=True)
        t.add_column(self._t("balance"), justify="right", no_wrap=True)
        for v in eng.venues.values():
            m = v.book.mid()
            pos = v.position
            if pos == 0 or (m is not None and abs(pos) * m < 0.5):
                p = Text(self._t("no position"), style="dim")
            else:
                long_ = pos > 0
                p = Text("▲ " + self._t("LONG") if long_
                         else "▼ " + self._t("SHORT"),
                         style="bold cyan" if long_ else "bold magenta")
                p.append(f" {abs(pos):.6g} {eng.cfg.symbol}")
                if m is not None:
                    p.append(f" · ${abs(pos) * m:,.2f}", style="dim")
            bal = (Text(f"${v.equity:,.2f}") if v.equity is not None
                   else Text("—", style="dim"))
            t.add_row(Text(self._venue_title(v), style="bold"), p, bal)
        return t

    def _totals_line(self) -> Optional[Text]:
        eng = self.eng
        s = getattr(eng, "risk_last_sample", None)
        base = getattr(eng, "session_base_total", None)
        if s is None or base is None:
            return None
        return Text(self._t("Total {now} · at start {start}",
                            now=f"${s.total:,.2f}", start=f"${base:,.2f}"),
                    style="dim")

    def _stop_line(self) -> Text:
        eng, cfg = self.eng, self.eng.cfg
        pct = cfg.max_loss_pct
        if pct <= 0:
            return Text(self._t("Loss stop: off"), style="yellow")
        g = getattr(eng, "risk_guard", None)
        if g is None:
            return Text(self._t("Loss stop: {pct}% (waiting for the start "
                                "balance)", pct=f"{pct:g}"), style="dim")
        return Text(self._t("Loss stop: at −${usd} ({pct}% of ${base})",
                            usd=f"{g.limit_usd:,.2f}", pct=f"{pct:g}",
                            base=f"{g.base_total:,.2f}"), style="dim")

    def _header(self):
        eng = self.eng
        mode = self._t("TEST RECORDING") if eng.record_only \
            else self._t("TRADING")
        st, st_style = self.state()
        up = int(time.time() - eng.start_ts)
        g = Table.grid(expand=True)
        g.add_column(justify="left")
        g.add_column(justify="right")
        right = Text(mode + "  ", style="bold")
        right.append(f" {st} ", style=st_style)
        right.append(f"  {up // 3600}:{up % 3600 // 60:02d}:{up % 60:02d}",
                     style="dim")
        g.add_row(Text(self._pair_title(), style="bold cyan"), right)
        return g

    # ------------------------------------------------------------ renderer

    def _render(self):
        eng = self.eng
        if eng.entropy is None or eng.hedge is None or not eng.markets_ready:
            return Panel(Text(self._t("starting — resolving markets…"),
                              style="yellow"), title="MoneyClub_entropybot_2",
                         box=box.ROUNDED)
        parts = [self._header(), Text("")]
        if eng.record_only:
            n = eng.recorder.rows_written if eng.recorder is not None else 0
            parts.append(Align.center(Text(
                self._t("Minutes recorded") + f": {n}", style="bold")))
            parts += self._premium_lines()
        else:
            parts += [self._result_block(), Text("")]
            info = Text(self._t("Turnover this session") + ": ", style="dim")
            info.append(f"${eng.turnover_usd():,.2f}", style="bold")
            info.append("    " + self._t("Trades") + ": ", style="dim")
            info.append(str(eng.trades), style="bold")
            parts.append(Align.center(info))
            parts.append(Align.center(Text(
                getattr(eng, "strategy_name", ""), style="bold cyan")))
            cmp_line = self._edge_line()
            if cmp_line is not None:
                parts.append(Align.center(cmp_line))
            st_line = self._strategy_line()
            if st_line is not None:
                parts.append(Align.center(st_line))
            parts.append(self._positions_table())
            tot = self._totals_line()
            if tot is not None:
                parts.append(tot)
            parts.append(self._stop_line())
        al = self.alerts()
        if al:
            parts.append(Text(""))
            for a in al:
                parts.append(Text("⚠ " + a, style="bold red"))
        nt = self.notes()
        if nt:
            if not al:
                parts.append(Text(""))
            for n in nt:
                parts.append(Text("• " + n, style="yellow"))
        return Panel(Group(*parts), title="MoneyClub_entropybot_2", box=box.ROUNDED,
                     padding=(0, 1),
                     subtitle=self._t("Q — exit to the menu (the bot keeps "
                                      "running)"))
