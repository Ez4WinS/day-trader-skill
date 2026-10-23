"""trader-autopilot tests. They run offline: the bars are TSLA's real Oct 19 2026
five-minute bars (tests/fixtures/trader-entry/TSLA-2026-10-19-5m.json) and the
broker is a fake that records what the bot sends, so no order ever leaves.
"""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest

from _load import load, FIX

auto = load("trader-autopilot", "autopilot")

TSLA = os.path.join(FIX, "trader-entry", "TSLA-2026-10-19-5m.json")
DAY = datetime.date(2026, 10, 19)
DT = datetime.datetime


def at(hm):
    h, m = hm.split(":")
    return DT(2026, 10, 19, int(h), int(m), 0)


def bars():
    with open(TSLA) as fh:
        return auto.parse_yahoo(json.load(fh), DAY)


def cfg(**over):
    c = auto.load_config(None)
    c["autopilot"]["watchlist"] = ["TSLA"]
    for k, v in over.items():
        if k in auto.AUTO_DEFAULTS:
            c["autopilot"][k] = v
        else:
            c[k] = v
    return c


def book(**over):
    b = {"cash": 100000.0, "pnl_today": 0.0, "invested": 0.0, "open_risk": 0.0, "entries": 0, "orders": 0}
    b.update(over)
    return b


class FakeBroker(object):
    paper = True

    def __init__(self):
        self.placed, self.canceled, self.closed = [], [], []
        self.held, self.open_orders, self.all_orders = [], [], []
        self.acct = {"cash": "100000", "equity": "100000", "last_equity": "100000", "status": "ACTIVE"}

    def clock(self):
        return {"timestamp": "2026-10-19T10:15:04.123456789-04:00", "is_open": True,
                "next_open": "2026-10-20T09:30:00-04:00", "next_close": "2026-10-19T16:00:00-04:00"}

    def account(self):
        return self.acct

    def positions(self):
        return self.held

    def orders(self, status="open", after=None, symbols=None):
        return self.open_orders if status == "open" else self.all_orders

    def place(self, order):
        self.placed.append(order)
        return {"id": "o-%d" % len(self.placed), "status": "accepted"}

    def cancel(self, oid):
        self.canceled.append(oid)

    def close_position(self, sym):
        self.closed.append(sym)


class Signal(unittest.TestCase):
    def test_the_reel_trade_triggers_at_1015(self):
        s = auto.orb_signal(bars(), at("10:15"))
        self.assertEqual(s["status"], "triggered")
        self.assertEqual((s["hi"], s["lo"], s["mid"]), (362.18, 354.47, 358.32))
        self.assertEqual((s["at"], s["price"]), (at("10:15"), 364.18))

    def test_waits_while_the_1010_bar_is_still_open(self):
        s = auto.orb_signal(bars(), at("10:12"))
        self.assertEqual(s["status"], "waiting")

    def test_range_still_forming_before_945(self):
        self.assertEqual(auto.orb_signal(bars(), at("09:44"))["status"], "forming")

    def test_alpaca_time_with_nanoseconds(self):
        t, off = auto.parse_ts("2026-10-19T10:15:04.123456789-04:00")
        self.assertEqual((t, off), (DT(2026, 10, 19, 10, 15, 4), -4.0))
        self.assertEqual(auto.parse_ts("2026-10-19T14:15:00Z"), (DT(2026, 10, 19, 14, 15), 0.0))


class Plan(unittest.TestCase):
    def sig(self):
        return auto.orb_signal(bars(), at("10:15"))

    def test_reel_sizing_with_the_example_limits(self):
        order, why, n = auto.plan_trade("TSLA", self.sig(), cfg(budget=5000, max_loss_per_trade_usd=50,
                                        max_daily_loss_usd=150, max_position_pct=75, max_order_usd=4000,
                                        cash_buffer_pct=10), book(), at("10:15"))
        self.assertEqual(why, [])
        self.assertEqual((n["qty"], n["max_loss"], n["target"]), (8, 46.88, 370.04))
        self.assertEqual(order, {"symbol": "TSLA", "qty": "8", "side": "buy", "type": "market",
                                 "time_in_force": "day", "order_class": "bracket",
                                 "take_profit": {"limit_price": "370.04"}, "stop_loss": {"stop_price": "358.32"},
                                 "client_order_id": "ap-20261019-TSLA"})

    def test_default_1000_budget_buys_one_share(self):
        order, why, n = auto.plan_trade("TSLA", self.sig(), cfg(), book(), at("10:15"))
        self.assertEqual((why, n["qty"], n["max_loss"]), ([], 1, 5.86))

    def test_blocks(self):
        cases = [
            (cfg(), book(entries=3), at("10:15"), "trades: 3 of 3"),
            (cfg(), book(pnl_today=-26.0), at("10:15"), "daily loss"),
            (cfg(), book(), at("11:45"), "window"),
            (cfg(min_stop_pct=2.0), book(), at("10:15"), "too tight"),
            (cfg(blocked=["tsla"]), book(), at("10:15"), "blocked"),
            (cfg(max_loss_per_trade_usd=5), book(), at("10:15"), "not one whole share"),
            (cfg(target_r=0.5), book(), at("10:15"), "reward:risk"),
        ]
        for c, b, now, text in cases:
            order, why, _ = auto.plan_trade("TSLA", self.sig(), c, b, now)
            self.assertIsNone(order, text)
            self.assertIn(text, " ".join(why))

    def test_budget_caps_a_big_paper_account(self):
        _, why, _ = auto.plan_trade("TSLA", self.sig(), cfg(), book(invested=800.0), at("10:15"))
        self.assertIn("cash 0", " ".join(why))


class BotRun(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.broker = FakeBroker()

    def tearDown(self):
        shutil.rmtree(self.d)

    def bot(self, dry=False, c=None):
        b = auto.Bot(c or cfg(), self.broker, folder=self.d, dry_run=dry, out=io.StringIO(),
                     fetch=lambda s, now: bars(), sleep=lambda s: None)
        b.load_state(DAY)
        return b

    def test_places_one_bracket_order_on_its_own_and_never_twice(self):
        b = self.bot()
        b.tick(at("10:15"))
        self.assertEqual(len(self.broker.placed), 1)
        self.assertEqual(self.broker.placed[0]["stop_loss"], {"stop_price": "358.32"})
        b.tick(DT(2026, 10, 19, 10, 15, 40))
        b.tick(at("10:20"))
        self.assertEqual(len(self.broker.placed), 1)
        again = self.bot()   # a restart reads the state file
        again.tick(at("10:25"))
        self.assertEqual(len(self.broker.placed), 1)
        self.assertEqual(again.state["trades"][0]["order_id"], "o-1")

    def test_dry_run_sends_nothing(self):
        b = self.bot(dry=True)
        b.tick(at("10:15"))
        self.assertEqual(self.broker.placed, [])
        self.assertEqual(b.state["trades"][0]["status"], "dry-run")

    def test_a_breakout_it_did_not_see_live_is_skipped(self):
        b = self.bot()
        b.tick(at("10:40"))
        self.assertEqual(self.broker.placed, [])
        self.assertTrue(b.state["tried"]["TSLA"].startswith("missed"))

    def test_flat_by_cancels_the_legs_and_closes(self):
        b = self.bot()
        b.tick(at("10:15"))
        self.broker.held = [{"symbol": "TSLA", "qty": "1"}]
        self.broker.open_orders = [{"id": "o-1", "symbol": "TSLA", "status": "filled",
                                    "legs": [{"id": "tp", "status": "new"}, {"id": "sl", "status": "held"}]}]
        b.tick(at("15:55"))
        self.assertEqual(self.broker.canceled, ["tp", "sl"])
        self.assertEqual(self.broker.closed, ["TSLA"])
        self.assertTrue(b.state["flattened"])

    def test_report_from_fills(self):
        b = self.bot()
        b.tick(at("10:15"))
        self.broker.all_orders = [{"symbol": "TSLA", "side": "buy", "filled_qty": "1", "filled_avg_price": "364.20",
                                   "legs": [{"symbol": "TSLA", "side": "sell", "filled_qty": "1",
                                             "filled_avg_price": "370.04"}]}]
        rep = b.report(DT(2026, 10, 19, 16, 1))
        self.assertEqual((rep["pnl"], rep["wins"]), (5.84, 1))
        with open(os.path.join(self.d, "autopilot", "journal.csv")) as fh:
            self.assertIn("2026-10-19,paper,1,1,0,5.84,TSLA", fh.read())


class Safety(unittest.TestCase):
    def test_keys_only_from_the_environment(self):
        with self.assertRaises(auto.AutopilotError) as cm:
            auto.Alpaca("", "", paper=True)
        self.assertIn("never in a chat", str(cm.exception))

    def test_paper_by_default_and_live_needs_the_flag(self):
        self.assertEqual(auto.Alpaca("k", "s").base, auto.PAPER_URL)
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "config.json")
            with open(path, "w") as fh:
                json.dump({"paper": False}, fh)
            a = type("A", (), {"config": path, "live": False, "dir": d})()
            with self.assertRaises(auto.AutopilotError) as cm:
                auto.make_bot(a)
            self.assertIn("--live", str(cm.exception))
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
