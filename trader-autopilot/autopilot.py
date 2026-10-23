#!/usr/bin/env python3
"""Autopilot for trader-autopilot: trades the opening-range breakout on its own
on an Alpaca account. No yes per order: the rules in trader/config.json are the
permission. Standard library only. Long side only. Paper account by default.

    python3 autopilot.py run                  # trade today's session, then write the report
    python3 autopilot.py run --dry-run        # same decisions, no orders sent
    python3 autopilot.py run --forever        # every session until you stop it (Ctrl+C)
    python3 autopilot.py status               # account, clock, today's state
    python3 autopilot.py report               # today's fills and P/L from Alpaca
    python3 autopilot.py flatten              # cancel the bot's open orders, close its positions

What it does in a session (New York time):
  09:30-09:45  builds each watchlist stock's opening range from the 9:30, 9:35
               and 9:40 five-minute bars.
  09:45-11:30  after every closed bar, a stock whose bar CLOSES above its range
               high triggers (first close only; a breakout it did not see live
               is skipped, never chased). Strongest breakout first.
               Each trigger goes through the risk rules below; a PASS is sent
               at once as ONE bracket order: buy at market, stop-loss at the
               range midpoint, take-profit at target_r times the risk.
  15:55        flat_by: cancels the bot's open orders and closes its positions.
  after 16:00  writes trader/autopilot/<date>.json and a line in journal.csv.

Risk rules (trader/config.json, same keys as trader-risk): max_loss_per_trade_usd,
max_daily_loss_usd (today's loss plus the risk still open plus this trade),
max_trades_per_day, max_orders_per_day, min_reward_risk, no_entries_before,
no_entries_after, flat_by, budget (the bot never uses more than this, even on a
bigger paper account), cash_buffer_pct, max_position_pct, max_order_usd,
min_order_usd, blocked. The "autopilot" block adds: watchlist, entries_until,
target_r, min_stop_pct, data ("yahoo" or "alpaca").

Keys: ALPACA_API_KEY and ALPACA_SECRET_KEY environment variables, never in a
file in this folder and never in a chat. "paper": true (the default) uses
paper-api.alpaca.markets. A live account needs "paper": false in the config
AND --live on the command line.

Data: Yahoo's free chart JSON by default (consolidated bars, unofficial, can
lag); "data": "alpaca" uses Alpaca's free IEX bars instead (only IEX trades).
The clock, the account, orders and positions always come from Alpaca.

Files: trader/autopilot/state-<date>.json (what it saw and did, so a restart
never doubles an order), trader/autopilot/autopilot.log, <date>.json, journal.csv.
"""

import argparse
import csv
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
YAHOO_HOSTS = ("query2", "query1")
BAR = datetime.timedelta(minutes=5)
OPEN = datetime.time(9, 30)
OR_BARS = 3            # 9:30, 9:35, 9:40: the first 15 minutes
PAUSE = 0.3            # seconds between data requests
GRACE = 3              # seconds after a bar closes before asking for it
SETTLE = 90            # seconds a closed bar may take to show up in the data
STALE = datetime.timedelta(minutes=8)   # a trigger older than this was not seen live
CENT = 0.005

# the 50 biggest and best-known US stocks; override with "autopilot": {"watchlist": [...]}
WATCHLIST = ("AAPL MSFT NVDA AMZN GOOGL META TSLA BRK-B AVGO JPM LLY V UNH XOM MA WMT JNJ PG HD COST "
             "ORCL NFLX ABBV BAC KO CVX MRK PEP CRM AMD ADBE TMO CSCO MCD ABT WFC DIS IBM QCOM INTC "
             "PLTR UBER NKE PYPL BA GS CAT PFE SBUX T").split()

DEFAULTS = {
    "budget": 1000, "cash_buffer_pct": 5, "max_position_pct": 40, "max_order_usd": 900, "min_order_usd": 5,
    "max_loss_per_trade_usd": 10, "max_daily_loss_usd": 30, "max_trades_per_day": 3,
    "max_orders_per_day": 10, "min_reward_risk": 1.0, "no_entries_before": "09:45",
    "no_entries_after": "15:30", "flat_by": "15:55", "paper": True, "blocked": [],
}
AUTO_DEFAULTS = {"watchlist": WATCHLIST, "entries_until": "11:30", "target_r": 1.0,
                 "min_stop_pct": 0.1, "data": "yahoo"}


class AutopilotError(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def hhmm(text, what):
    m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", str(text or "").strip())
    if not m:
        raise AutopilotError("%s wants a New York time like 11:30, got %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def money(x):
    return ("-$%.2f" % -x) if x < 0 else ("$%.2f" % x)


def cents_down(x):
    return int(x * 100 + 1e-6) / 100.0


def parse_ts(text):
    """Alpaca time "2026-10-19T10:15:02.123456789-04:00" or "...Z" -> (naive local time, utc offset hours)."""
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:?\d\d)$", str(text).strip())
    if not m:
        raise AutopilotError("cannot read the time %r" % text)
    local = datetime.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
    z = m.group(2)
    if z == "Z":
        return local, 0.0
    sign = -1 if z[0] == "-" else 1
    digits = z[1:].replace(":", "")
    return local, sign * (int(digits[:2]) + int(digits[2:]) / 60.0)


def load_config(path):
    cfg = dict(DEFAULTS)
    auto = dict(AUTO_DEFAULTS)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                user = json.load(fh)
        except json.JSONDecodeError as e:
            raise AutopilotError("%s is not valid JSON: %s" % (path, e))
        auto.update(user.pop("autopilot", None) or {})
        cfg.update({k: v for k, v in user.items() if v is not None})
    cfg["autopilot"] = auto
    for k in ("no_entries_before", "no_entries_after", "flat_by"):
        hhmm(cfg[k], k)
    hhmm(auto["entries_until"], "autopilot.entries_until")
    if auto["data"] not in ("yahoo", "alpaca"):
        raise AutopilotError('autopilot.data is "yahoo" or "alpaca", got %r' % auto["data"])
    auto["watchlist"] = [str(s).upper().strip() for s in auto["watchlist"] if str(s).strip()]
    if not auto["watchlist"]:
        raise AutopilotError("the autopilot watchlist is empty")
    return cfg


# ---------------------------------------------------------------- HTTP

_urlopen = urllib.request.urlopen   # tests swap this


def http_json(url, headers=None, method="GET", body=None, timeout=20):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = {"User-Agent": UA, "Accept": "application/json"}
    if data is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with _urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8", "replace")).get("message")
        except Exception:
            msg = None
        raise AutopilotError("HTTP %s from %s%s" % (e.code, url.split("?")[0], (": " + msg) if msg else ""))
    except urllib.error.URLError as e:
        raise AutopilotError("could not reach %s: %s" % (url.split("?")[0], e.reason))
    return json.loads(raw.decode("utf-8")) if raw.strip() else None


class Alpaca(object):
    """The few Trading API calls the bot needs. Keys come from the environment only."""

    def __init__(self, key, secret, paper=True):
        if not key or not secret:
            raise AutopilotError("set ALPACA_API_KEY and ALPACA_SECRET_KEY in your environment "
                                 "(your terminal or Claude Code's MCP/env setup), never in a chat")
        self.base = PAPER_URL if paper else LIVE_URL
        self.paper = paper
        self._h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    def _call(self, method, path, query=None, body=None):
        url = self.base + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        return http_json(url, self._h, method, body)

    def clock(self):
        return self._call("GET", "/v2/clock")

    def account(self):
        return self._call("GET", "/v2/account")

    def positions(self):
        return self._call("GET", "/v2/positions") or []

    def orders(self, status="open", after=None, symbols=None):
        q = {"status": status, "nested": "true", "limit": 500}
        if after:
            q["after"] = after
        if symbols:
            q["symbols"] = ",".join(symbols)
        return self._call("GET", "/v2/orders", q) or []

    def place(self, order):
        return self._call("POST", "/v2/orders", body=order)

    def cancel(self, order_id):
        return self._call("DELETE", "/v2/orders/" + urllib.parse.quote(order_id))

    def close_position(self, symbol):
        return self._call("DELETE", "/v2/positions/" + urllib.parse.quote(symbol))

    def bars_iex(self, symbol, start_utc):
        q = {"timeframe": "5Min", "start": start_utc, "feed": "iex", "limit": 100}
        url = DATA_URL + "/v2/stocks/" + urllib.parse.quote(symbol) + "/bars?" + urllib.parse.urlencode(q)
        return http_json(url, self._h)


# ---------------------------------------------------------------- bars

def parse_yahoo(js, day):
    """Yahoo chart JSON -> today's regular-session 5-minute bars as dicts with a naive New York "t"."""
    try:
        r = js["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        return []
    off = r.get("meta", {}).get("gmtoffset", 0)
    q = (r.get("indicators", {}).get("quote") or [{}])[0]
    out = []
    for i, ts in enumerate(r.get("timestamp") or []):
        t = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=ts + off)
        if t.date() != day or t.second or t.minute % 5 or t.time() < OPEN or t.time() >= datetime.time(16, 0):
            continue
        vals = [q.get(k, [None] * (i + 1))[i] for k in ("open", "high", "low", "close")]
        if None in vals:
            continue
        out.append({"t": t, "o": vals[0], "h": vals[1], "l": vals[2], "c": vals[3]})
    return out


def parse_alpaca_bars(js, ny_offset_h):
    out = []
    for b in (js or {}).get("bars") or []:
        utc, _ = parse_ts(b["t"])
        out.append({"t": utc + datetime.timedelta(hours=ny_offset_h), "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]})
    return out


def fetch_yahoo(symbol, day):
    last = None
    for host in YAHOO_HOSTS:
        url = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m" % (
            host, urllib.parse.quote(symbol))
        try:
            return parse_yahoo(http_json(url), day)
        except AutopilotError as e:
            last = e
    raise last


# ---------------------------------------------------------------- the rule

def orb_signal(bars, now):
    """Opening-range breakout on closed 5-minute bars, as of naive New York time `now`."""
    done = [b for b in bars if b["t"] + BAR <= now]
    first = datetime.datetime.combine(now.date(), OPEN)
    rng = done[:OR_BARS]
    if len(rng) < OR_BARS:
        return {"status": "forming"}
    if [b["t"] for b in rng] != [first + BAR * i for i in range(OR_BARS)]:
        return {"status": "no-data", "why": "the opening-range bars are incomplete"}
    hi = max(b["h"] for b in rng)
    lo = min(b["l"] for b in rng)
    sig = {"hi": round(hi, 2), "lo": round(lo, 2), "mid": cents_down((hi + lo) / 2.0), "last_bar": done[-1]["t"]}
    for b in done[OR_BARS:]:
        if b["c"] > hi + 1e-9:
            sig.update(status="triggered", at=b["t"] + BAR, price=round(b["c"], 2), over=b["c"] / hi - 1)
            return sig
    sig["status"] = "waiting"
    return sig


def plan_trade(symbol, sig, cfg, book, now):
    """One trigger -> (order or None, reasons it is blocked, the numbers). `book` is today's picture."""
    auto = cfg["autopilot"]
    entry, stop = sig["price"], sig["mid"]
    risk = round(entry - stop, 2)
    target = round(entry + float(auto["target_r"]) * risk, 2)
    why = []
    start = hhmm(cfg["no_entries_before"], "no_entries_before")
    until = min(hhmm(cfg["no_entries_after"], "no_entries_after"), hhmm(auto["entries_until"], "entries_until"))
    if not (start <= now.time() <= until):
        why.append("window: %s is outside %s-%s" % (now.strftime("%H:%M"), start.strftime("%H:%M"), until.strftime("%H:%M")))
    if symbol in [str(s).upper() for s in cfg.get("blocked") or []]:
        why.append("blocked: %s is on your blocked list" % symbol)
    if risk <= 0:
        why.append("stop: the stop %.2f is not below the entry %.2f" % (stop, entry))
    elif risk < entry * float(auto["min_stop_pct"]) / 100.0 - 1e-9:
        why.append("stop: %.2f a share is too tight to survive the spread (min %s%% of the price)"
                   % (risk, auto["min_stop_pct"]))
    rr = float(auto["target_r"])
    if rr < float(cfg["min_reward_risk"]) - 1e-9:
        why.append("reward:risk %.2f is under your minimum %s" % (rr, cfg["min_reward_risk"]))
    if book["entries"] >= int(cfg["max_trades_per_day"]):
        why.append("trades: %d of %d entries today" % (book["entries"], int(cfg["max_trades_per_day"])))
    if book["orders"] >= int(cfg["max_orders_per_day"]):
        why.append("orders: %d of %d orders today" % (book["orders"], int(cfg["max_orders_per_day"])))
    qty = 0
    if risk > 0:
        budget = float(cfg["budget"])
        buffer_usd = budget * float(cfg["cash_buffer_pct"]) / 100.0
        free = min(float(book["cash"]), budget - book["invested"]) - buffer_usd
        limits = {
            "max loss": int(float(cfg["max_loss_per_trade_usd"]) / risk + 1e-9),
            "position cap": int(budget * float(cfg["max_position_pct"]) / 100.0 / entry + 1e-9),
            "max order": int(float(cfg["max_order_usd"]) / entry + 1e-9),
            "cash": max(0, int(free / entry + 1e-9)),
        }
        qty = min(limits.values())
        if qty < 1:
            why.append("size: not one whole share fits (%s)" % ", ".join("%s %d" % kv for kv in limits.items()))
        elif qty * entry < float(cfg["min_order_usd"]) - CENT:
            why.append("size: %s is under min_order_usd" % money(qty * entry))
        max_loss = qty * risk
        used = max(0.0, -book["pnl_today"]) + book["open_risk"]
        daily = float(cfg["max_daily_loss_usd"])
        if qty >= 1 and used + max_loss > daily + CENT:
            why.append("daily loss: %s already lost or at risk today plus %s for this one is over your %s limit"
                       % (money(used), money(max_loss), money(daily)))
    nums = {"entry": entry, "stop": stop, "target": target, "risk": risk, "qty": qty,
            "max_loss": round(qty * risk, 2), "value": round(qty * entry, 2)}
    if why:
        return None, why, nums
    order = {"symbol": symbol, "qty": str(qty), "side": "buy", "type": "market", "time_in_force": "day",
             "order_class": "bracket", "take_profit": {"limit_price": "%.2f" % target},
             "stop_loss": {"stop_price": "%.2f" % stop},
             "client_order_id": "ap-%s-%s" % (now.strftime("%Y%m%d"), symbol)}
    return order, [], nums


# ---------------------------------------------------------------- the bot

class Bot(object):
    def __init__(self, cfg, broker, folder="trader", dry_run=False, out=None, fetch=None, sleep=time.sleep):
        self.cfg = cfg
        self.broker = broker
        self.folder = os.path.join(folder, "autopilot")
        self.dry = dry_run
        self.out = out or sys.stdout
        self.fetch = fetch or self._fetch
        self.sleep = sleep
        self.state = None
        self.offset = -4.0

    # -- files
    def _path(self, name):
        return os.path.join(self.folder, name)

    def log(self, msg, now=None):
        line = "%s  %s" % ((now or datetime.datetime.now()).strftime("%Y-%m-%d %H:%M:%S"), msg)
        self.out.write(line + "\n")
        self.out.flush()
        os.makedirs(self.folder, exist_ok=True)
        with open(self._path("autopilot.log"), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def load_state(self, day):
        path = self._path("state-%s.json" % day.isoformat())
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                self.state = json.load(fh)
        else:
            self.state = {"date": day.isoformat(), "dry_run": self.dry, "tried": {}, "seen": {}, "trades": [],
                          "orders": 0, "flattened": False, "reported": False}
        return self.state

    def save_state(self):
        os.makedirs(self.folder, exist_ok=True)
        path = self._path("state-%s.json" % self.state["date"])
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=2)
            fh.write("\n")
        os.replace(path + ".tmp", path)

    # -- market
    def now(self):
        clk = self.broker.clock()
        now, self.offset = parse_ts(clk["timestamp"])
        return now, clk

    def _fetch(self, symbol, now):
        if self.cfg["autopilot"]["data"] == "alpaca":
            start = (datetime.datetime.combine(now.date(), OPEN) - datetime.timedelta(hours=self.offset))
            return parse_alpaca_bars(self.broker.bars_iex(symbol, start.strftime("%Y-%m-%dT%H:%M:%SZ")), self.offset)
        return fetch_yahoo(symbol, now.date())

    def book(self):
        """Today's picture for the risk rules, from Alpaca."""
        acct = self.broker.account()
        mine = set(t["symbol"] for t in self.state["trades"])
        invested, open_risk = 0.0, 0.0
        for p in self.broker.positions():
            if p["symbol"] in mine:
                qty = abs(float(p["qty"]))
                invested += qty * float(p["avg_entry_price"])
                stop = next((t["stop"] for t in self.state["trades"] if t["symbol"] == p["symbol"]), None)
                if stop is not None:
                    open_risk += max(0.0, (float(p["current_price"]) - stop) * qty)
        return {"cash": float(acct.get("cash") or 0), "pnl_today": float(acct.get("equity") or 0) - float(acct.get("last_equity") or 0),
                "invested": invested, "open_risk": open_risk,
                "entries": len(self.state["trades"]), "orders": self.state["orders"],
                "blocked": bool(acct.get("trading_blocked") or acct.get("account_blocked"))}

    # -- one look at the market
    def tick(self, now):
        st, cfg = self.state, self.cfg
        if now.time() >= hhmm(cfg["flat_by"], "flat_by"):
            if not st["flattened"]:
                self.flatten(now)
            return
        start = hhmm(cfg["no_entries_before"], "no_entries_before")
        until = min(hhmm(cfg["no_entries_after"], "no_entries_after"),
                    hhmm(cfg["autopilot"]["entries_until"], "entries_until"))
        if now.time() < start or now.time() > until:
            return
        last_bar = datetime.datetime.combine(now.date(), OPEN) + BAR * (int((now - datetime.datetime.combine(now.date(), OPEN)).total_seconds() // 300) - 1)
        pending = [s for s in cfg["autopilot"]["watchlist"] if s not in st["tried"] and st["seen"].get(s) != last_bar.isoformat()]
        hits = []
        for s in pending:
            try:
                sig = orb_signal(self.fetch(s, now), now)
            except AutopilotError as e:
                self.log("%s: no data (%s)" % (s, e), now)
                continue
            finally:
                self.sleep(PAUSE)
            if sig["status"] in ("forming",):
                continue
            if sig["status"] == "no-data":
                st["tried"][s] = "no data: " + sig["why"]
                continue
            st["seen"][s] = sig["last_bar"].isoformat()
            if sig["status"] == "triggered":
                if now - sig["at"] > STALE:
                    st["tried"][s] = "missed: broke out at %s, before the bot saw it" % sig["at"].strftime("%H:%M")
                else:
                    hits.append((s, sig))
        hits.sort(key=lambda h: -h[1]["over"])
        for s, sig in hits:
            self.try_trade(s, sig, now)
        self.save_state()

    def try_trade(self, symbol, sig, now):
        st = self.state
        book = self.book()
        if book["blocked"]:
            st["tried"][symbol] = "blocked: Alpaca says this account cannot trade"
            self.log("BLOCK %s: Alpaca says this account cannot trade" % symbol, now)
            return
        order, why, n = plan_trade(symbol, sig, self.cfg, book, now)
        if not order:
            st["tried"][symbol] = "blocked: " + "; ".join(why)
            self.log("BLOCK %s broke out at %.2f over %.2f: %s" % (symbol, n["entry"], sig["hi"], "; ".join(why)), now)
            return
        st["tried"][symbol] = "traded"
        st["orders"] += 1
        rec = {"symbol": symbol, "qty": n["qty"], "entry": n["entry"], "stop": n["stop"], "target": n["target"],
               "max_loss": n["max_loss"], "at": now.isoformat(), "client_order_id": order["client_order_id"]}
        if self.dry:
            rec["status"] = "dry-run"
        else:
            try:
                res = self.broker.place(order)
                rec.update(status=res.get("status", "sent"), order_id=res.get("id"))
            except AutopilotError as e:
                rec.update(status="rejected", note=str(e))
                self.log("REJECTED %s by Alpaca: %s" % (symbol, e), now)
                self.save_state()
                return
        st["trades"].append(rec)
        self.log("%s %s: buy %d at ~%.2f, stop %.2f, target %.2f, max loss %s%s"
                 % ("DRY-RUN" if self.dry else "ORDER", symbol, n["qty"], n["entry"], n["stop"], n["target"],
                    money(n["max_loss"]), "" if self.dry else " (Alpaca %s)" % rec["status"]), now)
        self.save_state()

    def flatten(self, now, why="flat by %s"):
        st = self.state
        mine = sorted(set(t["symbol"] for t in st["trades"]))
        if mine and not self.dry:
            for o in self.broker.orders("open", symbols=mine):
                for x in [o] + (o.get("legs") or []):
                    if x.get("status") in ("new", "accepted", "held", "partially_filled", "pending_new"):
                        try:
                            self.broker.cancel(x["id"])
                        except AutopilotError as e:
                            self.log("cancel %s %s: %s" % (x.get("symbol"), x["id"], e), now)
            self.sleep(2)
            held = set(p["symbol"] for p in self.broker.positions())
            for s in mine:
                if s in held:
                    try:
                        self.broker.close_position(s)
                        self.log("CLOSE %s at market (%s)" % (s, why % self.cfg["flat_by"]), now)
                    except AutopilotError as e:
                        self.log("CLOSE %s failed: %s. Close it in the Alpaca dashboard." % (s, e), now)
        st["flattened"] = True
        self.save_state()

    # -- after the close
    def report(self, now):
        st = self.state
        mine = sorted(set(t["symbol"] for t in st["trades"]))
        rows = []
        if self.dry:
            rows = [{"symbol": t["symbol"], "status": "dry run: buy %d at ~%.2f, stop %.2f, target %.2f"
                     % (t["qty"], t["entry"], t["stop"], t["target"])} for t in st["trades"]]
        elif mine:
            after = (datetime.datetime.combine(now.date(), datetime.time(0, 0)) - datetime.timedelta(hours=self.offset))
            flows = {}
            for o in self.broker.orders("all", after=after.strftime("%Y-%m-%dT%H:%M:%SZ"), symbols=mine):
                for x in [o] + (o.get("legs") or []):
                    q = float(x.get("filled_qty") or 0)
                    if q and x.get("filled_avg_price"):
                        f = flows.setdefault(x["symbol"], {"buy": 0.0, "sell": 0.0, "bq": 0.0, "sq": 0.0})
                        f[x["side"]] += q * float(x["filled_avg_price"])
                        f["bq" if x["side"] == "buy" else "sq"] += q
            for s in mine:
                f = flows.get(s)
                if not f or not f["bq"]:
                    rows.append({"symbol": s, "status": "not filled"})
                    continue
                rows.append({"symbol": s, "qty": f["bq"], "buy": round(f["buy"] / f["bq"], 2),
                             "sell": round(f["sell"] / f["sq"], 2) if f["sq"] else None,
                             "pnl": round(f["sell"] - f["buy"], 2) if f["sq"] >= f["bq"] else None,
                             "status": "closed" if f["sq"] >= f["bq"] else "STILL OPEN"})
        closed = [r for r in rows if r.get("pnl") is not None]
        total = round(sum(r["pnl"] for r in closed), 2)
        rep = {"date": st["date"], "dry_run": self.dry, "trades": rows, "pnl": total,
               "wins": sum(1 for r in closed if r["pnl"] > 0), "losses": sum(1 for r in closed if r["pnl"] <= 0),
               "skipped": {k: v for k, v in st["tried"].items() if v != "traded"}}
        os.makedirs(self.folder, exist_ok=True)
        with open(self._path("%s.json" % st["date"]), "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2)
            fh.write("\n")
        if not st["reported"]:
            new = not os.path.exists(self._path("journal.csv"))
            with open(self._path("journal.csv"), "a", encoding="utf-8", newline="") as fh:
                w = csv.writer(fh)
                if new:
                    w.writerow(["date", "mode", "trades", "wins", "losses", "pnl_usd", "symbols"])
                w.writerow([st["date"], "dry-run" if self.dry else ("paper" if self.cfg.get("paper", True) else "live"),
                            len(rows), rep["wins"], rep["losses"], "%.2f" % total, " ".join(mine)])
        st["reported"] = True
        self.save_state()
        lines = ["Day %s: %d trade(s), %d won, %d lost, P/L %s%s" % (st["date"], len(rows), rep["wins"], rep["losses"],
                                                                money(total), " (dry run)" if self.dry else "")]
        for r in rows:
            lines.append("  %-6s %s" % (r["symbol"], ("%g @ %.2f -> %s  %s" % (r["qty"], r["buy"], r["sell"], money(r["pnl"])))
                                         if r.get("pnl") is not None else r["status"]))
        self.log("\n".join(lines), now)
        return rep

    # -- the loop
    def run(self, forever=False):
        while True:
            now, clk = self.now()
            if self.state is None or self.state["date"] != now.date().isoformat():
                self.load_state(now.date())
                self.log("Autopilot on %s account, %d stocks, budget %s, max loss %s a trade, %s"
                         % ("PAPER" if getattr(self.broker, "paper", True) else "LIVE", len(self.cfg["autopilot"]["watchlist"]),
                            money(float(self.cfg["budget"])), money(float(self.cfg["max_loss_per_trade_usd"])),
                            "DRY RUN, no orders" if self.dry else "orders are sent without asking"), now)
            if not clk.get("is_open"):
                if now.time() >= datetime.time(16, 0) and self.state["trades"] and not self.state["reported"]:
                    if not self.state["flattened"]:
                        self.flatten(now)
                    self.report(now)
                if not forever and (now.time() >= datetime.time(16, 0) or self.state["reported"]):
                    self.log("Market closed. Done for today.", now)
                    return 0
                nxt, _ = parse_ts(clk["next_open"])
                wait = max(30, min(1800, (nxt - now).total_seconds()))
                self.log("Market closed. Next open %s New York. Waiting %d min." % (nxt.strftime("%a %H:%M"), wait // 60), now)
                self.sleep(wait)
                continue
            self.tick(now)
            if self.state["flattened"]:
                close, _ = parse_ts(clk["next_close"])
                self.sleep(max(30, (close - now).total_seconds() + 60))
                continue
            self.sleep(self._wait(now))

    def _wait(self, now):
        """Seconds to the next bar close, or a short re-poll while the last bar's data is still missing."""
        anchor = datetime.datetime.combine(now.date(), OPEN)
        k = int((now - anchor).total_seconds() // 300)
        next_close = anchor + BAR * (k + 1)
        since = (now - (anchor + BAR * k)).total_seconds()
        waiting = [s for s in self.cfg["autopilot"]["watchlist"] if s not in self.state["tried"]]
        if since < SETTLE and waiting and any(self.state["seen"].get(s) != (anchor + BAR * (k - 1)).isoformat() for s in waiting):
            return 30
        return max(5, (next_close - now).total_seconds() + GRACE)


# ---------------------------------------------------------------- CLI

def make_bot(a, dry_run=False):
    cfg = load_config(a.config)
    paper = bool(cfg.get("paper", True))
    if not paper and not a.live:
        raise AutopilotError('"paper": false is set, but a live account also needs --live on the command line')
    broker = Alpaca(os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY"), paper=paper)
    return Bot(cfg, broker, folder=a.dir, dry_run=dry_run)


def main(argv=None):
    p = argparse.ArgumentParser(description="Trades the opening-range breakout on its own (trader-autopilot).")
    p.add_argument("cmd", choices=["run", "status", "report", "flatten"])
    p.add_argument("--config", default=os.path.join("trader", "config.json"))
    p.add_argument("--dir", default="trader", help="where trader/autopilot/ lives (default trader)")
    p.add_argument("--dry-run", action="store_true", help="decide and log, send nothing")
    p.add_argument("--forever", action="store_true", help="keep going, session after session")
    p.add_argument("--live", action="store_true", help='needed together with "paper": false')
    a = p.parse_args(argv)
    try:
        bot = make_bot(a, dry_run=a.dry_run)
        if a.cmd == "run":
            return bot.run(forever=a.forever)
        now, clk = bot.now()
        bot.load_state(now.date())
        if a.cmd == "status":
            acct = bot.broker.account()
            print("%s account %s, equity %s, cash %s. Market %s (New York %s). Today: %d trade(s), %d stock(s) checked."
                  % ("PAPER" if bot.broker.paper else "LIVE", acct.get("status"), money(float(acct.get("equity") or 0)),
                     money(float(acct.get("cash") or 0)), "open" if clk.get("is_open") else "closed",
                     now.strftime("%Y-%m-%d %H:%M"), len(bot.state["trades"]), len(bot.state["tried"])))
            return 0
        if a.cmd == "flatten":
            bot.flatten(now, why="by hand")
            return 0
        bot.report(now)
        return 0
    except AutopilotError as e:
        sys.stderr.write("autopilot.py: %s\n" % e)
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("autopilot.py: stopped. Open bracket orders stay at Alpaca; run `autopilot.py flatten` to close.\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
