"""The result is the balance change; the sum of trade edges is only part of it.

Each arbitrage trade's fill edge is the price difference of its two legs,
and the two venues trade at different prices (the premium). So one trade
alone is "plus" or "minus" by design: with a premium below zero, selling
Entropy books a minus and buying it back books a plus. Only a full round
(open + close) is income. Whatever position is still open after the last
trade is closed by the position fixes and the Stop close, which are not
arbitrage trades and are not in that sum.

    balance change = trades (fill edges, fees at the configured rates)
                   + funding
                   + the rest: closing the leftover position (fixes, Stop),
                     the real fees vs the configured rates, rebates

Результат — это изменение баланса. Здесь он раскладывается на части.
"""
from __future__ import annotations

from typing import Iterable, List, Optional


def _num(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def money(v: Optional[float], sign: bool = True) -> str:
    if v is None:
        return "—"
    d = 4 if abs(v) < 1 else 2
    r = round(v, d)
    if r == 0:
        return "$0"
    s = f"${abs(r):,.{d}f}"
    return (("+" if r > 0 else "-") + s) if sign else s


def rest(pnl: float, trades: float, funding: float) -> float:
    """Part of the balance change that is neither trades nor funding."""
    return pnl - trades - funding


def breakdown_line(pnl: Optional[float], trades: Optional[float],
                   fund_e: Optional[float] = None,
                   fund_h: Optional[float] = None) -> Optional[str]:
    """'Из результата: сделки +$5.53 · funding -$0.12 · закрытие остатка
    позиции и прочее -$4.33' — None without the balance or the trades."""
    if pnl is None or trades is None:
        return None
    funding = (fund_e or 0.0) + (fund_h or 0.0)
    parts = [f"сделки {money(trades)}"]
    if fund_e is not None or fund_h is not None:
        parts.append(f"funding {money(funding)}")
    parts.append(f"закрытие остатка позиции и прочее "
                 f"{money(rest(pnl, trades, funding))}")
    return "Из результата: " + " · ".join(parts)


def sessions_breakdown(sessions: Iterable[dict]) -> Optional[dict]:
    """Sums over sessions.csv rows that carry both the balance change and
    the trades' fill edge: pnl, trades, funding_e, funding_h, rest, n."""
    out = dict(pnl=0.0, trades=0.0, funding_e=0.0, funding_h=0.0, n=0)
    for s in sessions:
        pnl, fill = _num(s.get("pnl_usd")), _num(s.get("fill_edge_usd"))
        if pnl is None or fill is None:
            continue
        out["pnl"] += pnl
        out["trades"] += fill
        out["funding_e"] += _num(s.get("funding_entropy_usd")) or 0.0
        out["funding_h"] += _num(s.get("funding_hedge_usd")) or 0.0
        out["n"] += 1
    if not out["n"]:
        return None
    out["rest"] = rest(out["pnl"], out["trades"],
                       out["funding_e"] + out["funding_h"])
    return out


def breakdown_lines(b: Optional[dict], hedge_title: str = "хедж",
                    indent: str = "  ") -> List[str]:
    """Russian lines for a sessions_breakdown() result."""
    if not b:
        return []
    return [
        f"{indent}Результат по балансу ({b['n']} сесс.): {money(b['pnl'])}",
        f"{indent}  из него сделки (сумма fill edge): {money(b['trades'])}",
        f"{indent}  funding: Entropy {money(b['funding_e'])} · {hedge_title} "
        f"{money(b['funding_h'])}",
        f"{indent}  закрытие остатка позиции, выравнивания, разница "
        f"комиссий: {money(b['rest'])}",
    ]


EXPLAIN = [
    "Результат — изменение баланса обеих бирж. Сумма по сделкам — только "
    "его часть:",
    "у каждой сделки в доход входит разница цен двух бирж (премия), поэтому "
    "доход даёт",
    "только круг «открытие + закрытие». Позиция, оставшаяся после последней "
    "сделки,",
    "закрывается выравниванием или Стопом — это и есть «закрытие остатка "
    "позиции».",
]
