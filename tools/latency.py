#!/usr/bin/env python3
"""How far this server is from the exchanges: round-trip time of a small
public API request to each venue (no keys), 5 tries each, median.

Пинг до бирж: время ответа на небольшой публичный запрос. Каждый замер
дописывается в logs/latency.csv (с именем сервера), и под результатом
показаны прошлые замеры — так видно, стало ли лучше после переезда, и
можно сравнить серверы между собой.

Usage:  python3 tools/latency.py [--history-file logs/latency.csv]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import socket
import statistics
import time
from datetime import datetime, timezone
import http.client
import urllib.parse

TRIES = 5
TIMEOUT = 8.0
TARGETS = [
    ("Hyperliquid (Entropy)", "https://api.hyperliquid.xyz/info",
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


HISTORY_HEADER = ["ts", "time_utc", "server", "target", "ms"]
HISTORY_SHOWN = 5


def server_name() -> str:
    """Имя сервера и его внешний адрес по DNS (без запросов наружу)."""
    host = socket.gethostname()
    try:
        ip = socket.gethostbyname(host)
    except OSError:
        ip = ""
    return f"{host} ({ip})" if ip and not ip.startswith("127.") else host


def save_history(path: str, server: str, results) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    new = not os.path.exists(path)
    now = time.time()
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M")
    with open(path, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(HISTORY_HEADER)
        for name, ms in results:
            w.writerow([f"{now:.0f}", stamp, server, name,
                        "" if ms is None else f"{ms:.0f}"])


def read_history(path: str):
    """[(time_utc, server, {target: ms})] — по замерам, старые первыми."""
    runs = {}
    try:
        with open(path, newline="") as fh:
            for r in csv.DictReader(fh):
                key = (r.get("ts"), r.get("time_utc"), r.get("server"))
                ms = r.get("ms")
                runs.setdefault(key, {})[r.get("target")] = (
                    float(ms) if ms else None)
    except FileNotFoundError:
        return []
    return [(k[1], k[2], v) for k, v in
            sorted(runs.items(), key=lambda kv: float(kv[0][0] or 0))]


def print_history(path: str) -> None:
    runs = read_history(path)[-HISTORY_SHOWN:]
    if len(runs) < 2:
        return
    names = [n for n, _, _ in TARGETS]
    short = {n: n.split(" (")[0] if "core" not in n else "Lighter core"
             for n in names}
    print(f"Последние замеры (мс), файл {path}:")
    print("  " + f"{'время UTC':<17}" + "".join(f"{short[n]:>14}"
                                               for n in names) + "  сервер")
    for when, server, vals in runs:
        cells = "".join(f"{'—' if vals.get(n) is None else f'{vals[n]:.0f}':>14}"
                        for n in names)
        print(f"  {when:<17}{cells}  {server}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--history-file", default="logs/latency.csv")
    args = ap.parse_args()
    server = server_name()
    print(f"Пинг до бирж с сервера {server} (медиана из 5 запросов, мс):\n")
    worst = 0.0
    results = []
    for name, url, payload in TARGETS:
        ms, err = measure(url, payload)
        results.append((name, ms))
        if ms is None:
            print(f"  {name:<34} нет ответа ({err})")
            continue
        worst = max(worst, ms) if "core" not in name else worst
        print(f"  {name:<34} {ms:6.0f} мс — {verdict(ms)}")
    print()
    try:
        save_history(args.history_file, server, results)
        print_history(args.history_file)
    except OSError as e:
        print(f"История замеров не сохранена: {e}\n")
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
