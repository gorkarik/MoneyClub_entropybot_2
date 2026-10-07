#!/usr/bin/env python3
"""How far this server is from the exchanges: round-trip time of a small
public API request to each venue (no keys), 5 tries each, median.

Замер связи с биржами: время ответа на небольшой публичный запрос.

Usage:  python3 tools/latency.py
"""
from __future__ import annotations

import json
import statistics
import time
import http.client
import urllib.parse

TRIES = 5
TIMEOUT = 8.0
TARGETS = [
    ("Hyperliquid (Entropy, trade.xyz)", "https://api.hyperliquid.xyz/info",
     {"type": "exchangeStatus"}),
    # any HTTP answer counts: only the round trip is measured
    ("Lighter RH", "https://api.rh.lighter.xyz/", None),
    ("Lighter (core)", "https://mainnet.zklighter.elliot.ai/", None),
]


def measure(url: str, payload):
    """(median ms of the warm tries, error text or None). One connection is
    kept open, like the bot keeps it: the first request also pays DNS + TLS
    and is not counted."""
    u = urllib.parse.urlsplit(url)
    conn = http.client.HTTPSConnection(u.hostname, u.port or 443,
                                       timeout=TIMEOUT)
    body = None if payload is None else json.dumps(payload)
    headers = {"Content-Type": "application/json", "User-Agent": "moneyclub"}
    method = "GET" if payload is None else "POST"
    times, err = [], None
    try:
        for i in range(TRIES + 1):
            t0 = time.perf_counter()
            try:
                conn.request(method, u.path or "/", body=body, headers=headers)
                conn.getresponse().read()   # any status: the server answered
            except Exception as e:          # noqa: BLE001 — shown to the user
                err = type(e).__name__
                conn.close()
                continue
            if i:
                times.append((time.perf_counter() - t0) * 1000.0)
    finally:
        conn.close()
    return (statistics.median(times) if times else None), err


def verdict(ms: float) -> str:
    if ms < 30:
        return "отлично — сервер рядом"
    if ms < 100:
        return "хорошо"
    if ms < 200:
        return "заметно: сигнал успевает сдвинуться, пока идёт ордер"
    return "далеко: стоит подумать о сервере ближе к бирже"


def main() -> None:
    print("Связь с биржами с этого сервера (медиана из 5 запросов, мс):\n")
    worst = 0.0
    for name, url, payload in TARGETS:
        ms, err = measure(url, payload)
        if ms is None:
            print(f"  {name:<34} нет ответа ({err})")
            continue
        worst = max(worst, ms) if "core" not in name else worst
        print(f"  {name:<34} {ms:6.0f} мс — {verdict(ms)}")
    print()
    print("Это время запроса «туда и обратно» без ключей. Ордер идёт примерно "
          "столько же.")
    print("Чем меньше, тем меньше цена успевает уйти между сигналом и "
          "исполнением.")
    print("Lighter core важен, только если вы торгуете пары с ним.")
    if worst >= 200:
        print("\nДо Hyperliquid или Lighter RH больше 200 мс — переезд сервера "
              "ближе к бирже\nможет заметно уменьшить проскальзывание. Сравните "
              "замер с сервера в другом регионе.")


if __name__ == "__main__":
    main()
