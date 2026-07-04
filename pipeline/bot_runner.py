#!/usr/bin/env python3
"""
bot_runner.py — autonomous daily trading for the Valuatio bot.

Runs as a GitHub Action so the bot makes trades WITHOUT the app being open. Each
run it:

  1. READS state         — bot_training_data.json from this repo (bankroll, open
                           positions, learned weights, equity curve, full history).
  2. READS the market    — master.json (universe + latest prices) from TRAPP2 /
                           TRAPP2-2, and daily history from data/history/<T>.json.
  3. MANAGES positions   — marks open positions to the latest price; closes any
                           that hit their stop, target, or horizon, freezing
                           realized P&L from shares (exactly like the app's z35).
  4. SCORES + TRADES     — scores candidates with a faithful subset of the app's
                           signal engine (trend, momentum, mean-reversion, grade),
                           weighted by the bot's LEARNED weights, then opens the
                           best few within risk + cash limits. Never re-buys a
                           name already held.
  5. WRITES state        — recomputes performance + equity point, writes
                           bot_training_data.json back (the workflow commits it).
  6. Supabase            — the workflow's existing sync step mirrors trades after.

HONEST SCOPE
------------
The scoring is a faithful subset of the app's brain that now includes: trend
(SMA20/60), momentum (~3-month), mean-reversion (RSI), PEER-SECTOR GRADE (sector-
relative grade percentile blended with sector-ETF momentum), and REGIME (SPY
trend + realized vol → risk-on/off/choppy, which both tilts the signal weights
and applies a sector/beta regime-grade signal) — all weighted by the bot's
LEARNED weights. Still not ported: options IV, explicit cross-asset, the macro-
tab "quad", and Fed expectations. The runner remains long-only by default (no
shorts/options/leverage) unless turned on. With peer-grade + regime in, the
runner's picks now converge much closer to the app's; it stays deliberately
conservative on sizing and trade count. Everything is recorded in the same schema
the app reads.

SAFETY KNOBS (env vars, all optional)
  RUNNER_MAX_NEW_TRADES   default 2    new positions opened per run
  RUNNER_MAX_POSITIONS    default 15   total open positions allowed
  RUNNER_POSITION_PCT     default 5    % of bankroll per new position
  RUNNER_CASH_RESERVE_PCT default 20   % of bankroll kept as cash, never deployed
  RUNNER_MIN_SCORE        default 0.35 minimum signed score to open a long
  RUNNER_DRY_RUN          default off  set to "1" to compute + log but NOT write

Stdlib only (urllib + json + math). No pip installs.
"""
import json
import math
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Canonical state lives in data/. The runner previously read the ROOT copy, which
# was stale (bankroll reset to $100k, only a couple of trades) — so it ignored the
# real book. Read data/ first; migrate from the legacy root file if data/ is absent.
STATE_FILE = ROOT / "data" / "bot_training_data.json"
_LEGACY_STATE = ROOT / "bot_training_data.json"

RAW = "https://raw.githubusercontent.com/GoodGlobeLLC"
UNIVERSE_SOURCES = [
    f"{RAW}/TRAPP2/main/data/master.json",
    f"{RAW}/TRAPP2-2/main/data/master.json",
]
HISTORY_BASE = {
    "TRAPP2": f"{RAW}/TRAPP2/main/data/history",
    "TRAPP2-2": f"{RAW}/TRAPP2-2/main/data/history",
}
GRADES_URL = f"{RAW}/TRAPP2-ANALYTICS/main/data/research_grades.json"
# Sector ETFs live in TRAPP2-1's history; used for sector-momentum + regime.
ETF_HISTORY_BASE = f"{RAW}/TRAPP2-1/main/data/history"
SPY_HISTORY_URL = f"{RAW}/TRAPP2-1/main/data/history/SPY.json"
# The REAL FRED-derived quad, written by TRAPP2-1's macro-quad workflow. If
# present the runner uses it; otherwise it falls back to the market-implied proxy.
MACRO_QUAD_URL = f"{RAW}/TRAPP2-1/main/data/macro/quad.json"
# Option chains live in TRAPP2 (data/options/<TICKER>.json + manifest.json). The
# runner reads them for the same forward-looking IV/skew/term signals the app
# uses, plus a market-wide options read (index IV → vol regime + market tilt).
OPTIONS_BASE = f"{RAW}/TRAPP2/main/data/options"
OPTIONS_MANIFEST_URL = f"{OPTIONS_BASE}/manifest.json"
# Index proxies whose ATM IV defines the market's expected move (VIX-like).
MKT_OPT_INDEXES = ["SPY", "QQQ", "IWM"]

# Cross-asset baskets (mirrors extractCrossAssetSignals). The runner reads the
# latest daily return of each from TRAPP2-1 history.
XASSET_BASKETS = {
    "dollar":     ["UUP", "DX=F"],
    "safeHaven":  ["GLD", "TLT", "^VIX"],     # gold + bonds + VIX (flight to safety)
    "industrial": ["CPER", "XLB", "PICK"],    # copper / materials / mining
    "energy":     ["USO", "XLE", "AMLP"],
    "indices":    ["^GSPC", "^IXIC", "^FTSE", "^N225", "^GDAXI", "^HSI"],
}

# SECTOR_ETFS quad favoring (mirrors the app): which quad (1-4) each sector ETF
# favors / is hurt by. Quads: 1=Growth↑Infl↓ (Goldilocks), 2=Growth↑Infl↑
# (Reflation), 3=Growth↓Infl↑ (Stagflation), 4=Growth↓Infl↓ (Deflation).
SECTOR_ETF_QUAD = {
    "XLK":  {"favors": [1, 2, 3], "hurts": [4]},
    "XLY":  {"favors": [1, 2],    "hurts": [3, 4]},
    "XLI":  {"favors": [1, 2],    "hurts": [3]},
    "XLF":  {"favors": [2],       "hurts": [3, 4]},
    "XLE":  {"favors": [2, 3],    "hurts": [1]},
    "XLP":  {"favors": [4],       "hurts": [1]},
    "XLV":  {"favors": [4],       "hurts": []},
    "XLU":  {"favors": [3, 4],    "hurts": [1]},
    "XLRE": {"favors": [3, 4],    "hurts": []},
    "XLB":  {"favors": [2, 3],    "hurts": [4]},
    "XLC":  {"favors": [1, 2],    "hurts": [4]},
}

# Equity sector name → sector ETF (mirrors the app's SECTOR_TO_ETF).
SECTOR_TO_ETF = {
    "Technology": "XLK",
    "Consumer Cyclical": "XLY", "Consumer Discretionary": "XLY",
    "Industrials": "XLI", "Industrial": "XLI",
    "Financial Services": "XLF", "Financials": "XLF", "Financial": "XLF",
    "Energy": "XLE",
    "Consumer Defensive": "XLP", "Consumer Staples": "XLP",
    "Healthcare": "XLV", "Health Care": "XLV",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Basic Materials": "XLB", "Materials": "XLB",
    "Communication Services": "XLC",
}

STARTING_BANKROLL = 100000.0

# ---- safety knobs ----
def _envf(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)

MAX_NEW_TRADES = int(_envf("RUNNER_MAX_NEW_TRADES", 2))
MAX_POSITIONS = int(_envf("RUNNER_MAX_POSITIONS", 25))

# ---- confidence, triggers, and horizon-aware exits -------------------------
# The runner classifies every position as SWING (momentum, tight exits) or CORE
# (long-term / value-like, ride it). Which one is decided by CONFIDENCE (signal
# agreement + strength + breadth) and whether the blend leans on slow, fundamental
# signals. Exit rules then differ by class — a long-term winner is TRIMMED, not
# force-closed, and is never dumped on a fixed calendar.
CORE_CONF      = _envf("RUNNER_CORE_CONF", 0.62)        # min confidence to hold as long-term/core
TRIGGER_LEVEL  = _envf("RUNNER_TRIGGER_LEVEL", 0.33)    # an aligned signal this strong "fires"
MIN_TRIGGERS   = int(_envf("RUNNER_MIN_TRIGGERS", 1))   # concrete triggers needed to open

SWING_STOP     = _envf("RUNNER_SWING_STOP", 8) / 100.0      # -8%  swing stop
SWING_TARGET   = _envf("RUNNER_SWING_TARGET", 16) / 100.0   # +16% swing target
SWING_HORIZON  = int(_envf("RUNNER_SWING_HORIZON", 21))     # base swing horizon (days), conf-scaled

CORE_HARD_STOP = _envf("RUNNER_CORE_STOP", 25) / 100.0      # -25% disaster stop for core
CORE_TRAIL     = _envf("RUNNER_CORE_TRAIL", 18) / 100.0     # 18% trailing from peak (once in profit)
CORE_EXIT_SCORE= _envf("RUNNER_CORE_EXIT_SCORE", 0.12)      # thesis-break: signal flips this far against
CORE_TRIM_TIERS= [0.25, 0.50, 1.00]                         # +25/50/100% → trim tiers (long only)
CORE_TRIM_FRAC = _envf("RUNNER_CORE_TRIM_FRAC", 25) / 100.0 # trim this fraction of shares per tier

LONG_TERM_SIGNALS = ("fundamentals", "researchGrade", "health", "peerGrade", "regimeGrade", "fed", "crossAsset")
SWING_SIGNALS     = ("trend", "momentum", "meanReversion", "optionsIV", "optionsMarket")
POSITION_PCT = _envf("RUNNER_POSITION_PCT", 5) / 100.0
CASH_RESERVE_PCT = _envf("RUNNER_CASH_RESERVE_PCT", 20) / 100.0
MIN_SCORE = _envf("RUNNER_MIN_SCORE", 0.35)
DRY_RUN = os.environ.get("RUNNER_DRY_RUN", "") in ("1", "true", "yes")
# Optional: allow the runner to SHORT on strongly-negative scores (off by default;
# the runner is long-only unless you flip this). Options/leverage remain off.
ALLOW_SHORTS = os.environ.get("RUNNER_ALLOW_SHORTS", "") in ("1", "true", "yes")
SHORT_SCORE = -_envf("RUNNER_MIN_SCORE", 0.35) - 0.10   # a bit more conviction to short
FEE_BPS = 0.0005  # ~5bps/side, matches the app's estimate


def log(*a):
    print("[bot_runner]", *a, flush=True)


def fetch_json(url, timeout=45):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "valuatio-bot-runner"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code != 404:
            log(f"  ! HTTP {e.code} for {url}")
        return None
    except Exception as e:
        log(f"  ! fetch failed {url}: {e}")
        return None


# ----------------------------- state -----------------------------------------
def load_state():
    for path in (STATE_FILE, _LEGACY_STATE):
        if path.exists():
            try:
                d = json.loads(path.read_text())
                if isinstance(d, dict):
                    if path is _LEGACY_STATE:
                        log(f"  migrated bot state from legacy {path.name} "
                            f"(bankroll ${d.get('bankroll', 0):,.0f}, {len(d.get('trades') or [])} trades)")
                    return d
            except Exception as e:
                log(f"  ! state parse failed ({path.name}): {e}")
    # fresh
    return {
        "schema": "valuatio-bot-training/v1",
        "bankroll": STARTING_BANKROLL,
        "startingBankroll": STARTING_BANKROLL,
        "trades": [],
        "openPositions": [],
        "equityCurve": [],
        "learnedWeights": {},
        "benchmarkStart": None,
    }


def trades_list(state):
    """The full per-trade records live in 'trades'; that's our working list."""
    t = state.get("trades")
    return t if isinstance(t, list) else []


# ----------------------------- market data ------------------------------------
def load_universe():
    """ticker -> {price, sector, name, grade, repo, ...} from master.json files."""
    rows = {}
    for url in UNIVERSE_SOURCES:
        data = fetch_json(url)
        if not isinstance(data, list):
            continue
        repo = "TRAPP2" if "TRAPP2/main" in url else "TRAPP2-2"
        for r in data:
            t = (r.get("ticker") or r.get("symbol") or "").upper()
            if not t or t in rows:
                continue
            price = r.get("price")
            if price is None:
                price = r.get("close")
            try:
                price = float(price)
            except (TypeError, ValueError):
                continue
            if not price or price <= 0:
                continue
            rows[t] = {
                "ticker": t, "price": price,
                "sector": r.get("sector") or "Unknown",
                "name": r.get("name") or t,
                "repo": repo,
                "changepct": _f(r.get("changepct")),
                "beta": _f(r.get("beta")),
                # Fundamentals carried through for the ported reasoning-chain /
                # health engine (same fields the app's stockbook rows expose).
                "marketcap": _f(r.get("marketcap")),
                "eps": _f(r.get("eps")),
                "profitMargin": _f(r.get("profitMargin")),
                "operatingMargin": _f(r.get("operatingMargin")),
                "freeCashFlow": _f(r.get("freeCashFlow")),
                "netIncome": _f(r.get("netIncome")),
                "revenue": _f(r.get("revenue")),
                "revenueGrowth": _f(r.get("revenueGrowth")),
                "earningsGrowth": _f(r.get("earningsGrowth")),
                "returnOnEquity": _f(r.get("returnOnEquity")),
                "returnOnAssets": _f(r.get("returnOnAssets")),
                "debtToEquity": _f(r.get("debtToEquity")),
                "currentRatio": _f(r.get("currentRatio")),
                "cash": _f(r.get("cash")),
                "totalDebt": _f(r.get("totalDebt")),
                "totalEquity": _f(r.get("totalEquity")),
                "pe": _f(r.get("pe")),
                "priceToBook": _f(r.get("priceToBook")),
                "dividend_yield": _f(r.get("dividend_yield")),
                "payoutRatio": _f(r.get("payoutRatio")),
            }
    log(f"universe: {len(rows)} priced tickers")
    return rows


def load_grades():
    """ticker -> gradeScore (0-100) from research_grades.json. {} if unavailable."""
    data = fetch_json(GRADES_URL)
    out = {}
    if isinstance(data, dict):
        bt = data.get("byTicker") or {}
        for t, g in bt.items():
            gs = g.get("gradeScore")
            if isinstance(gs, (int, float)):
                out[t.upper()] = {"gradeScore": float(gs), "sector": g.get("sector")}
    log(f"grades: {len(out)} tickers")
    return out


# Cache sector-ETF 3-month momentum so we fetch each ETF once per run.
_ETF_MOM_CACHE = {}

def sector_etf_momentum(sector):
    """3-month return of the sector's ETF, mapped to a 0-100 score (50 = flat).
    Mirrors the app: −15%..+15% over ~63 trading days → 0..100."""
    etf = SECTOR_TO_ETF.get(sector)
    if not etf:
        return None
    if etf in _ETF_MOM_CACHE:
        return _ETF_MOM_CACHE[etf]
    data = fetch_json(f"{ETF_HISTORY_BASE}/{etf}.json", timeout=30)
    score = None
    if isinstance(data, list) and len(data) >= 60:
        closes = [(_f(b.get("close")) or _f(b.get("price"))) for b in data]
        closes = [c for c in closes if c and c > 0]
        if len(closes) >= 60:
            last = closes[-1]
            ago = closes[max(0, len(closes) - 63)]
            ret = (last - ago) / ago if ago > 0 else 0
            score = max(0.0, min(100.0, 50 + (ret / 0.15) * 50))
    _ETF_MOM_CACHE[etf] = score
    return score


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_history(ticker, repo):
    """Daily closes [{date, close}] for a ticker, newest last. None if absent."""
    base = HISTORY_BASE.get(repo)
    if not base:
        return None
    data = fetch_json(f"{base}/{ticker}.json", timeout=30)
    if not isinstance(data, list) or len(data) < 30:
        return None
    closes = []
    for bar in data:
        c = bar.get("close")
        if c is None:
            c = bar.get("price")
        try:
            c = float(c)
        except (TypeError, ValueError):
            continue
        if c and c > 0:
            closes.append(c)
    return closes if len(closes) >= 30 else None


# ----------------------------- signals ----------------------------------------
def sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def rsi(vals, n=14):
    if len(vals) < n + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-n, 0):
        ch = vals[i] - vals[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100 - (100 / (1 + rs))


def score_ticker(closes, weights):
    """A faithful SUBSET of the app's engine → signed score in [-1, +1].

    Components (each signed, then weighted by the bot's learned weights):
      trend         SMA20 vs SMA60 alignment + price above/below
      momentum      3-month return, squashed
      meanReversion RSI extremes (oversold = mild long signal)
    Returns (signed_score, components_dict).
    """
    last = closes[-1]
    s20, s60 = sma(closes, 20), sma(closes, 60)
    comps = {}

    # Trend: +1 strong uptrend, -1 strong downtrend.
    if s20 and s60:
        if last > s20 > s60:
            comps["trend"] = min(1.0, (last - s60) / s60 * 4)
        elif last < s20 < s60:
            comps["trend"] = max(-1.0, (last - s60) / s60 * 4)
        else:
            comps["trend"] = (last - s60) / s60 * 1.5
            comps["trend"] = max(-1.0, min(1.0, comps["trend"]))

    # Momentum: ~3-month (63 trading days) return, squashed with tanh.
    if len(closes) >= 64:
        mom = (last - closes[-64]) / closes[-64]
        comps["momentum"] = math.tanh(mom * 3)

    # Mean reversion: RSI. Oversold (<30) → mild long; overbought (>70) → mild short.
    r = rsi(closes, 14)
    if r is not None:
        if r < 30:
            comps["meanReversion"] = (30 - r) / 30 * 0.6      # up to +0.6 when very oversold
        elif r > 70:
            comps["meanReversion"] = -(r - 70) / 30 * 0.6     # down to -0.6 when very overbought
        else:
            comps["meanReversion"] = 0.0
    return comps


def compute_regime(spy_closes, opt_market=None):
    """Market regime from SPY trend + realized vol → mode + weight modifiers.
    Faithful to the app's botAssessRegime / _regimeProfile. When an options-market
    read is supplied, rich index IV ('fear'/'stressed') de-risks the regime and
    genuine 'calm' can confirm risk-on — the same forward-looking overlay the app
    applies."""
    profiles = {
        "risk-on":  {"weightMods": {"trend": 1.3, "momentum": 1.3, "meanReversion": 0.7,
                                    "peerGrade": 1.2, "regimeGrade": 1.4, "crossAsset": 1.1,
                                    "optionsIV": 0.8, "optionsMarket": 0.9, "fed": 1.0}, "longBar": 0.0},
        "risk-off": {"weightMods": {"trend": 0.7, "momentum": 0.8, "meanReversion": 1.0,
                                    "peerGrade": 1.3, "regimeGrade": 1.5, "crossAsset": 1.2,
                                    "optionsIV": 1.4, "optionsMarket": 1.5, "fed": 1.2}, "longBar": 0.10},
        "choppy":   {"weightMods": {"trend": 0.7, "momentum": 0.8, "meanReversion": 1.3,
                                    "peerGrade": 1.1, "regimeGrade": 1.2, "crossAsset": 1.0,
                                    "optionsIV": 1.3, "optionsMarket": 1.3, "fed": 1.1}, "longBar": 0.05},
    }
    mode = "choppy"
    trend_down = False
    if spy_closes and len(spy_closes) >= 60:
        last = spy_closes[-1]
        s20, s60 = sma(spy_closes, 20), sma(spy_closes, 60)
        rets = [(spy_closes[i] - spy_closes[i - 1]) / spy_closes[i - 1]
                for i in range(len(spy_closes) - 20, len(spy_closes))]
        mean = sum(rets) / len(rets)
        vol20 = math.sqrt(sum((x - mean) ** 2 for x in rets) / len(rets))
        trend_up = last > s20 > s60
        trend_down = last < s20 < s60
        vol_high = vol20 > 0.018
        if trend_down or (vol_high and not trend_up):
            mode = "risk-off"
        elif trend_up and not vol_high:
            mode = "risk-on"

    # Options overlay: the market's chains price in forward risk price history
    # hasn't shown. Fear/stress de-risks; calm confirms.
    opt_note = ""
    if opt_market:
        vr = opt_market.get("volRegime")
        opt_note = f" · opt IV {opt_market.get('indexIV', 0)*100:.0f}% ({vr})"
        if vr in ("fear", "stressed") and mode != "risk-off":
            mode = "choppy" if mode == "risk-on" else "risk-off"
            opt_note += " → de-risked"
        elif vr == "calm" and mode == "choppy" and not trend_down:
            mode = "risk-on"
            opt_note += " → calm confirms"

    prof = profiles[mode]
    prof["mode"] = mode
    prof["optNote"] = opt_note
    return prof


def peer_rank_table(grades):
    """For each ticker, its percentile WITHIN its sector by gradeScore (0-100).
    Mirrors computePeerGrade's peer-rank component."""
    by_sector = {}
    for t, g in grades.items():
        sec = g.get("sector")
        if sec and g.get("gradeScore") is not None:
            by_sector.setdefault(sec, []).append((t, g["gradeScore"]))
    out = {}
    for sec, lst in by_sector.items():
        if len(lst) < 3:
            continue
        lst.sort(key=lambda kv: kv[1], reverse=True)
        n = len(lst)
        for idx, (t, _gs) in enumerate(lst):
            out[t] = round((1 - idx / (n - 1)) * 100) if n > 1 else 50
    return out


def peer_grade_signal(ticker, sector, peer_ranks):
    """Blend sector-relative grade percentile (65%) + sector ETF momentum (35%),
    return signed [-1,+1]. Faithful to computePeerGrade."""
    peer_rank = peer_ranks.get(ticker)
    etf_mom = sector_etf_momentum(sector)
    if peer_rank is not None and etf_mom is not None:
        score = peer_rank * 0.65 + etf_mom * 0.35
    elif peer_rank is not None:
        score = peer_rank
    elif etf_mom is not None:
        score = etf_mom
    else:
        return None
    return (score - 50) / 50.0


def regime_grade_signal(sector, beta, mode, quad=None):
    """Full computeRegimeGrade: quad-favoring (±22) + risk-on/off sector/beta tilt
    (±14/16), signed [-1,+1]. quad is the market-implied proxy when present."""
    if not sector:
        return None
    b = beta if (beta and isfinite_num(beta)) else 1.0
    etf = SECTOR_TO_ETF.get(sector)
    qdef = SECTOR_ETF_QUAD.get(etf) if etf else None
    score = 50.0
    # Quad favoring (the piece newly ported from the macro layer).
    if qdef and quad is not None:
        if quad in qdef.get("favors", []):
            score += 22
        elif quad in qdef.get("hurts", []):
            score -= 22
    # Risk regime tilt by sector/beta.
    is_defensive = any(k in sector.lower() for k in ("utilit", "staple", "consumer defensive", "health"))
    is_cyclical = any(k in sector.lower() for k in ("tech", "consumer cyclical", "discretionary",
                                                    "financ", "industri", "material", "energy"))
    if mode == "risk-on":
        if is_cyclical or b > 1.1:
            score += 14
        elif is_defensive:
            score -= 8
    elif mode == "risk-off":
        if is_defensive or b < 0.9:
            score += 14
        elif is_cyclical or b > 1.1:
            score -= 16
    score = max(0.0, min(100.0, score))
    return (score - 50) / 50.0


def isfinite_num(x):
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


# --- Cross-asset + macro-quad (computed once per run) ---
_XASSET_LAST_RET = {}

def _latest_daily_return(ticker):
    """Last daily % return for a TRAPP2-1 ticker, as a percent (e.g. +0.8)."""
    if ticker in _XASSET_LAST_RET:
        return _XASSET_LAST_RET[ticker]
    data = fetch_json(f"{ETF_HISTORY_BASE}/{ticker}.json", timeout=25)
    ret = None
    if isinstance(data, list) and len(data) >= 2:
        closes = [(_f(b.get("close")) or _f(b.get("price"))) for b in data]
        closes = [c for c in closes if c and c > 0]
        if len(closes) >= 2 and closes[-2]:
            ret = (closes[-1] - closes[-2]) / closes[-2] * 100
    _XASSET_LAST_RET[ticker] = ret
    return ret


def _basket_avg(tickers):
    vals = [r for r in (_latest_daily_return(t) for t in tickers) if r is not None]
    return (sum(vals) / len(vals)) if vals else None


def compute_cross_asset():
    """Latest cross-asset basket moves → signed signals in [-1,+1], faithful to
    extractCrossAssetSignals (using daily returns since the runner is EOD)."""
    out = {}
    d = _basket_avg(XASSET_BASKETS["dollar"])
    out["dollar"] = max(-1, min(1, d / 2)) if d is not None else None
    # safe haven: gold+bonds avg + 0.3×VIX. Risk-OFF when high → invert for risk appetite.
    gold = _latest_daily_return("GLD")
    bonds = _latest_daily_return("TLT")
    vix = _latest_daily_return("^VIX")
    sh_vals = [v for v in (gold, bonds) if v is not None]
    if sh_vals:
        sh = sum(sh_vals) / len(sh_vals) + (vix * 0.3 if vix is not None else 0)
        out["safeHaven"] = max(-1, min(1, sh / 2))
    else:
        out["safeHaven"] = None
    ind = _basket_avg(XASSET_BASKETS["industrial"])
    out["industrial"] = max(-1, min(1, ind / 1.5)) if ind is not None else None
    en = _basket_avg(XASSET_BASKETS["energy"])
    out["energy"] = max(-1, min(1, en / 1.5)) if en is not None else None
    idx = _basket_avg(XASSET_BASKETS["indices"])
    out["indices"] = max(-1, min(1, idx / 1.5)) if idx is not None else None
    return out


def cross_asset_for_sector(sector, xa):
    """Pick the most relevant cross-asset signal for a sector (mirrors the app)."""
    if not sector or not xa:
        return None
    s = sector.lower()
    if any(k in s for k in ("material", "metal", "chem")):
        return xa.get("industrial")
    if "energy" in s:
        return xa.get("energy")
    if any(k in s for k in ("financ", "tech", "communication")):
        return xa.get("indices")
    # default macro alignment = global indices minus safe-haven pull
    idx = xa.get("indices")
    sh = xa.get("safeHaven")
    if idx is None and sh is None:
        return None
    return max(-1, min(1, (idx or 0) - 0.5 * (sh or 0)))


def compute_quad_proxy(xa, regime_mode):
    """MARKET-IMPLIED quad proxy (1-4) when the FRED growth/inflation RoC isn't
    available to the runner. Growth proxy = industrial-metals + index breadth;
    inflation proxy = energy + dollar-inverse. This is an honest market proxy of
    the app's FRED-based classifyQuad, not the macro tab's exact quad.

    Returns (quad:int|None, note:str)."""
    if not xa:
        return None, "no cross-asset data"
    growth_bits = [xa.get("industrial"), xa.get("indices")]
    growth_bits = [b for b in growth_bits if b is not None]
    infl_bits = []
    if xa.get("energy") is not None:
        infl_bits.append(xa["energy"])
    if xa.get("dollar") is not None:
        infl_bits.append(-xa["dollar"])      # strong dollar ↔ disinflationary
    if not growth_bits or not infl_bits:
        return None, "insufficient proxy inputs"
    growth_up = (sum(growth_bits) / len(growth_bits)) > 0
    infl_up = (sum(infl_bits) / len(infl_bits)) > 0
    if growth_up and not infl_up:
        return 1, "proxy: growth↑ inflation↓ (Goldilocks)"
    if growth_up and infl_up:
        return 2, "proxy: growth↑ inflation↑ (Reflation)"
    if not growth_up and infl_up:
        return 3, "proxy: growth↓ inflation↑ (Stagflation)"
    return 4, "proxy: growth↓ inflation↓ (Deflation)"


def load_real_quad():
    """Read the FRED-derived quad written by TRAPP2-1's macro-quad workflow.
    Returns (quad:int, note:str) or (None, reason)."""
    data = fetch_json(MACRO_QUAD_URL, timeout=20)
    if isinstance(data, dict):
        cur = data.get("current") or {}
        q = cur.get("quad")
        if isinstance(q, int) and 1 <= q <= 4:
            return q, f"FRED quad {q} ({cur.get('label','')}, as of {cur.get('asOf','?')})"
    return None, "no FRED quad file"


def resolve_quad(xa, regime_mode):
    """Prefer the REAL FRED quad; fall back to the market-implied proxy."""
    q, note = load_real_quad()
    if q is not None:
        return q, note
    return compute_quad_proxy(xa, regime_mode)


# --- Options: per-ticker IV signal + market-wide options read (computed once) ---
_OPT_CACHE = {}          # ticker -> parsed chain (or None)
_OPT_MARKET = {"done": False, "data": None}

def _load_option_chain(ticker):
    if ticker in _OPT_CACHE:
        return _OPT_CACHE[ticker]
    data = fetch_json(f"{OPTIONS_BASE}/{ticker}.json", timeout=25)
    _OPT_CACHE[ticker] = data if isinstance(data, dict) else None
    return _OPT_CACHE[ticker]

def _atm(lst, spot):
    if not lst:
        return None
    return sorted(lst, key=lambda c: abs((c.get("strike") or 0) - spot))[0]

def extract_options_signal(ticker):
    """Per-name forward signal from the option chain, faithful to the app's
    extractOptionsSignal: ATM IV level + put/call skew + term structure → a small
    signed signal (rich IV / heavy downside skew / backwardation lean negative).
    Returns dict or None."""
    od = _load_option_chain(ticker)
    if not od or not isinstance(od.get("expiries"), list) or not od["expiries"]:
        return None
    spot = od.get("spot")
    if not spot:
        return None
    exps = sorted(od["expiries"], key=lambda e: (e.get("dte") if e.get("dte") is not None else 9999))
    ref = next((e for e in exps if 20 <= (e.get("dte") or 0) <= 60), exps[0])
    if not ref:
        return None
    atm_call, atm_put = _atm(ref.get("calls"), spot), _atm(ref.get("puts"), spot)
    ivs = [x.get("iv") for x in (atm_call, atm_put) if x and x.get("iv") is not None and isfinite_num(x.get("iv"))]
    if not ivs:
        return None
    iv = sum(ivs) / len(ivs)
    skew = None
    if atm_put and atm_call and atm_put.get("iv") is not None and atm_call.get("iv") is not None:
        skew = atm_put["iv"] - atm_call["iv"]
    term_slope = None
    far = next((e for e in exps if (e.get("dte") or 0) >= 100), None)
    if far:
        far_atm = _atm(far.get("calls"), spot)
        if far_atm and far_atm.get("iv") is not None and isfinite_num(far_atm.get("iv")):
            term_slope = iv - far_atm["iv"]
    # Map to a signed signal (same anchors as the app).
    iv_signal = 0.0
    if iv > 0.45:
        iv_signal = -min(1.0, (iv - 0.45) / 0.45)
    elif iv < 0.22:
        iv_signal = min(0.5, (0.22 - iv) / 0.22)
    if skew is not None and skew >= 0.05:
        iv_signal -= min(0.3, (skew - 0.05) / 0.15 * 0.3 + 0.05)
    if term_slope is not None and term_slope > 0.05:
        iv_signal -= min(0.2, (term_slope - 0.05) / 0.15)
    return {"iv": round(iv, 4), "skew": round(skew, 4) if skew is not None else None,
            "termSlope": round(term_slope, 4) if term_slope is not None else None,
            "ivSignal": max(-1.0, min(1.0, round(iv_signal, 3)))}

def compute_options_market(universe_tickers):
    """Aggregate option chains across the manifest into a market-wide read:
    index-led IV → vol regime + a signed market-trend tilt. Faithful to the app's
    computeOptionsMarketSignal. Loads each chain once. Returns dict or None."""
    if _OPT_MARKET["done"]:
        return _OPT_MARKET["data"]
    man = fetch_json(OPTIONS_MANIFEST_URL, timeout=20)
    tickers = (man.get("tickers") if isinstance(man, dict) else None) or []
    rows, idx_iv = [], []
    for tk in tickers:
        sig = extract_options_signal(tk)
        if not sig or sig.get("iv") is None:
            continue
        is_index = tk in MKT_OPT_INDEXES
        rows.append({"ticker": tk, "iv": sig["iv"], "skew": sig["skew"],
                     "termSlope": sig["termSlope"], "isIndex": is_index})
        if is_index:
            idx_iv.append(sig["iv"])
    if not rows:
        _OPT_MARKET.update(done=True, data=None)
        return None
    def avg(xs):
        xs = [x for x in xs if x is not None]
        return sum(xs) / len(xs) if xs else None
    breadth_iv = avg([r["iv"] for r in rows])
    index_iv = (sum(idx_iv) / len(idx_iv)) if idx_iv else breadth_iv
    avg_skew = avg([r["skew"] for r in rows])
    avg_term = avg([r["termSlope"] for r in rows])
    iv_pct = index_iv * 100
    if iv_pct >= 32:
        vol_regime = "fear"
    elif iv_pct >= 24:
        vol_regime = "stressed"
    elif iv_pct >= 17:
        vol_regime = "normal"
    else:
        vol_regime = "calm"
    market_trend = 0.0
    if iv_pct < 17:
        market_trend += min(0.5, (17 - iv_pct) / 17)
    elif iv_pct > 24:
        market_trend -= min(0.8, (iv_pct - 24) / 16)
    if avg_skew is not None and avg_skew >= 0.05:
        market_trend -= min(0.4, (avg_skew - 0.05) / 0.15 * 0.4)
    if avg_term is not None and avg_term > 0.05:
        market_trend -= min(0.3, (avg_term - 0.05) / 0.15 * 0.3)
    data = {"count": len(rows), "indexIV": round(index_iv, 4), "breadthIV": round(breadth_iv, 4),
            "avgSkew": round(avg_skew, 4) if avg_skew is not None else None,
            "volRegime": vol_regime, "marketTrend": max(-1.0, min(1.0, round(market_trend, 3)))}
    _OPT_MARKET.update(done=True, data=data)
    return data


# --- Fed rate expectations (baseline path) ---
# The app's Fed signal blends user-editable odds with seeded market odds in the
# browser; the runner can't see the user's overrides, so it replicates the SEEDED
# baseline (public-record current range + market-implied odds for the remaining
# FOMC meetings). Expected move per meeting = cut×−25 + hike×+25 bps.
FED_CURRENT_RANGE = {"low": 3.50, "high": 3.75}
FOMC_MARKET_ODDS = {
    "2026-06-17": {"cut": 0.06, "hike": 0.00},
    "2026-07-29": {"cut": 0.20, "hike": 0.02},
    "2026-09-16": {"cut": 0.42, "hike": 0.02},
    "2026-10-28": {"cut": 0.30, "hike": 0.02},
    "2026-12-09": {"cut": 0.35, "hike": 0.02},
}

def fed_expectation(today):
    """Expected rate-path bps change from now to year-end, from the seeded odds.
    Negative = net cuts expected (easing). Faithful to fedRateExpectation's
    baseline."""
    mid = (FED_CURRENT_RANGE["low"] + FED_CURRENT_RANGE["high"]) / 2
    expected = mid
    ahead = 0
    for date, odds in sorted(FOMC_MARKET_ODDS.items()):
        if date < today:
            continue
        expected += ((odds["cut"] * -25) + (odds["hike"] * 25)) / 100.0
        ahead += 1
    bps = (expected - mid) * 100
    stance = "easing" if bps < -10 else ("tightening" if bps > 10 else "on-hold")
    return {"expectedBpsChange": round(bps, 1), "stance": stance, "meetingsAhead": ahead}

def fed_signal(sector, fed):
    """Rate tailwind/headwind for a sector, signed [-1,+1]. Easing helps rate-
    sensitive longs (REITs, utilities, homebuilders) and growth; hurts banks.
    Mirrors the app engine's 'fed' component."""
    if not sector or not fed:
        return None
    easing = max(-1.0, min(1.0, -fed["expectedBpsChange"] / 75.0))   # cuts → positive
    s = sector.lower()
    if any(k in s for k in ("real estate", "reit", "utilit", "homebuild")):
        sens = 1.0
    elif any(k in s for k in ("tech", "growth", "biotech")):
        sens = 0.5
    elif any(k in s for k in ("financ", "bank")):
        sens = -0.4
    else:
        return None
    return max(-1.0, min(1.0, round(easing * sens, 3)))


# Per-signal BASE weights — copied verbatim from the in-app engine's add() calls
# (app.js). The runner previously blended every signal at equal weight (1.0),
# which made its decisions diverge from the app even with identical inputs.
# These restore the app's relative signal importance for the 9 shared signals.
BASE_WEIGHTS = {
    # Fundamental core (ported from the app's bot engine — was missing server-side).
    "fundamentals": 0.26, "researchGrade": 0.16, "health": 0.12,
    # Price / cross-sectional / macro signals.
    "trend": 0.18, "momentum": 0.14, "meanReversion": 0.06,
    "crossAsset": 0.10, "peerGrade": 0.12, "regimeGrade": 0.14,
    "optionsIV": 0.07, "optionsMarket": 0.05, "fed": 0.08,
}


def blend_score(comps, weights, weight_mods):
    """Weighted blend mirroring the app engine: base × learned × regime-mod.

    base    = BASE_WEIGHTS (the app's per-signal importance)
    learned = the bot's learnedWeights (1.0 = neutral; grows from feedback)
    mod     = the regime tilt (weightMods). Matches app.js add() exactly.
    """
    num, den = 0.0, 0.0
    for k, v in comps.items():
        if v is None:
            continue
        base = BASE_WEIGHTS.get(k, 0.10)          # app per-signal base weight
        learned = weights.get(k, 1.0)             # learning layer (1.0 = neutral)
        try:
            learned = float(learned)
        except (TypeError, ValueError):
            learned = 1.0
        mod = weight_mods.get(k, 1.0)             # regime tilts the signal's influence
        w = base * learned * mod
        num += v * w
        den += abs(w)
    signed = (num / den) if den else 0.0
    return max(-1.0, min(1.0, signed))


# ----------------------------- position management ----------------------------
def realized_pnl(pos, exit_price):
    """Shares-based realized P&L — identical to the app's z35 freeze (long-only here)."""
    shares = pos.get("shares")
    entry = pos.get("entryPrice")
    if not shares and entry:
        shares = (pos.get("notional") or pos.get("dollars") or 0) / entry
    fees = pos.get("fees") or 0
    direction = -1 if pos.get("direction") == "short" else 1
    lev = pos.get("leverage") or 1
    return round(direction * (exit_price - entry) * shares * lev - fees, 2)


def _norm_fund(u):
    """Map master.json fields to the engine's names + derive missing ratios.
    Faithful to the app's normalizeEngineFields."""
    def n(v):
        try:
            x = float(v)
            return x if (x == x and x not in (float("inf"), float("-inf"))) else None
        except (TypeError, ValueError):
            return None
    f = {
        "marketCap": n(u.get("marketcap")) if n(u.get("marketcap")) is not None else n(u.get("marketCap")),
        "eps": n(u.get("eps")),
        "netMargin": n(u.get("profitMargin")),
        "freeCashFlow": n(u.get("freeCashFlow")),
        "roe": n(u.get("returnOnEquity")),
        "debtToEquity": n(u.get("debtToEquity")),
        "currentRatio": n(u.get("currentRatio")),
        "cash": n(u.get("cash")),
        "totalDebt": n(u.get("totalDebt")),
        "totalEquity": n(u.get("totalEquity")),
        "pe": n(u.get("pe")),
        "priceToBook": n(u.get("priceToBook")),
        "revenueGrowth": n(u.get("revenueGrowth")),
        "payoutRatio": n(u.get("payoutRatio")),
    }
    if f["debtToEquity"] is None:
        d, e = n(u.get("totalDebt")), n(u.get("totalEquity"))
        if d is not None and e and e > 0:
            f["debtToEquity"] = d / e
    ps = n(u.get("priceToSales"))
    if ps is None:
        mc, rev = f["marketCap"], n(u.get("revenue"))
        if mc is not None and rev and rev > 0:
            ps = mc / rev
    f["priceToSales"] = ps
    # master.json emits dividend_yield as a PERCENT (0.49 = 0.49%) → decimal.
    dy = n(u.get("dividend_yield"))
    if dy is None:
        dy = n(u.get("dividendYield"))
    if dy is None:
        f["dividendYield"] = None
    elif dy == 0:
        f["dividendYield"] = 0.0
    else:
        f["dividendYield"] = (dy / 100) if dy <= 40 else None
    return f


def _fund_engine(u):
    """Port of financialReasoningChain (-> conviction) + computeCompanyHealth's
    dimension composite (-> health), both 0-100. One pass over the same fundamental
    step points powers both signals, exactly as the app does."""
    f = _norm_fund(u)
    steps = {}
    mc = f["marketCap"]
    if mc is None:
        steps[1] = None
    elif mc >= 1e12: steps[1] = 100
    elif mc >= 200e9: steps[1] = 92
    elif mc >= 10e9: steps[1] = 80
    elif mc >= 2e9: steps[1] = 62
    elif mc >= 1e9: steps[1] = 52
    elif mc >= 300e6: steps[1] = 38
    elif mc >= 50e6: steps[1] = 20
    else: steps[1] = 10
    over1b = mc is not None and mc >= 1e9

    eps, nm, fcf, roe = f["eps"], f["netMargin"], f["freeCashFlow"], f["roe"]
    if eps is not None or nm is not None or fcf is not None:
        profitable = (eps is not None and eps > 0) or (nm is not None and nm > 0) or (fcf is not None and fcf > 0)
        if profitable:
            steps[2] = min(100.0, 55 + (min(30.0, nm * 150) if nm is not None else 0) + (10 if (roe is not None and roe > 0.15) else 0))
        else:
            steps[2] = max(0.0, 35 - (20 if (nm is not None and nm < -0.1) else 0))
    else:
        steps[2] = None

    de, curr, cash, debt, eq = f["debtToEquity"], f["currentRatio"], f["cash"], f["totalDebt"], f["totalEquity"]
    if de is not None or curr is not None or (cash is not None and debt is not None):
        pts = 50.0
        if de is not None: pts += 18 if de < 0.5 else 8 if de < 1 else -5 if de < 2 else -20
        if curr is not None: pts += 12 if curr > 2 else 4 if curr > 1 else -18
        if cash is not None and debt is not None and cash > debt: pts += 12
        if eq is not None and eq < 0: pts -= 25
        steps[3] = max(0.0, min(100.0, pts))
    else:
        steps[3] = None

    pe, pb, ps, rg = f["pe"], f["priceToBook"], f["priceToSales"], f["revenueGrowth"]
    if pe is not None or pb is not None or ps is not None:
        pts = 50.0
        quality = (roe is not None and roe > 0.15) or (rg is not None and rg > 0.15)
        if pe is not None and pe > 0:
            fair = 35 if quality else 20
            if pe < fair * 0.6: pts += 18
            elif pe < fair: pts += 6
            elif pe < fair * 1.7: pts -= 6
            else: pts -= 18
        if ps is not None:
            if ps < 2: pts += 8
            elif ps > 12: pts -= 10
        if pb is not None and pb < 1: pts += 10
        steps[4] = max(0.0, min(100.0, pts))
    else:
        steps[4] = None

    dy, payout = f["dividendYield"], f["payoutRatio"]
    if dy is not None and dy > 0:
        pts = 50 + min(25.0, dy * 500)
        if payout is not None:
            if payout > 0.9: pts -= 20
            elif payout < 0.6: pts += 10
        steps[5] = max(0.0, min(100.0, pts))
    elif dy == 0:
        steps[5] = 50.0
    else:
        steps[5] = None

    steps[6] = max(0.0, min(100.0, 50 + rg * 150)) if rg is not None else None

    cw = ({1: 0.12, 2: 0.22, 3: 0.20, 4: 0.22, 5: 0.10, 6: 0.14} if over1b
          else {1: 0.10, 2: 0.24, 3: 0.28, 4: 0.18, 5: 0.04, 6: 0.16})
    ssum = wsum = 0.0
    for k, p in steps.items():
        if p is not None:
            ssum += p * cw[k]; wsum += cw[k]
    conviction = (ssum / wsum) if wsum > 0 else None

    # Health = 60% dimension composite + 40% chain, dims mapped to the same step
    # points (scale/profit/strength/value/income/growth), stage-weighted.
    is_growth = (rg or 0) > 0.15 and (dy or 0) < 0.01
    is_div = (dy or 0) > 0.02
    if is_growth:
        hw = {1: 0.08, 2: 0.10, 4: 0.16, 3: 0.14, 6: 0.24, 5: 0.04}
    elif is_div:
        hw = {1: 0.08, 2: 0.18, 4: 0.13, 3: 0.18, 6: 0.08, 5: 0.17}
    else:
        hw = {1: 0.10, 2: 0.18, 4: 0.15, 3: 0.13, 6: 0.12, 5: 0.08}
    hsum = hwsum = 0.0
    for k, w in hw.items():
        p = steps.get(k)
        if p is not None:
            hsum += p * w; hwsum += w
    dim_comp = (hsum / hwsum) if hwsum > 0 else None
    if dim_comp is not None and conviction is not None:
        health = dim_comp * 0.6 + conviction * 0.4
    elif conviction is not None:
        health = conviction
    else:
        health = dim_comp
    return {"conviction": conviction, "health": health}


def score_symbol(tk, u, ctx):
    """Full signal blend for one name — the SAME pipeline open_new uses, so a held
    position is re-scored identically for thesis checks."""
    closes = load_history(tk, u["repo"])
    if not closes:
        return None
    comps = score_ticker(closes, ctx["weights"])
    # Fundamental core (reasoning-chain conviction + health) + research grade.
    fe = _fund_engine(u)
    if fe.get("conviction") is not None:
        comps["fundamentals"] = max(-1.0, min(1.0, (fe["conviction"] - 50) / 50))
    if fe.get("health") is not None:
        comps["health"] = max(-1.0, min(1.0, (fe["health"] - 50) / 50))
    g = (ctx.get("grades") or {}).get(tk)
    if g and g.get("gradeScore") is not None:
        comps["researchGrade"] = max(-1.0, min(1.0, (g["gradeScore"] - 50) / 50))
    pg = peer_grade_signal(tk, u.get("sector"), ctx["peer_ranks"])
    if pg is not None:
        comps["peerGrade"] = pg
    rg = regime_grade_signal(u.get("sector"), u.get("beta"), ctx["mode"], ctx["quad"])
    if rg is not None:
        comps["regimeGrade"] = rg
    ca = cross_asset_for_sector(u.get("sector"), ctx["xa"])
    if ca is not None:
        comps["crossAsset"] = ca
    osig = extract_options_signal(tk)
    if osig and osig.get("ivSignal") is not None and abs(osig["ivSignal"]) > 0.05:
        comps["optionsIV"] = osig["ivSignal"]
    if ctx.get("opt_market") and ctx["opt_market"].get("marketTrend") is not None \
            and abs(ctx["opt_market"]["marketTrend"]) > 0.03:
        comps["optionsMarket"] = ctx["opt_market"]["marketTrend"]
    if ctx.get("fed"):
        fs = fed_signal(u.get("sector"), ctx["fed"])
        if fs is not None and abs(fs) > 0.02:
            comps["fed"] = fs
    return {"comps": comps, "signed": blend_score(comps, ctx["weights"], ctx["weight_mods"])}


def compute_confidence(comps, signed):
    """0..1 — how much the signals AGREE, how STRONG the blend is, how BROAD the
    support. Distinct from raw score: a +0.4 driven by one signal is less certain
    than a +0.4 with six aligned signals."""
    active = [v for v in comps.values() if isinstance(v, (int, float)) and abs(v) > 0.05]
    if not active:
        return 0.0
    same = sum(1 for v in active if (v > 0) == (signed >= 0))
    agreement = same / len(active)
    strength = min(1.0, abs(signed) / 0.6)
    breadth = min(1.0, len(active) / 6.0)
    return round(0.5 * agreement + 0.35 * strength + 0.15 * breadth, 3)


def compute_triggers(comps, signed, level):
    """The signals that actually FIRED to make this trade attractive: aligned with
    the direction and at least `level` strong. This is what makes a trade real vs a
    marginal blend — and it's stored so the reasoning is auditable."""
    dir_pos = signed >= 0
    fired = [{"signal": k, "value": round(v, 3)}
             for k, v in comps.items()
             if isinstance(v, (int, float)) and abs(v) >= level and (v > 0) == dir_pos]
    fired.sort(key=lambda x: abs(x["value"]), reverse=True)
    return fired


def classify_horizon(comps, confidence):
    """SWING vs CORE. CORE = high confidence AND the blend leans on slow, fundamental
    / macro signals (peer grade, regime, Fed, cross-asset). Those get ridden and
    trimmed; momentum names stay swing with tight exits."""
    lt = sum(abs(comps.get(k, 0) or 0) for k in LONG_TERM_SIGNALS)
    sw = sum(abs(comps.get(k, 0) or 0) for k in SWING_SIGNALS)
    lean = lt / (lt + sw + 1e-9)
    return ("core" if (confidence >= CORE_CONF and lean >= 0.45) else "swing"), round(lean, 3)


def manage_open_positions(state, universe, today, ctx=None):
    """Mark to market, then apply CONFIDENCE- and HORIZON-aware management:
      • Every held name is re-scored and RE-CLASSIFIED each run (the bot reassesses
        what it holds), so a position that has grown into a high-conviction, macro-
        driven idea is treated as CORE even if it was opened as a swing.
      • SWING: tight -8% stop / +16% target; horizon exit ONLY once the signal that
        justified it has faded (no arbitrary calendar dump).
      • CORE : no fixed horizon. Disaster stop at -25%; a trailing stop protects big
        gains once in profit; a decisive signal flip closes on thesis-break. A long
        winner is TRIMMED in tiers (+25/50/100%) rather than force-sold — held like a
        value position."""
    closed_now, trimmed_now = [], []
    for pos in trades_list(state):
        if pos.get("status") != "open":
            continue
        u = universe.get((pos.get("ticker") or "").upper())
        if not u:
            continue
        px = u["price"]
        pos["lastPrice"] = px
        direction = -1 if pos.get("direction") == "short" else 1
        entry = pos.get("entryPrice") or px
        shares = pos.get("shares") or ((pos.get("notional") or 0) / entry if entry else 0)
        pos["pnl"] = round(direction * (px - entry) * shares, 2)
        ret = (direction * (px - entry) / entry) if entry else 0.0

        # favorable peak, for the core trailing stop
        peak = pos.get("peakPrice")
        peak = entry if peak is None else peak
        peak = max(peak, px) if direction == 1 else min(peak, px)
        pos["peakPrice"] = round(peak, 4)
        peak_ret = (direction * (peak - entry) / entry) if entry else 0.0

        # Re-score + RE-CLASSIFY this holding (the bot reassesses what it owns).
        signed_now = None
        htype = pos.get("horizonType") or "swing"
        if ctx is not None:
            sc = score_symbol(pos["ticker"], u, ctx)
            if sc:
                signed_now = sc["signed"]
                conf_now = compute_confidence(sc["comps"], signed_now)
                htype, lean_now = classify_horizon(sc["comps"], conf_now)
                pos["scoreNow"] = round(signed_now, 3)
                pos["confidence"] = conf_now
                pos["horizonType"] = htype
                pos["longTermLean"] = lean_now

        aligned_now = (signed_now * direction) if signed_now is not None else None
        exit_reason = None

        if htype == "core":
            if ret <= -CORE_HARD_STOP:
                exit_reason = "hard-stop"
            elif aligned_now is not None and aligned_now <= -CORE_EXIT_SCORE:
                exit_reason = "thesis-break"
            elif peak_ret >= 0.10 and direction == 1 and px <= peak * (1 - CORE_TRAIL):
                exit_reason = "trail-stop"
            elif peak_ret >= 0.10 and direction == -1 and px >= peak * (1 + CORE_TRAIL):
                exit_reason = "trail-stop"
            else:
                # TRIM winners in tiers instead of a hard target exit (long only).
                tier = int(pos.get("trimTier") or 0)
                if direction == 1 and tier < len(CORE_TRIM_TIERS) and ret >= CORE_TRIM_TIERS[tier]:
                    trim_shares = round(shares * CORE_TRIM_FRAC, 4)
                    if trim_shares > 0 and (shares - trim_shares) > 0:
                        realized = round((px - entry) * trim_shares - abs(px * trim_shares) * FEE_BPS, 2)
                        new_shares = round(shares - trim_shares, 4)
                        pos["shares"] = new_shares
                        pos["notional"] = round(new_shares * entry, 2)  # cost basis of remainder
                        pos["trimTier"] = tier + 1
                        pos.setdefault("trims", []).append(
                            {"date": today, "shares": trim_shares, "price": px,
                             "realized": realized, "tier": tier + 1, "atReturnPct": round(ret * 100, 2)})
                        pos["realizedTrim"] = round((pos.get("realizedTrim") or 0) + realized, 2)
                        state["bankroll"] = round(state.get("bankroll", STARTING_BANKROLL) + realized, 2)
                        trimmed_now.append((pos["ticker"], trim_shares, realized, tier + 1))
                # else: HOLD — ride it; no calendar exit
        else:
            stop, target = pos.get("stopPrice"), pos.get("targetPrice")
            if stop and ((direction == 1 and px <= stop) or (direction == -1 and px >= stop)):
                exit_reason = "stop-loss"
            elif target and ((direction == 1 and px >= target) or (direction == -1 and px <= target)):
                exit_reason = "target"
            elif aligned_now is not None and aligned_now <= -CORE_EXIT_SCORE:
                exit_reason = "thesis-break"
            else:
                # Horizon exit ONLY when the reason to hold (the signal) has faded.
                ed = pos.get("entryDate")
                hd = pos.get("horizonDays") or SWING_HORIZON
                signal_gone = (aligned_now is None) or (aligned_now < MIN_SCORE)
                if ed and hd and signal_gone:
                    try:
                        age = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(ed[:10], "%Y-%m-%d")).days
                        if age >= hd:
                            exit_reason = "horizon"
                    except ValueError:
                        pass

        if exit_reason:
            r = realized_pnl(pos, px)
            pos["status"] = "closed"
            pos["exitDate"] = today
            pos["exitPrice"] = px
            pos["exitReason"] = exit_reason
            total_r = round(r + (pos.get("realizedTrim") or 0), 2)  # include locked trim gains
            pos["realizedPL"] = total_r
            pos["pnl"] = total_r
            pos["won"] = total_r > 0
            entry_cap = (shares or 0) * entry
            pos["returnPct"] = round((r / entry_cap) * 100, 2) if entry_cap else 0
            pos["sharesAtExit"] = round(shares or 0, 4)
            state["bankroll"] = round(state.get("bankroll", STARTING_BANKROLL) + r, 2)
            closed_now.append((pos["ticker"], exit_reason, total_r))
    for tk, why, r in closed_now:
        log(f"  closed {tk} ({why}) realized ${r:,.2f}")
    for tk, sh, r, tier in trimmed_now:
        log(f"  trimmed {tk} {sh:g} sh @tier{tier} -> locked ${r:,.2f}")
    return closed_now


# ----------------------------- open new trades --------------------------------
def open_new_trades(state, universe, today, regime, grades, peer_ranks, xa, quad, opt_market=None, fed=None):
    bankroll = state.get("bankroll", STARTING_BANKROLL)
    held = {(p.get("ticker") or "").upper() for p in trades_list(state) if p.get("status") == "open"}
    n_open = len(held)
    if n_open >= MAX_POSITIONS:
        log(f"  at max positions ({n_open}/{MAX_POSITIONS}) — no new entries")
        return []

    weights = state.get("learnedWeights", {}) if isinstance(state.get("learnedWeights"), dict) else {}
    weight_mods = regime.get("weightMods", {})
    mode = regime.get("mode", "choppy")
    min_score = max(MIN_SCORE, regime.get("longBar", 0.0) + MIN_SCORE * 0.0)
    ctx = {"weights": weights, "weight_mods": weight_mods, "mode": mode,
           "peer_ranks": peer_ranks, "xa": xa, "quad": quad,
           "opt_market": opt_market, "fed": fed, "grades": grades}

    committed = sum((p.get("notional") or 0) for p in trades_list(state) if p.get("status") == "open")
    cash = bankroll - committed
    reserve = bankroll * CASH_RESERVE_PCT
    deployable = max(0.0, cash - reserve)
    if deployable < bankroll * 0.02:
        log(f"  only ${deployable:,.0f} deployable after reserve — sitting out")
        return []

    candidates = []
    scanned = 0
    for tk, u in universe.items():
        if tk in held:
            continue
        sc = score_symbol(tk, u, ctx)
        scanned += 1
        if sc is None:
            continue
        comps, signed = sc["comps"], sc["signed"]
        conf = compute_confidence(comps, signed)
        trigs = compute_triggers(comps, signed, TRIGGER_LEVEL)
        # Only take a trade that actually has concrete triggers firing for it.
        if signed >= min_score and len(trigs) >= MIN_TRIGGERS:
            candidates.append({"ticker": tk, "score": signed, "direction": "long",
                               "components": comps, "confidence": conf, "triggers": trigs,
                               "price": u["price"], "sector": u["sector"], "name": u["name"]})
        elif ALLOW_SHORTS and signed <= SHORT_SCORE and len(trigs) >= MIN_TRIGGERS:
            candidates.append({"ticker": tk, "score": abs(signed), "direction": "short",
                               "components": comps, "confidence": conf, "triggers": trigs,
                               "price": u["price"], "sector": u["sector"], "name": u["name"]})
    candidates.sort(key=lambda c: c["score"], reverse=True)
    n_long = sum(1 for c in candidates if c["direction"] == "long")
    n_short = sum(1 for c in candidates if c["direction"] == "short")
    log(f"  regime={mode} quad={quad} · scanned {scanned} · {n_long} long"
        f"{f' / {n_short} short' if ALLOW_SHORTS else ''} pass (min {min_score:.2f})")

    opened = []
    slots = min(MAX_NEW_TRADES, MAX_POSITIONS - n_open)
    for c in candidates[:slots]:
        conf = c.get("confidence", 0.0)
        htype, lean = classify_horizon(c["components"], conf)
        is_short = c.get("direction") == "short"
        # Confidence-scaled sizing: 0.6x (low) → 1.4x (high), hard-capped at 1.6x base.
        size = min(bankroll * POSITION_PCT * (0.6 + 0.8 * conf),
                   deployable, bankroll * POSITION_PCT * 1.6)
        if size < bankroll * 0.01:
            break
        shares = round(size / c["price"], 4)
        if shares <= 0:
            continue
        notional = round(shares * c["price"], 2)
        fees = round(notional * FEE_BPS, 2)
        if htype == "core":
            stop = round(c["price"] * ((1 + CORE_HARD_STOP) if is_short else (1 - CORE_HARD_STOP)), 4)
            target = None                                   # ride it; managed by trim + thesis-break
            horizon_days = None                             # no fixed-calendar exit
            style = "core"
        else:
            stop = round(c["price"] * ((1 + SWING_STOP) if is_short else (1 - SWING_STOP)), 4)
            target = round(c["price"] * ((1 - SWING_TARGET) if is_short else (1 + SWING_TARGET)), 4)
            horizon_days = int(SWING_HORIZON * (1 + conf))  # more confidence → longer leash
            style = "momentum"
        trigs = c.get("triggers", [])
        trade = {
            "id": f"{c['ticker']}-{today}-runner-{int(datetime.now(timezone.utc).timestamp())}",
            "ticker": c["ticker"], "name": c["name"], "sector": c["sector"],
            "direction": "short" if is_short else "long", "instrument": "shares",
            "style": style, "horizonType": htype, "horizonDays": horizon_days,
            "entryDate": today, "exitDate": None,
            "entryPrice": c["price"], "exitPrice": None, "peakPrice": c["price"],
            "shares": shares, "notional": notional, "dollars": notional,
            "allocationPct": round(notional / bankroll * 100, 2), "leverage": 1, "hedge": False,
            "stopPrice": stop, "targetPrice": target, "trimTier": 0, "trims": [],
            "fees": fees, "realizedPL": 0, "pnl": 0, "returnPct": 0, "won": False,
            "exitReason": "open", "status": "open",
            "conviction": round(c["score"] if not is_short else -c["score"], 3),
            "confidence": round(conf, 3), "longTermLean": lean,
            "components": {k: round(v, 3) for k, v in c["components"].items()},
            "topDrivers": trigs,
            "signalsThatHelped": [t["signal"] for t in trigs],
            "rationale": [f"{htype.upper()} · conf {conf:.2f} · score {c['score']:.2f}. Triggers: " +
                          (", ".join(f"{t['signal']} {t['value']:+.2f}" for t in trigs) or "none")],
            "cashAfter": round(cash - notional, 2),
            "placedBy": "runner",
        }
        # cash decrements on buy so the book stays consistent
        committed += notional
        cash -= notional
        deployable -= notional
        state.setdefault("trades", []).append(trade)
        opened.append((c["ticker"], notional, c["score"]))
        if not state.get("startedAt"):
            state["startedAt"] = today
    for tk, notional, sc in opened:
        log(f"  opened {tk} ${notional:,.0f} (score {sc:.2f})")
    return opened


# ----------------------------- performance + equity ---------------------------
def recompute(state, universe, today):
    bets = trades_list(state)
    closed = [b for b in bets if b.get("status") == "closed"]
    open_b = [b for b in bets if b.get("status") == "open"]

    def _pl(b):
        v = b.get("realizedPL")
        return v if isinstance(v, (int, float)) else (b.get("pnl") or 0)

    wins = [b for b in closed if _pl(b) > 0]
    losses = [b for b in closed if _pl(b) <= 0]
    total_pnl = round(sum(_pl(b) for b in closed), 2)
    win_rate = round(len(wins) / len(closed), 3) if closed else None
    avg_win = round(sum(_pl(b) for b in wins) / len(wins), 2) if wins else None
    avg_loss = round(sum(_pl(b) for b in losses) / len(losses), 2) if losses else None
    pf = (round(abs((avg_win * len(wins)) / (avg_loss * len(losses))), 2)
          if avg_loss and wins and losses else None)

    # mark-to-market book value = bankroll + open unrealized
    open_pnl = sum((b.get("pnl") or 0) for b in open_b)
    book = round(state.get("bankroll", STARTING_BANKROLL) + open_pnl, 2)

    state["counts"] = {"total": len(bets), "open": len(open_b), "closed": len(closed),
                       "wins": len(wins), "losses": len(losses)}
    state["performance"] = {"winRate": win_rate, "totalPnl": total_pnl,
                            "avgWin": avg_win, "avgLoss": avg_loss, "profitFactor": pf}
    state["allTimeReturnPct"] = round((state.get("bankroll", STARTING_BANKROLL) - STARTING_BANKROLL)
                                      / STARTING_BANKROLL * 100, 2)
    state["openPositions"] = [{"ticker": b["ticker"], "direction": b.get("direction"),
                               "instrument": b.get("instrument"), "entryDate": b.get("entryDate"),
                               "entryPrice": b.get("entryPrice"), "conviction": b.get("conviction")}
                              for b in open_b]

    # one equity-curve point per day (replace today's if it already exists)
    curve = state.get("equityCurve") if isinstance(state.get("equityCurve"), list) else []
    curve = [p for p in curve if p.get("date") != today]
    curve.append({"date": today, "value": book})
    state["equityCurve"] = curve[-400:]

    # benchmark anchor for vs-SPY (price captured once on first run)
    spy = universe.get("SPY")
    if spy and not state.get("benchmarkStart"):
        state["benchmarkStart"] = spy["price"]

    state["schema"] = "valuatio-bot-training/v1"
    state["generatedAt"] = datetime.now(timezone.utc).isoformat()
    state["startingBankroll"] = STARTING_BANKROLL
    return state


# ----------------------------- main -------------------------------------------
def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log(f"=== run {today} · dry_run={DRY_RUN} ===")
    log(f"limits: maxNew={MAX_NEW_TRADES} maxPos={MAX_POSITIONS} sizePct={POSITION_PCT*100:.0f} "
        f"reservePct={CASH_RESERVE_PCT*100:.0f} minScore={MIN_SCORE}")

    state = load_state()
    log(f"state: bankroll ${state.get('bankroll', STARTING_BANKROLL):,.2f} · "
        f"{sum(1 for b in trades_list(state) if b.get('status')=='open')} open · "
        f"{len(trades_list(state))} total trades")

    universe = load_universe()
    if not universe:
        log("✗ no universe data — aborting (won't write empty state)")
        sys.exit(1)

    # Macro + cross-sectional context (faithful peer-grade + regime signals).
    grades = load_grades()
    peer_ranks = peer_rank_table(grades)
    spy_data = fetch_json(SPY_HISTORY_URL, timeout=30)
    spy_closes = None
    if isinstance(spy_data, list):
        spy_closes = [(_f(b.get("close")) or _f(b.get("price"))) for b in spy_data]
        spy_closes = [c for c in spy_closes if c and c > 0]
    regime = compute_regime(spy_closes)
    xa = compute_cross_asset()
    # Market-wide options read (index IV → vol regime + market tilt). Loaded once.
    opt_market = compute_options_market(list(universe.keys()))
    regime = compute_regime(spy_closes, opt_market)
    fed = fed_expectation(today)
    # Prefer the REAL FRED quad (macro_quad.json, written by the macro-quad
    # workflow). Fall back to the market-implied proxy only if the file is absent.
    quad, quad_note = None, ""
    mq = fetch_json(f"{RAW}/TRAPP2-1/main/data/macro_quad.json", timeout=20)
    if isinstance(mq, dict) and isinstance(mq.get("quad"), int):
        quad = mq["quad"]
        quad_note = f"FRED quad {quad}: {mq.get('quadName', '')}"
    else:
        quad, quad_note = resolve_quad(xa, regime.get("mode"))
    xa_have = [k for k, v in xa.items() if v is not None]
    opt_note = ""
    if opt_market:
        opt_note = f" · options {opt_market['indexIV']*100:.0f}% IV ({opt_market['volRegime']}, {opt_market['count']} chains)"
    fed_note = f" · Fed {fed['stance']} ({fed['expectedBpsChange']:+.0f}bps/{fed['meetingsAhead']}mtg)" if fed else ""
    log(f"regime: {regime['mode']}{regime.get('optNote','')} · {quad_note} · cross-asset: {', '.join(xa_have) or 'none'} · "
        f"peer-ranked sectors cover {len(peer_ranks)} tickers{opt_note}{fed_note}")

    weights = state.get("learnedWeights", {}) if isinstance(state.get("learnedWeights"), dict) else {}
    ctx = {"weights": weights, "weight_mods": regime.get("weightMods", {}),
           "mode": regime.get("mode", "choppy"), "peer_ranks": peer_ranks,
           "xa": xa, "quad": quad, "opt_market": opt_market, "fed": fed, "grades": grades}
    closed = manage_open_positions(state, universe, today, ctx)
    opened = open_new_trades(state, universe, today, regime, grades, peer_ranks, xa, quad, opt_market, fed)
    recompute(state, universe, today)

    log(f"summary: +{len(opened)} opened · {len(closed)} closed · "
        f"bankroll ${state['bankroll']:,.2f} · book ${state['equityCurve'][-1]['value']:,.2f}")

    if DRY_RUN:
        log("DRY RUN — not writing state.")
        return
    if not opened and not closed:
        # Still write the refreshed marks/equity point so the curve advances daily,
        # but only if something actually changed in the equity value.
        pass
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, separators=(",", ":")))
    log(f"wrote {STATE_FILE}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
