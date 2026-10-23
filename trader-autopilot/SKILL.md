---
name: trader-autopilot
description: >-
  Autopilot. Trades the opening-range breakout on its own on an Alpaca
  account, paper by default, with no yes per order: your rules in
  trader/config.json are the permission. Watches 50 big US stocks (or your
  list), builds each one's 15-minute opening range, buys the first 5-minute
  close above it as ONE bracket order (stop at the range midpoint, target at
  1R), sizes every trade to your budget and max loss, holds the daily loss
  limit and trades-per-day cap, closes everything by 15:55 New York and writes
  a daily report and a journal. Runs on your computer with your Alpaca keys in
  environment variables. Use when the user says "trade on your own", "run the
  bot", "autopilot", "trade automatically", "no confirmation", "let it trade
  by itself", "paper trade for me", "start the bot", "stop the bot", "how did
  the bot do", or "close everything".
argument-hint: "[run | status | report | flatten]"
---

# trader-autopilot

The Autopilot is the hands-off version of the pack's one-trade chain
(scanner, entry, exit, budget, risk, broker). It runs the same opening-range
breakout rule and the same risk limits as trader-risk, but it does not stop
to ask for a yes: once it is started, every order that passes the rules is
sent to Alpaca at once. The user's yes is given ONCE, by starting it, and
the limits in `trader/config.json` decide everything after that.

Because nobody confirms the orders, it is paper first: it uses Alpaca's
paper account unless the user sets `"paper": false` in the config AND starts
it with `--live` themselves.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/autopilot.py" status            # account, market clock, today so far
python3 "${CLAUDE_SKILL_DIR}/autopilot.py" run --dry-run     # one session: decide and log, send nothing
python3 "${CLAUDE_SKILL_DIR}/autopilot.py" run               # one session, orders go to Alpaca
python3 "${CLAUDE_SKILL_DIR}/autopilot.py" run --forever     # every session until Ctrl+C
python3 "${CLAUDE_SKILL_DIR}/autopilot.py" report            # today's fills and P/L
python3 "${CLAUDE_SKILL_DIR}/autopilot.py" flatten           # cancel the bot's orders, close its positions
```

## The rule, bar by bar (New York time)

- **09:30 to 09:45**: the opening range is the high and low of the 9:30, 9:35
  and 9:40 five-minute bars.
- **09:45 to `entries_until` (11:30)**: after each closed bar, a stock whose
  bar CLOSES above its range high triggers. A wick above the high is not a
  trigger. Only the first close counts, and a breakout the bot did not see
  live (more than 8 minutes old) is skipped, never chased.
- Several triggers in one bar: the strongest breakout (furthest over its
  high, in percent) goes first.
- **The order**: buy at market, `time_in_force` day, bracket: stop-loss at
  the range midpoint (rounded down to the cent), take-profit at `target_r`
  times the risk. One `client_order_id` per stock per day, so a restart never
  doubles an order.
- **`flat_by` (15:55)**: cancels the bot's open stop and target orders and
  closes its positions at market.
- **After 16:00**: `trader/autopilot/<date>.json` (every trade, every skipped
  stock and why) and one line in `trader/autopilot/journal.csv`.

## The limits (trader/config.json)

The same keys trader-budget and trader-risk read. A trigger that breaks any
of them is BLOCKED and logged with the reason; nothing is sent.

| key | default | what it does |
| --- | --- | --- |
| `budget` | 1000 | the most the bot ever uses, even on a $100,000 paper account |
| `max_loss_per_trade_usd` | 10 | shares = this / (entry - stop), rounded down |
| `max_daily_loss_usd` | 30 | today's loss + risk still open + this trade must fit |
| `max_trades_per_day` | 3 | entries per day |
| `max_orders_per_day` | 10 | orders per day |
| `min_reward_risk` | 1.0 | `target_r` must reach it |
| `max_position_pct` | 40 | of the budget, per stock |
| `max_order_usd` | 900 | per order |
| `cash_buffer_pct` | 5 | of the budget, never spent |
| `no_entries_before` / `no_entries_after` | 09:45 / 15:30 | the entry window |
| `flat_by` | 15:55 | everything closed by then |
| `blocked` | [] | tickers it never buys |
| `paper` | true | false only together with `--live` |

The `"autopilot"` block adds `watchlist` (default: the 50 biggest and
best-known US stocks), `entries_until` ("11:30"), `target_r` (1.0),
`min_stop_pct` (0.1, a tighter stop is refused) and `data` ("yahoo", or
"alpaca" for Alpaca's free IEX bars).

## Setup

1. An Alpaca account with **paper trading** API keys (dashboard, Paper, API
   Keys). Keep the paper account's balance as it is; the `budget` key, not
   the account balance, limits the bot.
2. The keys go into environment variables on the user's own computer, typed
   into their terminal, never into a chat:
   - macOS / Linux: `export ALPACA_API_KEY=...` and `export ALPACA_SECRET_KEY=...`
   - Windows PowerShell: `$env:ALPACA_API_KEY="..."` and `$env:ALPACA_SECRET_KEY="..."`
   If the user pastes a key into the chat anyway, tell them to delete the
   message and make new keys in the Alpaca dashboard.
3. Copy `trader-budget/config.example.json` to `trader/config.json` and set
   the limits above.
4. First a `run --dry-run` for a session. Read `trader/autopilot/autopilot.log`
   with the user. Then `run`.
5. The computer has to stay on and online from 15:30 to 22:00 German time
   (09:30 to 16:00 New York). If it sleeps, the bracket orders already at
   Alpaca still protect each open trade, but nothing closes at 15:55.
   `flatten` closes it by hand.

## Steps when the user asks

- "start the bot": check `status` first (paper? market open?), say what the
  limits are in plain words, then start `run` (or `run --dry-run` the first
  time) in the background and show the first log lines.
- "how did it do": `report`, then read `trader/autopilot/journal.csv` across
  days: trades, wins, losses, total P/L. A handful of days says little.
- "stop": stop the process (Ctrl+C) and ask whether to `flatten` too.
  Stopping alone leaves the open bracket orders at Alpaca.

## What it never does

- Never trades live unless the config says `"paper": false` and the user
  started it with `--live` themselves. Claude never adds `--live`.
- Never sells short, never uses margin, options or crypto.
- Never reads, stores or prints API keys.
- Never chases a breakout it did not see live, and never buys the same stock
  twice in one day.
- Never holds overnight: everything it opened is closed by `flat_by`.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a
  recommendation.
- Without a yes per order, the limits are the only brake. Start small, start
  on paper, and read the log.
- A stop is an order, not a promise: in a fast market or across a gap it can
  fill well below its price, so a loss can be bigger than the max loss shown.
- Paper fills are simulated and usually kinder than real ones. A paper
  result is the optimistic version.
- Yahoo's chart data is free and unofficial; it can lag or rate-limit. A
  stock with no data is skipped, never guessed.
