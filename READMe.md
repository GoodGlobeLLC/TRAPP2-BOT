# TRAPP2-BOT — Trading Bot Data & Training Repo

This repo is the **durable, cross-device source of truth for the Valuatio paper-trading bot**. It hosts the bot's entire trade history and everything it has learned, so the bot's memory survives a cleared browser cache, a new device, or a localStorage overflow.

## Why this repo exists

The bot used to keep its full trade log in the browser's `localStorage`. That log was set to grow forever ("permanent, never pruned"), and each trade carried a lot of detail (the full decision path, every signal's contribution, rationale). Over weeks of 6-trades-a-day that blew past the browser's ~5 MB storage limit. When `localStorage` is full, the browser silently refuses to save — so the bot's new trades weren't persisted, and a partially-written record could make the bot look like it had reset and lost everything.

Moving the history here fixes that permanently:
- The **repo** holds the complete journal (no size limit that matters).
- **localStorage** becomes a fast local cache that now self-trims (keeps open positions + the most recent closed trades) so it can never overflow and break again.

## How data flows (the write path)

The Valuatio app runs entirely in the browser and has **no GitHub write token**, so it can't push here directly. The flow mirrors the XTRAPP repo:

1. In the app, open the **Bot Bets** tab → click **⤓ Export Training**.
2. That downloads `bot_training_data.json`.
3. Commit that file to this repo at **`data/bot_training_data.json`**.
4. On any device, the app's **⤒ Import Repo** button pulls it back and restores the full history + learned weights.

The app reads it from:
```
https://raw.githubusercontent.com/GoodGlobeLLC/TRAPP2-BOT/main/data/bot_training_data.json
```

## What's in `data/bot_training_data.json`

A single JSON file (schema `valuatio-bot-training/v1`) written to be read by **both the bot and a human**. Top-level keys:

| Key | What it is |
|---|---|
| `generatedAt` | ISO timestamp of the export |
| `bankroll`, `startingBankroll`, `allTimeReturnPct` | Account state vs the fixed $100k basis |
| `counts` | total / open / closed / wins / losses |
| `performance` | winRate, totalPnl, avgWin, avgLoss, **profitFactor** |
| `learnedWeights` | the per-signal weights the bot has learned (what it trusts) |
| `signalScores` | **for each signal: win rate when it leans a direction** — which signals actually predict winners |
| `styleScores` | **per trade style: win rate + total P&L** — which styles actually make money |
| `takeaways` | plain-language lessons ("Most reliable signal: …", "X% stopped out — stops too tight") |
| `trades[]` | the full per-trade training corpus (see below) |
| `openPositions[]` | currently-open bets |
| `equityCurve[]` | daily book value for the chart |

### Each trade record (`trades[]`) — the "what & why" corpus

For every settled trade, so the bot can learn what to repeat and what to avoid:

- **What was done:** ticker, direction, `instrument` (shares / option / leveraged_etf), `style`, entry/exit price & date, hold days, P&L, return %, exit reason.
- **Why it was taken:** `topDrivers` (the signals with the biggest pull, with their lean), `rationale`, `conviction`, `confidence`, `regimeAtEntry`.
- **What worked / didn't:** `signalsThatHelped` and `signalsThatHurt` — which signals agreed with the realized outcome and which fought it.
- **What could have worked:** `couldHaveWorked` — an honest counterfactual for losers (e.g. "Stop was hit — entry was early or the stop too tight; a wider stop might have survived the shakeout").

## How the bot uses it to get better

- **`signalScores`** tells the bot which signals to trust more and which to down-weight — the data-driven version of the in-app **Retrain** button.
- **`styleScores`** shows which styles (momentum-long, short, leveraged-ETF, options, mean-reversion, value) are actually profitable in the current era, so the bot can lean into what works.
- **`trades[]` + `couldHaveWorked`** is the case-by-case record for spotting recurring mistakes (always stopping out early, fighting the regime, over-using leverage into volatility).

## Folder layout

```
TRAPP2-BOT/
  README.md
  data/
    bot_training_data.json      ← the journal (committed from the app's export)
  .github/workflows/
    validate.yml                ← sanity-checks the JSON on every commit
```

## Notes

- This repo is **for the bot only** — it is not a data source for the rest of the app (quotes, fundamentals, regime live in the other repos).
- The file is append-in-spirit: each export is a full snapshot, so committing a new one replaces the old. Git history preserves prior snapshots if you ever want to diff how the bot's learnings evolved.
- Nothing here contains API keys or secrets — it's trade metadata only — so a public repo is fine.
