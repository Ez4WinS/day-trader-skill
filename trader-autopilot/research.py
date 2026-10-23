"""Stock research for trader-autopilot: everything a buy decision should know
about a company beyond today's chart. Standard library only (Claude itself is
called through autopilot.Brain).

The dossier, all numbers computed here, nothing guessed:
  - quarters: the last 8 reported quarters (revenue, gross / operating / net
    margin, net income, diluted EPS, growth against the same quarter a year
    earlier). A fourth quarter is the 10-K year minus its three 10-Qs.
  - years: the last 8 fiscal years from the 10-Ks (revenue, net income, EPS,
    operating cash flow, capex, free cash flow).
  - balance: the latest cash, long-term debt, equity, assets, liabilities and
    shares outstanding.
  - price: 10 years of monthly closes from Yahoo: the return of every calendar
    year with its high and low, the 10-year high and low with their months,
    the distance from the high, the 8-year yearly growth rate (CAGR), the
    52-week range and the 50/200-day averages (trend).
  - earnings: the dates of the last quarterly-results 8-Ks (item 2.02) and the
    next date they suggest (last one + the usual gap), marked as an estimate.

Sources: data.sec.gov (companyfacts and submissions JSON, a User-Agent with a
name and email is required) and Yahoo's chart JSON.
"""

import datetime as _dt

CONCEPTS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "RevenuesNetOfInterestExpense", "RevenueFromContractWithCustomerIncludingAssessedTax",
                "SalesRevenueNet", "SalesRevenueGoodsNet", "SalesRevenueServicesNet"],
    "cost_of_revenue": ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "eps_diluted": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities",
                            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
}
BALANCE = {
    "cash": ["CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "long_term_debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "equity": ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "assets": ["Assets"],
    "liabilities": ["Liabilities"],
}
FORMS = ("10-Q", "10-K", "20-F", "40-F")


def _date(s):
    return _dt.date(int(s[:4]), int(s[5:7]), int(s[8:10]))


def facts_for(companyfacts, concept, unit_hint=None, ns="us-gaap"):
    node = (companyfacts.get("facts") or {}).get(ns, {}).get(concept)
    if not node:
        return []
    units = node.get("units", {})
    unit = unit_hint if unit_hint in units else next(iter(units), None)
    return units.get(unit, []) if unit else []


def periods(facts):
    """{(start, end): fact} for duration facts from 10-Q/10-K; the latest filing wins (restatements)."""
    latest, first = {}, {}
    for f in facts:
        if "start" not in f or f.get("form", "").split("/")[0] not in FORMS:
            continue
        key = (f["start"], f["end"])
        stamp = (f.get("filed", ""), f.get("accn", ""))
        if key not in latest or stamp > (latest[key].get("filed", ""), latest[key].get("accn", "")):
            latest[key] = f
        if key not in first or stamp < (first[key].get("filed", ""), first[key].get("accn", "")):
            first[key] = f
    out = {}
    for key, f in latest.items():
        g = dict(f)
        g["fy"], g["fp"] = first[key].get("fy"), first[key].get("fp")
        out[key] = g
    return out


def split_periods(facts, derive_q4=True):
    """(quarters, years): {end: {"value", "start", "end", "derived", "fy", "fp", "filed"}}."""
    q, y = {}, {}
    for (start, end), f in periods(facts).items():
        days = (_date(end) - _date(start)).days
        row = {"value": f["val"], "start": start, "end": end, "derived": False,
               "fy": f.get("fy"), "fp": f.get("fp"), "filed": f.get("filed")}
        if 80 <= days <= 100:
            q[end] = row
        elif 350 <= days <= 380:
            y[end] = row
    if derive_q4:
        for end, f in y.items():
            if end in q:
                continue
            inside = sorted((v for v in q.values() if v["start"] >= f["start"] and v["end"] < end),
                            key=lambda v: v["end"])
            if len(inside) != 3:
                continue
            start = (_date(inside[-1]["end"]) + _dt.timedelta(days=1)).isoformat()
            q[end] = {"value": f["value"] - sum(v["value"] for v in inside), "start": start, "end": end,
                      "derived": True, "fy": f.get("fy"), "fp": "Q4", "filed": f.get("filed")}
    return q, y


def series(companyfacts, key):
    """(quarters, years) for a metric, trying its concepts in order and filling gaps."""
    unit = "USD/shares" if key == "eps_diluted" else "USD"
    qs, ys = {}, {}
    for concept in CONCEPTS[key]:
        q, y = split_periods(facts_for(companyfacts, concept, unit), derive_q4=key not in ("operating_cash_flow", "capex"))
        for src, dst in ((q, qs), (y, ys)):
            for end, v in src.items():
                dst.setdefault(end, v)
    return qs, ys


def _yoy(series_, end, slack=10):
    target = _date(end) - _dt.timedelta(days=365)
    best = None
    for e in series_:
        gap = abs((_date(e) - target).days)
        if gap <= slack and (best is None or gap < best[0]):
            best = (gap, e)
    return series_[best[1]]["value"] if best else None


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return round(100.0 * (a - b) / abs(b), 1)


def margin(part, whole):
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 1)


def millions(v):
    return None if v is None else round(v / 1e6, 1)


def fundamentals(companyfacts, quarters=8, years=8):
    """Quarters, fiscal years and the latest balance sheet, in millions of USD (EPS in USD)."""
    q, y = {}, {}
    for key in CONCEPTS:
        q[key], y[key] = series(companyfacts, key)
    ends = sorted(set(q["revenue"]) | set(q["net_income"]), reverse=True)[:quarters]
    qrows = []
    for end in ends:
        r = q["revenue"].get(end, {}).get("value")
        gp = q["gross_profit"].get(end, {}).get("value")
        if gp is None and r is not None and end in q["cost_of_revenue"]:
            gp = r - q["cost_of_revenue"][end]["value"]
        oi = q["operating_income"].get(end, {}).get("value")
        ni = q["net_income"].get(end, {}).get("value")
        eps = q["eps_diluted"].get(end, {}).get("value")
        base = q["revenue"].get(end) or q["net_income"].get(end) or {}
        qrows.append({
            "quarter_end": end, "fiscal": ("%s %s" % (base.get("fp") or "", base.get("fy") or "")).strip(),
            "revenue_m": millions(r), "revenue_yoy_pct": pct(r, _yoy(q["revenue"], end)),
            "gross_margin_pct": margin(gp, r), "operating_margin_pct": margin(oi, r), "net_margin_pct": margin(ni, r),
            "net_income_m": millions(ni), "net_income_yoy_pct": pct(ni, _yoy(q["net_income"], end)),
            "eps_diluted": round(eps, 2) if eps is not None else None,
            "eps_yoy_pct": pct(eps, _yoy(q["eps_diluted"], end)),
            "q4_derived_from_10k": bool(base.get("derived")),
        })
    yends = sorted(set(y["revenue"]) | set(y["net_income"]), reverse=True)[:years]
    yrows = []
    for end in yends:
        r = y["revenue"].get(end, {}).get("value")
        ni = y["net_income"].get(end, {}).get("value")
        ocf = y["operating_cash_flow"].get(end, {}).get("value")
        capex = y["capex"].get(end, {}).get("value")
        base = y["revenue"].get(end) or y["net_income"].get(end) or {}
        yrows.append({
            "fiscal_year_end": end, "fiscal_year": base.get("fy"),
            "revenue_m": millions(r), "revenue_growth_pct": pct(r, _yoy(y["revenue"], end, slack=20)),
            "net_income_m": millions(ni), "net_margin_pct": margin(ni, r),
            "eps_diluted": round(y["eps_diluted"][end]["value"], 2) if end in y["eps_diluted"] else None,
            "operating_cash_flow_m": millions(ocf), "capex_m": millions(capex),
            "free_cash_flow_m": millions(ocf - capex) if ocf is not None and capex is not None else None,
        })
    bal = {}
    for key, concepts in BALANCE.items():
        best = None
        for c in concepts:
            for f in facts_for(companyfacts, c, "USD"):
                if "start" in f or f.get("form", "").split("/")[0] not in FORMS:
                    continue
                if best is None or (f["end"], f.get("filed", "")) > (best["end"], best.get("filed", "")):
                    best = f
            if best:
                break
        bal[key + "_m"] = millions(best["val"]) if best else None
        if best:
            bal["as_of"] = max(bal.get("as_of") or "", best["end"])
    shares = sorted(facts_for(companyfacts, "EntityCommonStockSharesOutstanding", "shares", ns="dei"),
                    key=lambda f: (f.get("end", ""), f.get("filed", "")))
    bal["shares_outstanding_m"] = millions(shares[-1]["val"]) if shares else None
    ttm = {}
    for key in ("revenue", "net_income", "eps_diluted"):
        last4 = sorted(q[key])[-4:]
        if len(last4) == 4 and (_date(last4[-1]) - _date(last4[0])).days < 300:
            v = sum(q[key][e]["value"] for e in last4)
            ttm[key + ("" if key == "eps_diluted" else "_m")] = round(v, 2) if key == "eps_diluted" else millions(v)
    return {"company": companyfacts.get("entityName"), "quarters": qrows, "years": yrows,
            "balance": bal, "ttm": ttm}


def price_history(monthly_js, daily_js=None):
    """10 years of monthly bars (+ 1 year of daily bars) -> the long-term picture in numbers."""
    try:
        r = monthly_js["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        return {}
    q = (r.get("indicators", {}).get("quote") or [{}])[0]
    rows = []
    for i, ts in enumerate(r.get("timestamp") or []):
        c, h, l = q.get("close", [None])[i], q.get("high", [None])[i], q.get("low", [None])[i]
        if None in (c, h, l):
            continue
        d = _dt.datetime(1970, 1, 1) + _dt.timedelta(seconds=ts)
        rows.append({"month": d.strftime("%Y-%m"), "year": d.year, "c": c, "h": h, "l": l})
    if not rows:
        return {}
    last = r.get("meta", {}).get("regularMarketPrice") or rows[-1]["c"]
    years = {}
    for x in rows:
        years.setdefault(x["year"], []).append(x)
    by_year, prev_close = [], None
    for yr in sorted(years):
        xs = years[yr]
        hi = max(xs, key=lambda x: x["h"])
        lo = min(xs, key=lambda x: x["l"])
        close = xs[-1]["c"]
        by_year.append({"year": yr, "return_pct": pct(close, prev_close) if prev_close else None,
                        "high": round(hi["h"], 2), "high_month": hi["month"],
                        "low": round(lo["l"], 2), "low_month": lo["month"], "close": round(close, 2),
                        "partial_year": len(xs) < 12})
        prev_close = close
    top = max(rows, key=lambda x: x["h"])
    bottom = min(rows, key=lambda x: x["l"])
    out = {"last_price": round(last, 2), "by_year": by_year,
           "range_high": round(top["h"], 2), "range_high_month": top["month"],
           "range_low": round(bottom["l"], 2), "range_low_month": bottom["month"],
           "from_high_pct": pct(last, top["h"]), "first_month": rows[0]["month"]}
    same_month = "%d%s" % (rows[-1]["year"] - 8, rows[-1]["month"][4:])   # exactly 8 years back
    eight = [x for x in rows if x["month"] == same_month]
    if eight and eight[0]["c"] > 0:
        out["cagr_8y_pct"] = round(100 * ((last / eight[0]["c"]) ** (1 / 8.0) - 1), 1)
    if daily_js:
        try:
            dq = (daily_js["chart"]["result"][0].get("indicators", {}).get("quote") or [{}])[0]
            closes = [c for c in dq.get("close") or [] if c is not None]
            highs = [h for h in dq.get("high") or [] if h is not None]
            lows = [x for x in dq.get("low") or [] if x is not None]
        except (KeyError, IndexError, TypeError):
            closes = []
        if closes:
            out["high_52w"] = round(max(highs or closes), 2)
            out["low_52w"] = round(min(lows or closes), 2)
            if len(closes) >= 50:
                out["avg_50d"] = round(sum(closes[-50:]) / 50.0, 2)
            if len(closes) >= 200:
                out["avg_200d"] = round(sum(closes[-200:]) / 200.0, 2)
            if out.get("avg_50d") and out.get("avg_200d"):
                out["trend"] = ("up" if last > out["avg_50d"] > out["avg_200d"] else
                                "down" if last < out["avg_50d"] < out["avg_200d"] else "mixed")
    return out


def earnings_dates(submissions, today):
    """Past quarterly-results 8-Ks (item 2.02) and an estimate of the next one."""
    try:
        r = submissions["filings"]["recent"]
    except (KeyError, TypeError):
        return {}
    past = sorted({r["filingDate"][i] for i, f in enumerate(r.get("form") or [])
                   if f in ("8-K", "8-K/A") and "2.02" in str((r.get("items") or [""] * (i + 1))[i])}, reverse=True)[:6]
    out = {"past_results_8k": past}
    if len(past) >= 2:
        gaps = [(_date(a) - _date(b)).days for a, b in zip(past, past[1:])]
        usual = sorted(gaps)[len(gaps) // 2]
        nxt = _date(past[0]) + _dt.timedelta(days=usual)
        while nxt < today:
            nxt += _dt.timedelta(days=usual)
        out["next_results_estimate"] = nxt.isoformat()
        out["days_to_next_estimate"] = (nxt - today).days
        out["note"] = "estimate from the usual gap between past reports, not a confirmed date"
    forms = [(r["filingDate"][i], f) for i, f in enumerate(r.get("form") or []) if f in ("10-K", "10-Q")]
    if forms:
        out["latest_report"] = {"form": forms[0][1], "filed": forms[0][0]}
    return out
