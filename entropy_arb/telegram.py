"""Telegram: a message when the bot starts, a session summary when it stops,
and read-only /status and /pnl commands while it runs.

Each user creates their own Telegram bot (@BotFather) and keeps its token
and their chat id in .env (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID). Messages
from any other chat are ignored. There are no commands that change anything
— a leaked token shows a PnL, it cannot trade.

Telegram is best-effort: every call has a timeout and every failure is
logged and swallowed, so a Telegram outage can never stop or slow trading.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

import aiohttp

log = logging.getLogger("telegram")

API = "https://api.telegram.org/bot{token}/{method}"
VENUE_TITLES = {"lighter-rh": "Lighter RH", "lighter": "Lighter",
                "tradexyz": "trade.xyz"}
HELP = ("Команды:\n"
        "/status — состояние бота: пара, режим, результат, позиции, премия\n"
        "/pnl — результат текущей сессии\n\n"
        "Бот отвечает, пока он запущен.")


class TelegramBot:
    def __init__(self, token: Optional[str], chat_id: Optional[str]):
        self.token = (token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self._session: Optional[aiohttp.ClientSession] = None
        self._offset: Optional[int] = None

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    async def _call(self, method: str, payload: dict, timeout: float) -> dict:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        url = API.format(token=self.token, method=method)
        async with self._session.post(
                url, json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            return await r.json(content_type=None)

    async def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        try:
            d = await self._call("sendMessage", {
                "chat_id": self.chat_id, "text": text,
                "disable_web_page_preview": True}, 10.0)
            if not d.get("ok"):
                log.warning("telegram send refused: %s", d.get("description"))
            return bool(d.get("ok"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("telegram send failed: %r", e)
            return False

    async def skip_backlog(self) -> None:
        """Commands sent while the bot was stopped are not answered later."""
        d = await self._call("getUpdates", {"offset": -1, "timeout": 0}, 10.0)
        res = d.get("result") or []
        if res:
            self._offset = int(res[-1]["update_id"]) + 1

    async def get_updates(self, timeout: int = 25) -> List[dict]:
        payload = {"timeout": timeout, "allowed_updates": ["message"]}
        if self._offset is not None:
            payload["offset"] = self._offset
        d = await self._call("getUpdates", payload, timeout + 10.0)
        res = d.get("result") or []
        for u in res:
            self._offset = int(u["update_id"]) + 1
        return res

    def command_of(self, update: dict) -> Optional[str]:
        """'/status' etc. of a message from OUR chat; None for anything
        else (other chats are ignored entirely)."""
        msg = update.get("message") or {}
        if str((msg.get("chat") or {}).get("id", "")) != self.chat_id:
            return None
        text = (msg.get("text") or "").strip()
        if not text:
            return None
        return text.split()[0].split("@")[0].lower()

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None


# ------------------------------------------------------------ message text

def _hms(sec: float) -> str:
    sec = int(max(0, sec))
    h, m = sec // 3600, sec % 3600 // 60
    return f"{h} ч {m:02d} мин" if h else f"{m} мин"


def _usd(v: Optional[float], sign: bool = False) -> str:
    if v is None:
        return "—"
    return f"{'+' if sign and v > 0 else ''}{'-' if v < 0 else ''}${abs(v):,.2f}"


def pair_title(eng) -> str:
    hedge = VENUE_TITLES.get(eng.cfg.hedge_venue, eng.cfg.hedge_venue)
    return f"{eng.cfg.symbol} · Entropy ↔ {hedge}"


def start_text(eng) -> str:
    mode = "тестовая запись (без сделок)" if eng.record_only \
        else "режим торговли"
    lines = ["▶️ MoneyClub_entropybot_2 запущен", f"Пара: {pair_title(eng)}",
             f"Режим: {mode}"]
    if not eng.record_only:
        cfg = eng.cfg
        lines.append(f"Пороги: центр {cfg.midline_bps:+g}, "
                     f"+{cfg.upper_bps:g} / −{cfg.lower_bps:g} bps")
        lines.append(strategy_name_line(eng))
        if getattr(eng, "autocalib_on", False):
            from .autocalib import window_label
            lines.append(f"Автокалибровка центра: вкл (медиана за "
                         f"{window_label(cfg.autocalib_window_hours)}, раз в "
                         f"{cfg.autocalib_every_hours:g} ч)")
    return "\n".join(lines)


def autocalib_text(eng, old: float, d) -> str:
    from .autocalib import window_label
    note = f"\n{d.reason}" if d.reason else ""
    return (f"🎯 Автокалибровка · {pair_title(eng)}\n"
            f"Центр: {old:+.1f} → {d.new_midline:+.1f} bps\n"
            f"Медиана за {window_label(eng.cfg.autocalib_window_hours)}: "
            f"{d.target:+.1f} ({d.minutes} мин){note}\n"
            f"Ручное значение: {eng.autocalib_anchor:+.1f}")


STOP_REASONS = {"manual": "вручную (Стоп)", "loss_limit": "стоп по убытку",
                "errors": "ошибки подряд"}


def finish_text(eng, s: dict) -> str:
    """s: what _finish_session computed (live) — see Engine._finish_session."""
    pnl, pct = s.get("pnl"), s.get("pnl_pct")
    head = "⏹ MoneyClub_entropybot_2 остановлен"
    lines = [head, f"Пара: {pair_title(eng)}"]
    res = _usd(pnl, sign=True)
    if pct is not None:
        res += f" ({pct:+.2f}%)"
    icon = "🟢" if (pnl or 0) > 0 else ("🔴" if (pnl or 0) < 0 else "⚪️")
    lines += [f"{icon} Результат: {res}",
              f"Оборот: {_usd(s.get('turnover'))} · сделок: {s.get('trades', 0)}",
              f"Время работы: {_hms(s.get('duration', 0))}"]
    if s.get("strategy"):
        lines.append(f"Стратегия: {s['strategy'].replace('Стратегия ', '№', 1)}")
    vc = volume_cost_text(s.get("volume_cost"), eng)
    if vc:
        lines.append(vc)
    el = edge_line(s.get("edge_compare"))
    if el:
        lines.append(el)
    reason = s.get("reason", "")
    lines.append("Остановка: " + STOP_REASONS.get(
        reason, reason.replace("halt: ", "аварийная: ") or "—"))
    fe, fh = s.get("funding_entropy"), s.get("funding_hedge")
    if fe is not None or fh is not None:
        hedge = VENUE_TITLES.get(eng.cfg.hedge_venue, eng.cfg.hedge_venue)
        part = lambda x: "—" if x is None else _usd(x, sign=True)  # noqa: E731
        lines.append(f"Funding: Entropy {part(fe)} · {hedge} {part(fh)}")
    closed = s.get("closed")
    lines.append("Позиции: " + {True: "закрыты",
                                False: "⚠️ НЕ ЗАКРЫЛИСЬ — проверьте биржи",
                                None: "—"}[closed])
    if s.get("dust_left"):
        lines.append(f"Остаток ${s['dust_left']:.2f} не закрыт — меньше "
                     f"минимального ордера, закройте вручную.")
    return "\n".join(lines)


def record_finish_text(eng) -> str:
    n = eng.recorder.rows_written if eng.recorder is not None else 0
    return (f"⏹ Тестовая запись остановлена\nПара: {pair_title(eng)}\n"
            f"Записано минут: {n}")


def _premium_line(eng) -> Optional[str]:
    p = eng.premium_bps()
    if p is None:
        return None
    line = f"Премия: {p:+.2f} bps"
    if not eng.record_only:
        line += f" (центр {eng.cfg.midline_bps:+g})"
    st = eng.premium_stats()
    if st is not None:
        lo, med, hi, _ = st
        line += f"\nЗа час: мин {lo:+.2f} · медиана {med:+.2f} · макс {hi:+.2f}"
    return line


def status_text(eng) -> str:
    import time
    up = _hms(time.time() - eng.start_ts)
    if eng.record_only:
        n = eng.recorder.rows_written if eng.recorder is not None else 0
        lines = [f"📊 {pair_title(eng)}",
                 f"Режим: тестовая запись · работает {up}",
                 f"Записано минут: {n}"]
        pl = _premium_line(eng)
        if pl:
            lines.append(pl)
        return "\n".join(lines)
    lines = [f"📊 {pair_title(eng)}", f"Режим: торговля · работает {up}",
             strategy_name_line(eng)]
    if eng.halted:
        lines.append(f"⚠️ Торговля остановлена: {eng.halt_reason}")
    lines.append(pnl_line(eng))
    lines.append(f"Оборот: {_usd(eng.turnover_usd())} · сделок: {eng.trades}")
    el = edge_line(eng.edge_compare())
    if el:
        lines.append(el)
    sl = strategy_line(eng)
    if sl:
        lines.append(sl)
    pos, bal = [], []
    for v in eng.venues.values():
        title = VENUE_TITLES.get(eng.cfg.hedge_venue, v.name) \
            if v.key == "hedge" else "Entropy"
        pos.append(f"{title} {v.position:+.6g}")
        bal.append(f"{title} {_usd(v.equity)}")
    lines.append("Позиции: " + " · ".join(pos))
    lines.append("Балансы: " + " · ".join(bal))
    pl = _premium_line(eng)
    if pl:
        lines.append(pl)
    if getattr(eng, "autocalib_on", False):
        lines.append(f"Автокалибровка: центр {eng.cfg.midline_bps:+.1f} "
                     f"(ручной {eng.autocalib_anchor:+.1f})"
                     + (" · ждёт закрытия позиции"
                        if eng.autocalib_waiting else ""))
    lim = [v for v in eng.venues.values() if eng.req_limited(v)]
    if lim:
        h = eng.req_budget[lim[0].key].get("headroom")
        if getattr(eng, "hl_shared", False):
            lines.append(f"Лимит запросов Hyperliquid исчерпан ({h:+d}): обе "
                         f"ноги на одном адресе — арбитраж стоит. Лимит "
                         f"растёт только от нового оборота на этом адресе; "
                         f"выход — отдельные ключи trade.xyz")
        else:
            lines.append(f"Лимит запросов Hyperliquid исчерпан ({h:+d}) — не "
                         f"чаще 1 ордера в 10 с")
    return "\n".join(lines)


def money(v: float) -> str:
    """Signed dollars; under $1 with 4 decimals — a trade of ~$20 earns
    cents and fractions of a cent, which 2 decimals would round to ±0.00."""
    d = 4 if abs(v) < 1 else 2
    r = round(v, d)
    if r == 0:
        return "$0"
    return f"{'+' if r > 0 else '-'}${abs(r):,.{d}f}"


def edge_line(c) -> Optional[str]:
    """c = (сделок, ожидалось $, получилось $) — see Engine.edge_compare."""
    if not c:
        return None
    n, exp, got = c
    return (f"По сделкам ({n}): ожидалось {money(exp)}, получилось "
            f"{money(got)}, разница {money(got - exp)}")


def strategy_name_line(eng) -> str:
    """'Стратегия: №2 · Сначала Entropy'."""
    from .strategy import strategy_title
    name = strategy_title(getattr(eng.cfg, "exec_mode", "simultaneous"))
    return "Стратегия: " + name.replace("Стратегия ", "№", 1)


def volume_cost_text(vc, eng) -> Optional[str]:
    """Strategy 3: 'Цена объёма: $1.40 за $10 000 на Entropy (лимит $2, по
    23 сделкам)'. vc = (cost, trades) from Engine.volume_cost()."""
    if getattr(eng.cfg, "exec_mode", "") != "volume" or not vc:
        return None
    cost, n = vc
    lim = getattr(eng.cfg, "volume_max_cost_usd", 2.0)
    if cost is None:
        return (f"Цена объёма: ещё не измерена ({n} сделок, нужно 10) · "
                f"лимит ${lim:g} за $10 000")
    word = "стоит" if cost > 0 else "приносит"
    return (f"Цена объёма: {word} ${abs(cost):.2f} за $10 000 на Entropy "
            f"(лимит ${lim:g}, по {n} сделкам)")


def strategy_line(eng) -> Optional[str]:
    """"промахов 3 из 10 · проскальзывание: Entropy 8.8 bps, хедж 0.1 bps
    (19) · к порогу входа +17.8 bps" — only the parts that apply; None when
    there is nothing to say. The strategy name is strategy_name_line."""
    from .strategy import tight_entropy_first
    cfg = eng.cfg
    parts = []
    mode = getattr(cfg, "exec_mode", "simultaneous")
    if tight_entropy_first(mode) and getattr(eng, "ef_attempts", 0):
        parts.append(f"Entropy не исполнилась {eng.ef_misses} раз из "
                     f"{eng.ef_attempts}")
    if mode == "volume":
        vc = volume_cost_text(eng.volume_cost(), eng)
        if vc:
            parts.append(vc)
        vch = eng.volume_charge_bps() if hasattr(eng, "volume_charge_bps") \
            else 0
        if vch > 0:
            parts.append(f"дороже лимита — к порогу открытия +{vch:.1f} bps")
    if getattr(cfg, "max_excess_bps", 0) > 0 and getattr(eng, "excess_skips", 0):
        parts.append(f"пропущено подозрительно больших сигналов: "
                     f"{eng.excess_skips}")
    slip = getattr(eng, "slip", None)
    if slip is not None:
        meds = []
        for key, title in (("entropy", "Entropy"), ("hedge", "хедж")):
            m, n = slip.median(key)
            if m is not None:
                meds.append(f"{title} {m:+.1f} bps")
        if meds:
            n = len(slip.recent("entropy"))
            parts.append("проскальзывание (медиана): " + ", ".join(meds)
                         + f" · {n} сделок")
    charge = eng.slip_charge_bps() if hasattr(eng, "slip_charge_bps") else 0
    if charge > 0:
        parts.append(f"поправка на потери: к порогу входа +{charge:.1f} bps")
    return " · ".join(parts) if parts else None


def pnl_line(eng) -> str:
    r = eng.session_result()
    if r is None:
        return "Результат сессии: читаю балансы…"
    base = eng.session_base_total
    pct = f" ({r / base * 100:+.2f}%)" if base else ""
    return f"Результат сессии: {_usd(r, sign=True)}{pct}"


def pnl_text(eng) -> str:
    if eng.record_only:
        return "Тестовая запись — сделок нет, результата нет."
    lines = [f"💰 {pair_title(eng)}", pnl_line(eng),
             f"Оборот: {_usd(eng.turnover_usd())} · сделок: {eng.trades}"]
    el = edge_line(eng.edge_compare())
    if el:
        lines.append(el)
    return "\n".join(lines)


def reply_for(eng, command: str) -> str:
    if command == "/status":
        return status_text(eng)
    if command == "/pnl":
        return pnl_text(eng)
    return HELP
