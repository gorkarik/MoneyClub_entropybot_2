#!/usr/bin/env python3
"""Hyperliquid request budget of the trading address: check it, and buy
more (the exchange's reserveRequestWeight action, 0.0005 USDC each).

Every order the bot sends — filled or not — spends one request. The budget
is 10 000 + 1 per USDC ever traded on the address; past it, Hyperliquid
lets through about one order per 10 seconds, and the bot practically stops.

Лимит запросов Hyperliquid: проверить и докупить.

Usage:
    python3 tools/hl_requests.py status [--xyz]
    python3 tools/hl_requests.py buy N [--xyz] --yes
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

API = "https://api.hyperliquid.xyz"
PRICE_USDC = 0.0005            # per request (Hyperliquid documentation)
MAX_BUY = 100_000              # one purchase at most ($50) — a typo guard


def post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        API + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "User-Agent": "moneyclub"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def keys(xyz: bool):
    """(private key, account address) from .env — the same rule as the bot:
    the address may be empty (then it is the key's own wallet)."""
    from dotenv import load_dotenv
    load_dotenv(".env")
    if xyz:
        k = (os.getenv("HL_PRIVATE_KEY_XYZ") or "").strip()
        a = (os.getenv("HL_ACCOUNT_ADDRESS_XYZ") or "").strip()
        if k:
            return k, a
    return ((os.getenv("HL_PRIVATE_KEY") or "").strip(),
            (os.getenv("HL_ACCOUNT_ADDRESS") or "").strip())


def address(key: str, addr: str) -> str:
    if addr:
        return addr
    if not key:
        raise SystemExit("Нет ключа Hyperliquid в .env (Настройки → Ключи).")
    from eth_account import Account
    return Account.from_key(key).address


def budget(addr: str) -> dict:
    r = post("/info", {"type": "userRateLimit", "user": addr})
    used = int(r.get("nRequestsUsed") or 0)
    cap = int(r.get("nRequestsCap") or 0)
    surplus = int(r.get("nRequestsSurplus") or 0)
    return {"used": used, "cap": cap, "surplus": surplus,
            "free": cap + surplus - used,
            "volume": float(r.get("cumVlm") or 0.0)}


def show(addr: str) -> dict:
    b = budget(addr)
    print(f"Адрес: {addr}")
    print(f"  потрачено запросов:   {b['used']:,}")
    print(f"  лимит (10 000 + оборот ${b['volume']:,.0f}): {b['cap']:,}")
    print(f"  куплено сверх лимита: {b['surplus']:,}")
    free = b["free"]
    if free > 0:
        print(f"  осталось:             {free:,}")
    else:
        print(f"  ПЕРЕРАСХОД:           {-free:,} — Hyperliquid пропускает "
              f"примерно 1 ордер в 10 секунд")
    return b


def buy(key: str, weight: int) -> dict:
    """Sign and send reserveRequestWeight (an L1 action, signed like an
    order) — the same signing path the bot uses for orders."""
    from eth_account import Account
    from hyperliquid.utils import signing
    wallet = Account.from_key(key)
    action = {"type": "reserveRequestWeight", "weight": int(weight)}
    nonce = int(time.time() * 1000)
    sig = signing.sign_l1_action(wallet, action, None, nonce, None, True)
    return post("/exchange", {"action": action, "nonce": nonce,
                              "signature": sig, "vaultAddress": None,
                              "expiresAfter": None})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status", "buy"])
    ap.add_argument("n", nargs="?", type=int, default=0)
    ap.add_argument("--xyz", action="store_true",
                    help="the trade.xyz wallet (HL_*_XYZ keys)")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    key, addr0 = keys(a.xyz)
    addr = address(key, addr0)
    if a.cmd == "status":
        show(addr)
        return
    if not 1 <= a.n <= MAX_BUY:
        raise SystemExit(f"Количество — от 1 до {MAX_BUY:,}.")
    if not key:
        raise SystemExit("Нет ключа Hyperliquid в .env (Настройки → Ключи).")
    if not a.yes:
        raise SystemExit("Нужно подтверждение (--yes).")
    try:
        r = buy(key, a.n)
    except ImportError:
        raise SystemExit("Не установлены библиотеки для торговли "
                         "(pip install -r requirements-live.txt).")
    if r.get("status") == "ok":
        print(f"✔ Куплено {a.n:,} запросов за ≈ ${a.n * PRICE_USDC:.2f} USDC.")
    else:
        print(f"✘ Hyperliquid отказал: {json.dumps(r, ensure_ascii=False)[:300]}")
        sys.exit(1)
    time.sleep(1.0)
    print()
    show(addr)


if __name__ == "__main__":
    main()
