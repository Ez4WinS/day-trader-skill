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


class FakeBrain(object):
    def __init__(self, entry=None, exit=None):
        self.asked, self.exits = [], []
        self._entry = entry or {"decision": "buy", "confidence": 80, "target_r": 1.5, "catalyst": "deal",
                                "reason": "Neue Nachricht stützt den Ausbruch."}
        self._exit = exit or {"decision": "hold", "reason": "Nichts Neues."}

    def entry(self, ctx):
        self.asked.append(ctx)
        return dict(self._entry)

    def exit(self, ctx):
        self.exits.append(ctx)
        return dict(self._exit)


class FakeNews(object):
    def __init__(self):
        self.items = [{"time": "Mon 08:04", "t": "2026-10-19T08:04:00", "id": "a",
                       "title": "Tesla-SpaceX Merger Mania Strikes Wall Street Ahead of Earnings", "summary": ""}]

    def headlines(self, symbol, now):
        return list(self.items)

    def filings(self, symbol, now):
        return [{"form": "8-K", "filed": "2026-10-19T07:00", "items": ["8.01 other events"]}]


class FakeTelegram(object):
    on = True

    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True


class Claude(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.broker = FakeBroker()
        self.tg = FakeTelegram()

    def tearDown(self):
        shutil.rmtree(self.d)

    def bot(self, brain, dry=False):
        b = auto.Bot(cfg(), self.broker, folder=self.d, dry_run=dry, out=io.StringIO(),
                     fetch=lambda s, now: bars(), sleep=lambda s: None, brain=brain, news=FakeNews(), telegram=self.tg)
        b.load_state(DAY)
        return b

    def test_claude_reads_the_news_and_buys_with_its_target(self):
        brain = FakeBrain()
        self.bot(brain).tick(at("10:15"))
        ctx = brain.asked[0]
        self.assertEqual(ctx["price"]["breakout_close"], 364.18)
        self.assertEqual(ctx["price"]["stop_if_bought"], 358.32)
        self.assertIn("Merger", ctx["headlines"][0]["title"])
        self.assertEqual(ctx["sec_8k_filings"][0]["items"], ["8.01 other events"])
        self.assertEqual(self.broker.placed[0]["take_profit"], {"limit_price": "372.97"})   # 364.18 + 1.5 x 5.86
        self.assertIn("KAUF TSLA", self.tg.sent[-1])
        self.assertIn("Neue Nachricht", self.tg.sent[-1])

    def test_a_skip_sends_nothing_to_the_broker(self):
        brain = FakeBrain(entry={"decision": "skip", "confidence": 30, "target_r": 1.0, "catalyst": "",
                                 "reason": "Kein News-Grund."})
        b = self.bot(brain)
        b.tick(at("10:15"))
        self.assertEqual(self.broker.placed, [])
        self.assertTrue(b.state["tried"]["TSLA"].startswith("ai: skip"))
        self.assertIn("übersprungen", self.tg.sent[-1])

    def test_low_confidence_buy_is_a_skip(self):
        brain = FakeBrain(entry={"decision": "buy", "confidence": 40, "target_r": 1.0, "catalyst": "", "reason": "Vage."})
        self.bot(brain).tick(at("10:15"))
        self.assertEqual(self.broker.placed, [])

    def test_claude_cannot_break_a_risk_rule(self):
        brain = FakeBrain()
        b = auto.Bot(cfg(max_loss_per_trade_usd=5), self.broker, folder=self.d, out=io.StringIO(),
                     fetch=lambda s, now: bars(), sleep=lambda s: None, brain=brain, news=FakeNews(), telegram=self.tg)
        b.load_state(DAY)
        b.tick(at("10:15"))
        self.assertEqual(self.broker.placed, [])
        self.assertIn("Risiko-Regel", self.tg.sent[-1])

    def test_new_bad_news_sells_an_open_trade(self):
        brain = FakeBrain(exit={"decision": "sell", "reason": "Herabstufung."})
        b = self.bot(brain)
        b.tick(at("10:15"))
        self.broker.held = [{"symbol": "TSLA", "qty": "1", "avg_entry_price": "364.2", "current_price": "365"}]
        self.broker.open_orders = [{"id": "o-1", "symbol": "TSLA", "status": "filled",
                                    "legs": [{"id": "tp", "status": "new"}, {"id": "sl", "status": "held"}]}]
        b.tick(at("10:20"))
        self.assertEqual(brain.exits, [])   # no new headline yet: Claude is not asked
        b.news.items.append({"time": "Mon 10:18", "t": "2026-10-19T10:18:00", "id": "b",
                             "title": "Analyst downgrades Tesla", "summary": ""})
        b.tick(at("10:25"))
        self.assertEqual([h["title"] for h in brain.exits[0]["new_headlines"]], ["Analyst downgrades Tesla"])
        self.assertEqual(self.broker.canceled, ["tp", "sl"])
        self.assertEqual(self.broker.closed, ["TSLA"])
        self.assertIn("VERKAUF TSLA", self.tg.sent[-1])

    def test_dry_run_messages_are_marked(self):
        b = auto.Bot(cfg(), self.broker, folder=self.d, dry_run=True, out=io.StringIO(),
                     fetch=lambda s, now: bars(), sleep=lambda s: None, brain=FakeBrain(), news=FakeNews(),
                     telegram=self.tg)
        b.load_state(DAY)
        b.tick(at("10:15"))
        self.assertTrue(self.tg.sent[-1].startswith("[TEST]"))
        self.assertEqual(self.broker.placed, [])


class Res(object):
    def __init__(self, text, stop="end_turn"):
        self.stop_reason = stop
        self.content = [type("B", (), {"type": "text", "text": text})()]


class FakeClient(object):
    def __init__(self, res):
        self.calls = []
        msgs = type("M", (), {"create": lambda _s, **kw: (self.calls.append(kw), res)[1]})()
        self.beta = type("Beta", (), {"messages": msgs})()


@unittest.skipUnless(__import__("importlib").util.find_spec("anthropic"), "pip install anthropic")
class BrainCalls(unittest.TestCase):
    def test_request_shape_and_answer(self):
        ans = {"decision": "buy", "confidence": 77, "target_r": 2.0, "catalyst": "beat", "reason": "Zahlen stark."}
        client = FakeClient(Res(json.dumps(ans)))
        out = auto.Brain("claude-opus-5-5", "medium", client=client).entry({"symbol": "TSLA"})
        self.assertEqual(out, ans)
        kw = client.calls[0]
        self.assertEqual(kw["model"], "claude-opus-5-5")
        self.assertEqual(kw["fallbacks"], "default")
        self.assertEqual(kw["betas"], ["server-side-fallback-2026-07-01"])
        self.assertEqual(kw["output_config"]["effort"], "medium")
        self.assertEqual(kw["output_config"]["format"]["schema"], auto.ENTRY_SCHEMA)

    def test_refusal_or_garbage_is_never_a_buy(self):
        for res in (Res("", stop="refusal"), Res("not json")):
            out = auto.Brain("claude-opus-5-5", "medium", client=FakeClient(res)).entry({})
            self.assertEqual(out["decision"], "skip")
            out = auto.Brain("claude-opus-5-5", "medium", client=FakeClient(res)).exit({})
            self.assertEqual(out["decision"], "hold")


# SYNTHETIC feeds (shapes of Yahoo's RSS and SEC's submissions JSON, not real items)
RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Fresh headline</title><guid>g1</guid><pubDate>Mon, 19 Oct 2026 13:50:00 +0000</pubDate></item>
<item><title>Old headline</title><guid>g2</guid><pubDate>Fri, 16 Oct 2026 13:50:00 +0000</pubDate></item>
<item><title>From the future</title><guid>g3</guid><pubDate>Mon, 19 Oct 2026 18:00:00 +0000</pubDate></item>
</channel></rss>"""
SUB = {"filings": {"recent": {"form": ["8-K", "10-Q", "8-K"], "filingDate": ["2026-10-19", "2026-10-18", "2026-09-01"],
                              "acceptanceDateTime": ["2026-10-19T07:02:11.000Z", "x", "y"],
                              "items": ["2.02,9.01", "", "5.02"]}}}


class Feeds(unittest.TestCase):
    def test_rss_last_24h_in_new_york_time(self):
        items = auto.parse_rss(RSS, at("10:15"), -4.0, 24)
        self.assertEqual([(i["title"], i["time"]) for i in items], [("Fresh headline", "Mon 09:50")])

    def test_8k_items_are_named(self):
        f = auto.recent_8k(SUB, at("10:15"))
        self.assertEqual(f, [{"form": "8-K", "filed": "2026-10-19T07:02", "items": ["2.02 quarterly results", "9.01"]}])

    def test_telegram_errors_never_show_the_token(self):
        import urllib.error
        logs = []

        def boom(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"description": "no"}'))
        old, auto._urlopen = auto._urlopen, boom
        try:
            ok = auto.Telegram("123:SECRET", "42", log=logs.append).send("hi")
        finally:
            auto._urlopen = old
        self.assertFalse(ok)
        self.assertNotIn("SECRET", " ".join(logs))
        self.assertIn("HTTP 401 from Telegram", logs[0])


RDIR = os.path.join(FIX, "trader-autopilot")
research = auto.research


def monthly_chart():
    """SYNTHETIC monthly bars (not market data): 2016-11 to 2026-10, price doubling every 4 years, a dip in 2022."""
    ts, o, h, l, c = [], [], [], [], []
    d = DT(2016, 11, 1)
    while d <= DT(2026, 10, 1):
        k = (d.year - 2016) * 12 + d.month - 11
        p = 30.0 * 2 ** (k / 48.0) * (0.7 if d.year == 2022 else 1.0)
        ts.append(int((d - DT(1970, 1, 1)).total_seconds()))
        o.append(p); h.append(p * 1.05); l.append(p * 0.95); c.append(p)
        d = DT(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return {"chart": {"result": [{"meta": {"regularMarketPrice": c[-1], "longName": "Demo Inc."}, "timestamp": ts,
                                   "indicators": {"quote": [{"open": o, "high": h, "low": l, "close": c}]}}]}}


def daily_chart():
    """SYNTHETIC daily bars, weekdays 2016-10-03 to 2026-10-01 (not market data): the same doubling every 4 years."""
    ts, h, l, c = [], [], [], []
    d, start = DT(2016, 10, 3), DT(2016, 10, 1)
    while d <= DT(2026, 10, 1):
        if d.weekday() < 5:
            p = 30.0 * 2 ** ((d - start).days / (365.25 * 4))
            ts.append(int((d - DT(1970, 1, 1)).total_seconds())); h.append(p * 1.01); l.append(p * 0.99); c.append(p)
        d += datetime.timedelta(days=1)
    return {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [{"high": h, "low": l, "close": c}]}}]}}


class Research(unittest.TestCase):
    def test_apple_quarters_years_and_ttm_from_the_sec_facts(self):
        with open(os.path.join(RDIR, "companyfacts-CIK0000320193.json")) as fh:
            f = research.fundamentals(json.load(fh))
        self.assertEqual(f["ttm"], {"revenue_m": 466823.0, "net_income_m": 128930.0, "eps_diluted": 8.71})
        q = f["quarters"][0]
        self.assertEqual((q["fiscal"], q["revenue_m"], q["revenue_yoy_pct"], q["gross_margin_pct"], q["eps_diluted"]),
                         ("Q3 2026", 109417.0, 16.4, 50.1, 2.02))
        self.assertEqual((f["years"][0]["fiscal_year"], f["years"][0]["revenue_growth_pct"]), (2025, 6.4))

    def test_results_dates_and_the_next_estimate(self):
        sub = {"filings": {"recent": {"form": ["8-K", "10-Q", "8-K", "8-K", "8-K"],
                                      "filingDate": ["2026-07-30", "2026-07-31", "2026-04-30", "2026-01-29", "2026-03-02"],
                                      "items": ["2.02,9.01", "", "2.02", "2.02", "5.02"]}}}
        e = research.earnings_dates(sub, datetime.date(2026, 10, 19))
        self.assertEqual(e["past_results_8k"], ["2026-07-30", "2026-04-30", "2026-01-29"])
        self.assertEqual((e["next_results_estimate"], e["days_to_next_estimate"]), ("2026-10-29", 10))
        self.assertEqual(e["latest_report"], {"form": "10-Q", "filed": "2026-07-31"})

    def test_a_deliveries_8k_is_not_a_results_date(self):
        # Tesla files its deliveries under item 2.02 too, about three weeks before the results
        sub = {"filings": {"recent": {"form": ["8-K"] * 5,
                                      "filingDate": ["2026-10-21", "2026-10-02", "2026-07-22", "2026-07-02", "2026-04-22"],
                                      "items": ["2.02"] * 5}}}
        e = research.earnings_dates(sub, datetime.date(2026, 10, 23))
        self.assertEqual(e["past_results_8k"], ["2026-10-21", "2026-07-22", "2026-04-22"])
        self.assertEqual(e["other_2_02_filings"], ["2026-10-02", "2026-07-02"])
        self.assertEqual(e["next_results_estimate"], "2027-01-20")

    def test_results_overdue_are_due_now_not_next_quarter(self):
        sub = {"filings": {"recent": {"form": ["8-K"] * 3, "filingDate": ["2026-07-22", "2026-04-23", "2026-01-22"],
                                      "items": ["2.02"] * 3}}}
        e = research.earnings_dates(sub, datetime.date(2026, 10, 23))
        self.assertEqual((e["next_results_estimate"], e["days_to_next_estimate"]), ("2026-10-21", 0))
        self.assertIn("due now", e["note"])

    def test_ten_years_of_prices(self):
        p = research.price_history(monthly_chart(), daily_chart())
        y2022 = [y for y in p["by_year"] if y["year"] == 2022][0]
        y2023 = [y for y in p["by_year"] if y["year"] == 2023][0]
        self.assertAlmostEqual(y2022["return_pct"], -16.8, delta=0.1)   # a year of growth (x1.19) times the 0.7 dip
        self.assertAlmostEqual(y2023["return_pct"], 69.9, delta=0.1)    # back from the dip plus a year of growth
        self.assertEqual((p["range_low_month"], p["range_high_month"]), ("2016-11", "2026-10"))
        self.assertAlmostEqual(p["cagr_8y_pct"], 18.9, delta=0.1)   # doubling every 4 years, exactly 8 years back
        self.assertEqual(p["price_8y_ago"]["date"], "2018-10-01")
        self.assertEqual((p["high_52w"] > p["low_52w"], p["trend"]), (True, "up"))


class FakeResearchClient(object):
    """Pauses once (a long web-search turn), then finishes; the verdict call returns JSON."""

    def __init__(self):
        self.calls = []

        def create(_s, **kw):
            self.calls.append(kw)
            if "tools" in kw:
                if len([c for c in self.calls if "tools" in c]) == 1:
                    return Res("Teil 1. ", stop="pause_turn")
                return Res("Teil 2.")
            return Res(json.dumps({"rating": "good", "score": 72, "long_term_trend": "up",
                                   "next_results_date": "2026-10-29", "results_within_2_days": False,
                                   "summary": "Solide.", "strengths": ["Marge"], "risks": ["Zoelle"]}))
        self.beta = type("Beta", (), {"messages": type("M", (), {"create": create})()})()


@unittest.skipUnless(__import__("importlib").util.find_spec("anthropic"), "pip install anthropic")
class ResearchCalls(unittest.TestCase):
    def test_web_search_turn_is_resumed_and_the_verdict_parsed(self):
        client = FakeResearchClient()
        brain = auto.Brain("claude-opus-5-5", "medium", client=client)
        text, err = brain.research({"symbol": "AAPL"}, web_searches=5, effort="high")
        self.assertEqual((text, err), ("Teil 1. Teil 2.", None))
        first, second = client.calls[0], client.calls[1]
        self.assertEqual(first["tools"], [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}])
        self.assertEqual(first["output_config"], {"effort": "high"})
        self.assertEqual(second["messages"][1]["role"], "assistant")   # the paused turn is sent back, no extra user message
        self.assertEqual(len(second["messages"]), 2)
        self.assertEqual(brain.verdict("AAPL", text)["rating"], "good")


class FakeResearcher(object):
    def __init__(self):
        self.asked = []

    def get(self, symbol, now, force=False):
        self.asked.append(symbol)
        return {"symbol": symbol, "made": "2026-10-19T08:50:00",
                "verdict": {"rating": "avoid", "score": 20, "results_within_2_days": True},
                "numbers": {"price": {"trend": "down", "by_year": [{"year": 2025, "return_pct": -30.0}]},
                            "fundamentals": {"quarters": [{"fiscal": "Q3 2026"}], "ttm": {}},
                            "earnings": {"next_results_estimate": "2026-10-20", "days_to_next_estimate": 1}}}


class ResearchInTheDecision(unittest.TestCase):
    def test_the_entry_decision_sees_the_analysis(self):
        d = tempfile.mkdtemp()
        try:
            brain, rs = FakeBrain(), FakeResearcher()
            b = auto.Bot(cfg(), FakeBroker(), folder=d, out=io.StringIO(), fetch=lambda s, now: bars(),
                         sleep=lambda s: None, brain=brain, news=FakeNews(), telegram=FakeTelegram(), researcher=rs)
            b.load_state(DAY)
            b.tick(at("10:15"))
            r = brain.asked[0]["research"]
            self.assertEqual(rs.asked, ["TSLA"])
            self.assertEqual((r["verdict"]["rating"], r["price_10y"]["trend"], r["days_to_next_results"]), ("avoid", "down", 1))
            self.assertEqual(r["yearly_returns_pct"], {"2025": -30.0})
        finally:
            shutil.rmtree(d)

    def test_deep_once_then_only_what_is_new(self):
        d = tempfile.mkdtemp()
        try:
            brain = CountingBrain()
            rs = auto.Researcher(cfg(), d, None, brain, lambda m: None)
            world = {"price": 100.0, "results": "2026-07-30", "quarters": ["2026-06-27"]}

            def dossier(symbol, now, fundamentals=True):
                out = {"symbol": symbol, "name": "Demo Inc.",
                       "price": {"last_price": world["price"], "trend": "up"},
                       "earnings": {"past_results_8k": [world["results"]], "latest_report": {"filed": world["results"]}},
                       "recent_headlines": []}
                if fundamentals:
                    brain.fundamentals_fetched += 1
                    out["fundamentals"] = {"quarters": [{"quarter_end": q} for q in world["quarters"]]}
                return out
            rs.dossier = dossier
            day = lambda n: DT(2026, 10, 19 + n, 9, 0)

            first = rs.get("DEMO", day(0))                     # nothing stored: the deep analysis, once
            self.assertEqual((first["kind"], brain.deep, brain.updates), ("deep", 1, 0))
            rs.get("DEMO", day(0))                             # same day: the stored one, nothing fetched
            world["price"] = 104.0
            r = rs.get("DEMO", day(1))                         # next day, nothing new: numbers only, no Claude
            self.assertEqual((brain.deep, brain.updates, brain.fundamentals_fetched), (1, 0, 1))
            self.assertEqual(r["numbers"]["price"]["last_price"], 104.0)

            world.update(results="2026-10-29", quarters=["2026-09-26", "2026-06-27"])
            r = rs.get("DEMO", day(2))                         # new results: an update with only the new quarter
            self.assertEqual((brain.deep, brain.updates), (1, 1))
            self.assertEqual([q["quarter_end"] for q in brain.last_delta["new_quarters"]], ["2026-09-26"])
            self.assertEqual(brain.last_delta["why_update"], "new results filed 2026-10-29")
            self.assertEqual((r["kind"], r["results_seen"], r["report"]), ("updated", "2026-10-29", "Bericht v2"))

            world["price"] = 125.0                             # +20% since the report: another update
            rs.get("DEMO", day(3))
            self.assertEqual(brain.updates, 2)
            self.assertIn("price moved +20.2%", brain.last_delta["why_update"])

            r = rs.get("DEMO", DT(2027, 4, 20, 9, 0))         # after research_full_every_days: deep again
            self.assertEqual((brain.deep, r["kind"]), (2, "deep"))
        finally:
            shutil.rmtree(d)


class CountingBrain(object):
    def __init__(self):
        self.deep = self.updates = self.fundamentals_fetched = 0
        self.last_delta = None

    def research(self, dossier, web_searches=5, effort="high"):
        self.deep += 1
        return "Bericht v1", None

    def update_report(self, report, delta, web_searches=2, effort="medium"):
        self.updates += 1
        self.last_delta = delta
        return "Bericht v%d" % (self.updates + 1), None

    def verdict(self, symbol, report):
        return {"rating": "good", "score": 70}


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
