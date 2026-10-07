"""Telegram: start/summary texts, owner-only read-only commands, and that a
Telegram failure never reaches the trading engine."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb import telegram as tgmsg  # noqa: E402
from entropy_arb.telegram import TelegramBot  # noqa: E402
from test_risk import make_engine  # noqa: E402


def _upd(uid, chat, text):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


def test_only_owner_chat_commands_are_read():
    tg = TelegramBot("t", "123")
    assert tg.command_of(_upd(1, 123, "/status")) == "/status"
    assert tg.command_of(_upd(2, 123, "/pnl@my_bot extra")) == "/pnl"
    assert tg.command_of(_upd(3, 999, "/status")) is None     # чужой чат
    assert tg.command_of({"update_id": 4, "message": {"chat": {"id": 123}}}) \
        is None                                                # не текст
    assert not TelegramBot("t", "").enabled
    assert not TelegramBot("", "1").enabled


def test_texts_for_live_engine(tmp_path):
    eng = make_engine(str(tmp_path), epos=0.0129, hpos=-0.0129)
    eng.record_only = False
    eng.trades = 12
    eng.session_base_total = 127.0
    t = tgmsg.start_text(eng)
    assert "▶️" in t and "SNDK" in t and "режим торговли" in t
    st = tgmsg.status_text(eng)
    assert "Режим: торговля" in st and "сделок: 12" in st
    assert "Позиции:" in st and "Балансы:" in st
    s = {"pnl": -0.42, "pnl_pct": -0.33, "turnover": 1234.5, "trades": 12,
         "duration": 3725, "reason": "loss_limit", "closed": True,
         "dust_left": 1.55}
    f = tgmsg.finish_text(eng, s)
    assert "🔴" in f and "-$0.42 (-0.33%)" in f and "$1,234.50" in f
    assert "стоп по убытку" in f and "закрыты" in f and "1 ч 02 мин" in f
    assert "Остаток $1.55" in f
    s2 = dict(s, pnl=0.5, pnl_pct=0.4, reason="manual", closed=False,
              dust_left=0.0)
    f2 = tgmsg.finish_text(eng, s2)
    assert "🟢" in f2 and "+$0.50" in f2 and "НЕ ЗАКРЫЛИСЬ" in f2
    assert "Остаток" not in f2
    assert "Результат сессии" in tgmsg.pnl_text(eng)
    assert "/status" in tgmsg.reply_for(eng, "/start")


def test_texts_for_test_recording(tmp_path):
    eng = make_engine(str(tmp_path))
    eng.record_only = True
    eng.recorder = None
    eng._prem_hist.append((time.time(), -8.0))
    assert "тестовая запись" in tgmsg.start_text(eng)
    st = tgmsg.status_text(eng)
    assert "тестовая запись" in st and "Записано минут" in st
    assert "сделок нет" in tgmsg.pnl_text(eng)
    assert "Записано минут: 0" in tgmsg.record_finish_text(eng)


class FakeTG:
    """TelegramBot stand-in: serves queued updates, records sent texts."""

    def __init__(self, batches, chat="123", fail_send=False):
        self.chat_id = chat
        self.batches = list(batches)
        self.sent = []
        self.fail_send = fail_send
        self.enabled = True

    async def skip_backlog(self):
        pass

    async def get_updates(self, timeout=25):
        if self.batches:
            return self.batches.pop(0)
        await asyncio.sleep(0.01)
        return []

    command_of = TelegramBot.command_of

    async def send(self, text):
        if self.fail_send:
            raise RuntimeError("telegram down")
        self.sent.append(text)
        return True

    async def close(self):
        pass


def test_command_loop_answers_owner_and_ignores_others(tmp_path):
    async def go():
        eng = make_engine(str(tmp_path))
        eng.record_only = False
        eng.telegram = FakeTG([[_upd(1, 999, "/status"),
                                _upd(2, 123, "/pnl"),
                                _upd(3, 123, "/status")]])
        task = asyncio.create_task(eng._tg_commands_loop())
        for _ in range(100):
            if len(eng.telegram.sent) >= 2:
                break
            await asyncio.sleep(0.01)
        eng.stop.set()
        await asyncio.wait_for(task, 2)
        return eng.telegram.sent
    sent = asyncio.run(go())
    assert len(sent) == 2                       # чужой /status не отвечен
    assert sent[0].startswith("💰") and sent[1].startswith("📊")


def test_telegram_failure_never_reaches_the_engine(tmp_path):
    async def go():
        eng = make_engine(str(tmp_path))
        eng.telegram = FakeTG([], fail_send=True)
        await eng._tg_send("x")                 # must not raise
        return True
    assert asyncio.run(go())


def test_engine_has_no_telegram_without_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    eng = make_engine(str(tmp_path))
    assert eng.telegram is None


def test_menu_connect_finds_chat_and_saves(tmp_path, monkeypatch):
    import club
    monkeypatch.chdir(tmp_path)
    tok = "1234567890:" + "A" * 35
    calls = []

    def fake_call(token, method, payload=None):
        calls.append((method, payload))
        if method == "getMe":
            return {"ok": True, "result": {"username": "my_mc_bot"}}
        if method == "getUpdates" and payload == {"timeout": 0}:
            return {"ok": True, "result": [
                {"update_id": 7, "message": {"chat": {"id": -100, "type": "group"}}},
                {"update_id": 8, "message": {"chat": {"id": 555, "type": "private",
                                                      "first_name": "Gowal"}}}]}
        return {"ok": True, "result": []}
    monkeypatch.setattr(club, "tg_call", fake_call)
    monkeypatch.setattr(club.getpass, "getpass", lambda prompt="": tok)
    monkeypatch.setattr(club, "confirm", lambda q, default=True: True)
    monkeypatch.setattr(club, "pause", lambda: None)
    monkeypatch.setattr(club, "header", lambda t="": None)
    club.telegram_connect()
    env = club.read_env()
    assert env["TELEGRAM_BOT_TOKEN"] == tok
    assert env["TELEGRAM_CHAT_ID"] == "555"          # private chat, not group
    assert ("getUpdates", {"offset": 9, "timeout": 0}) in calls
    assert any(m == "sendMessage" and p["chat_id"] == 555 for m, p in calls)


def test_menu_rejects_bad_token(tmp_path, monkeypatch):
    import club
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(club.getpass, "getpass", lambda prompt="": "not-a-token")
    monkeypatch.setattr(club, "tg_call", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call Telegram")))
    monkeypatch.setattr(club, "pause", lambda: None)
    monkeypatch.setattr(club, "header", lambda t="": None)
    club.telegram_connect()
    assert "TELEGRAM_BOT_TOKEN" not in club.read_env()
