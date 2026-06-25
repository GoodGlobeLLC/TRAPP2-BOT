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
STATE_FILE = ROOT / "bot_training_data.json"

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
MAX_POSITIONS = int(_envf("RUNNER_MAX_POSITIONS", 15))
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
    if STATE_FILE.exists():
        try:
            d = json.loads(STATE_FILE.read_text())
            if isinstance(d, dict):
                return d
        except Exception as e:
            log(f"  ! state parse failed: {e}")
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


def blend_score(comps, weights, weight_mods):
    """Weighted blend of signed components by learned weights × regime modifiers."""
    num, den = 0.0, 0.0
    for k, v in comps.items():
        if v is None:
            continue
        w = weights.get(k, 1.0)
        try:
            w = float(w)
        except (TypeError, ValueError):
            w = 1.0
        w *= weight_mods.get(k, 1.0)       # regime tilts the signal's influence
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


def manage_open_positions(state, universe, today):
    """Mark to market; close stop/target/horizon hits with frozen realized P&L."""
    closed_now = []
    for pos in trades_list(state):
        if pos.get("status") != "open":
            continue
        u = universe.get((pos.get("ticker") or "").upper())
        if not u:
            continue
        px = u["price"]
        pos["lastPrice"] = px
        # unrealized mark (long-only)
        shares = pos.get("shares") or ((pos.get("notional") or 0) / pos["entryPrice"] if pos.get("entryPrice") else 0)
        direction = -1 if pos.get("direction") == "short" else 1
        pos["pnl"] = round(direction * (px - pos["entryPrice"]) * shares, 2)

        exit_reason = None
        stop, target = pos.get("stopPrice"), pos.get("targetPrice")
        if stop and ((direction == 1 and px <= stop) or (direction == -1 and px >= stop)):
            exit_reason = "stop-loss"
        elif target and ((direction == 1 and px >= target) or (direction == -1 and px <= target)):
            exit_reason = "target"
        else:
            # horizon check
            ed = pos.get("entryDate")
            hd = pos.get("horizonDays") or 21
            if ed:
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
            pos["realizedPL"] = r
            pos["pnl"] = r
            pos["won"] = r > 0
            entry_cap = (shares or 0) * pos["entryPrice"]
            pos["returnPct"] = round((r / entry_cap) * 100, 2) if entry_cap else 0
            pos["sharesAtExit"] = round(shares or 0, 4)
            state["bankroll"] = round(state.get("bankroll", STARTING_BANKROLL) + r, 2)
            closed_now.append((pos["ticker"], exit_reason, r))
    for tk, why, r in closed_now:
        log(f"  closed {tk} ({why}) realized ${r:,.2f}")
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
        closes = load_history(tk, u["repo"])
        scanned += 1
        if not closes:
            continue
        comps = score_ticker(closes, weights)               # trend/momentum/meanReversion
        pg = peer_grade_signal(tk, u.get("sector"), peer_ranks)
        if pg is not None:
            comps["peerGrade"] = pg
        rg = regime_grade_signal(u.get("sector"), u.get("beta"), mode, quad)
        if rg is not None:
            comps["regimeGrade"] = rg
        ca = cross_asset_for_sector(u.get("sector"), xa)
        if ca is not None:
            comps["crossAsset"] = ca
        # Per-name options IV signal (only for names with a chain in the repo).
        osig = extract_options_signal(tk)
        if osig and osig.get("ivSignal") is not None and abs(osig["ivSignal"]) > 0.05:
            comps["optionsIV"] = osig["ivSignal"]
        # Market-wide options tilt (uniform across the book).
        if opt_market and opt_market.get("marketTrend") is not None and abs(opt_market["marketTrend"]) > 0.03:
            comps["optionsMarket"] = opt_market["marketTrend"]
        # Fed rate tailwind/headwind for rate-sensitive sectors.
        if fed:
            fs = fed_signal(u.get("sector"), fed)
            if fs is not None and abs(fs) > 0.02:
                comps["fed"] = fs
        signed = blend_score(comps, weights, weight_mods)
        if signed >= min_score:
            candidates.append({"ticker": tk, "score": signed, "direction": "long",
                               "components": comps, "price": u["price"],
                               "sector": u["sector"], "name": u["name"]})
        elif ALLOW_SHORTS and signed <= SHORT_SCORE:
            # Strongly-negative signal → short candidate (rank by |score|).
            candidates.append({"ticker": tk, "score": abs(signed), "direction": "short",
                               "components": comps, "price": u["price"],
                               "sector": u["sector"], "name": u["name"]})
    candidates.sort(key=lambda c: c["score"], reverse=True)
    n_long = sum(1 for c in candidates if c["direction"] == "long")
    n_short = sum(1 for c in candidates if c["direction"] == "short")
    log(f"  regime={mode} quad={quad} · scanned {scanned} · {n_long} long"
        f"{f' / {n_short} short' if ALLOW_SHORTS else ''} pass (min {min_score:.2f})")

    opened = []
    slots = min(MAX_NEW_TRADES, MAX_POSITIONS - n_open)
    for c in candidates[:slots]:
        size = bankroll * POSITION_PCT
        if size > deployable:
            size = deployable
        if size < bankroll * 0.01:
            break
        shares = round(size / c["price"], 4)
        if shares <= 0:
            continue
        notional = round(shares * c["price"], 2)
        fees = round(notional * FEE_BPS, 2)
        is_short = c.get("direction") == "short"
        stop = round(c["price"] * (1.08 if is_short else 0.92), 4)     # short: stop above; long: below
        target = round(c["price"] * (0.84 if is_short else 1.16), 4)   # short: target below; long: above
        trade = {
            "id": f"{c['ticker']}-{today}-runner-{int(datetime.now(timezone.utc).timestamp())}",
            "ticker": c["ticker"], "name": c["name"], "sector": c["sector"],
            "direction": "short" if is_short else "long", "instrument": "shares",
            "style": "momentum", "horizonType": "swing", "horizonDays": 21,
            "entryDate": today, "exitDate": None,
            "entryPrice": c["price"], "exitPrice": None,
            "shares": shares, "notional": notional, "dollars": notional,
            "allocationPct": round(POSITION_PCT * 100, 2), "leverage": 1, "hedge": False,
            "stopPrice": stop, "targetPrice": target,
            "fees": fees, "realizedPL": 0, "pnl": 0, "returnPct": 0, "won": False,
            "exitReason": "open", "status": "open",
            "conviction": round(c["score"], 3), "confidence": round(c["score"], 3),
            "components": {k: round(v, 3) for k, v in c["components"].items()},
            "rationale": [f"Runner score {c['score']:.2f}: " +
                          ", ".join(f"{k} {v:+.2f}" for k, v in c["components"].items())],
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

    closed = manage_open_positions(state, universe, today)
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
