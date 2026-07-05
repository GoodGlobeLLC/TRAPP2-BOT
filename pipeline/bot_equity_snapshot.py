#!/usr/bin/env python3
"""
bot_equity_snapshot.py — the bot's Robinhood-style equity tape.

Every run it values the WHOLE book at the freshest prices available and appends a
timestamped point to data/bot_equity_history.json (a price history, like a ticker),
then pushes the same point to Supabase. Run it on a 30-minute schedule (and/or
right after ticker prices are refreshed) so each point is a fresh mark.

TOTAL VALUE (reconciles exactly with the app's ledger + positions):
    value = STARTING_BANKROLL + realized + unrealized
      · realized   = bankroll - startingBankroll   (already booked by the runner)
      · unrealized = Σ open positions, per ticker on average cost:
                     shares × (live − avgCost) × direction
                     (options / leveraged ETFs use their stored non-linear P&L)
So  value − STARTING  ==  (ledger realized) + (positions unrealized)  in the app.

PRICES: live Yahoo quotes (best-effort, no key) for the held names, falling back
to the pipeline's master.json. As the price feed gets fresher, each 30-min point
moves — exactly the intraday shape you want.

Env (repo secrets): SUPABASE_URL + SUPABASE_SERVICE_ROLE (or _SERVICE_KEY) for the
BOT Supabase project. GitHub commit is done by the workflow.
"""
import json
import os
import sys
import urllib.request
import urllib.error
import re
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "data", "bot_training_data.json")
HIST = os.path.join(ROOT, "data", "bot_equity_history.json")
STARTING_DEFAULT = 100000.0
MAX_POINTS = 3000            # ~40 market days of 30-min points
BUCKET_MIN = 30             # snap timestamps to a 30-minute grid

RAW = "https://raw.githubusercontent.com/GoodGlobeLLC"
MASTER = [f"{RAW}/TRAPP2/main/data/master.json",
          f"{RAW}/TRAPP2-2/main/data/master.json",
          f"{RAW}/TRAPP2-1/main/data/master.json"]

URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
KEY = (os.environ.get("SUPABASE_SERVICE_ROLE") or os.environ.get("SUPABASE_SERVICE_KEY")
       or os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_ANON_KEY") or "")


# ---------------- timezone (Eastern wall-clock tagged +00:00) ----------------
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None


def _eastern_now():
    if _ET is not None:
        return datetime.now(_ET)
    u = datetime.now(timezone.utc); y = u.year

    def nth_sun(m, n):
        d = datetime(y, m, 1, tzinfo=timezone.utc)
        return 1 + ((6 - d.weekday()) % 7) + (n - 1) * 7
    start = datetime(y, 3, nth_sun(3, 2), 7, tzinfo=timezone.utc)
    end = datetime(y, 11, nth_sun(11, 1), 6, tzinfo=timezone.utc)
    return u + timedelta(hours=(-4 if start <= u < end else -5))


def now_iso():
    return _eastern_now().replace(tzinfo=timezone.utc).isoformat()


def bucket_iso():
    """Eastern wall-clock snapped down to the 30-min grid, tagged +00:00."""
    et = _eastern_now()
    snapped = et.replace(minute=(et.minute // BUCKET_MIN) * BUCKET_MIN, second=0, microsecond=0)
    return snapped.replace(tzinfo=timezone.utc).isoformat()


# ---------------------------------- io ---------------------------------------
def _num(v):
    try:
        f = float(v)
        return None if (f != f or f in (float("inf"), float("-inf"))) else f
    except (TypeError, ValueError):
        return None


def fetch_json(url, timeout=30):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "valuatio-bot-equity"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


FX_URL = f"{RAW}/TRAPP2-1/main/data/fx/rates.json"


def base_price_map():
    """{ticker: {price(local), currency}} — currency from master.json so we can
    convert foreign quotes (KRW/CNY/JPY/…) to USD."""
    px = {}
    for u in MASTER:
        d = fetch_json(u)
        rows = d.values() if isinstance(d, dict) else (d if isinstance(d, list) else [])
        for r in rows:
            if not isinstance(r, dict):
                continue
            t = (r.get("ticker") or r.get("symbol") or "").upper()
            p = _num(r.get("price")) or _num(r.get("fmpPrice")) or _num(r.get("close")) or _num(r.get("last"))
            if t and p is not None and t not in px:
                px[t] = {"price": p, "currency": (r.get("currency") or "USD")}
    return px


def load_fx():
    """USD-per-unit map from TRAPP2-1/data/fx/rates.json (same file the app uses)."""
    d = fetch_json(FX_URL)
    rates = (d.get("rates") if isinstance(d, dict) else {}) or {}
    out = {}
    for c, v in rates.items():
        up = _num(v.get("usdPer")) if isinstance(v, dict) else None
        if up:
            out[c.upper()] = up
    return out


def make_price_of(base, live, fx):
    """USD price for a ticker: live quote (or master) in its local currency,
    converted with fx. GBX/GBp (London pence) handled. Returns None if the
    currency has no loaded rate (so it's excluded rather than mis-valued)."""
    def price_of(tic):
        e = base.get(tic)
        ccy = ((e["currency"] if e else "USD") or "USD").upper()
        local = live.get(tic) if tic in live else (e["price"] if e else None)
        if local is None:
            return None
        pence = 1.0
        if ccy in ("GBX", "GBP", "GBP.", "ZAC", "ILA"):
            if ccy in ("GBX",):
                ccy, pence = "GBP", 0.01
        if ccy in ("GBP",) and False:
            pass
        if ccy == "USD":
            return local
        r = fx.get(ccy)
        return (local * pence * r) if r else None
    return price_of


def live_quotes(tickers):
    """Best-effort live prices from Yahoo's public quote endpoint (no key). Returns
    {ticker: price}; empty on any failure so the caller falls back to master.json."""
    out = {}
    syms = [t for t in tickers if t]
    for i in range(0, len(syms), 40):
        chunk = syms[i:i + 40]
        u = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=" + ",".join(chunk)
        j = fetch_json(u, timeout=20)
        try:
            for q in (j.get("quoteResponse", {}).get("result", []) if isinstance(j, dict) else []):
                sym = (q.get("symbol") or "").upper()
                p = _num(q.get("regularMarketPrice")) or _num(q.get("postMarketPrice")) or _num(q.get("preMarketPrice"))
                if sym and p is not None:
                    out[sym] = p
        except Exception:
            pass
    return out


# ------------------------------- valuation -----------------------------------
def _is_foreign(tic):
    # Non-US listings (e.g. .KS/.SZ/.HK/.L/.T) quote in LOCAL currency in master.json,
    # so their marks aren't USD-comparable to a USD entry. US class shares (.A/.B/.C)
    # are fine. We exclude foreign names (and any absurd mark) from the book until FX
    # conversion exists, rather than let a ₩/¥ price blow up the total.
    if "." not in tic:
        return False
    return tic.rsplit(".", 1)[1].upper() not in ("A", "B", "C")


def _sane_mark(px, cost):
    if px is None or cost is None or cost <= 0:
        return False
    r = px / cost
    return 0.1 <= r <= 10          # a >10x/<0.1x move vs cost ⇒ currency/data error


def value_book(state, price_of):
    """Return (value, realized, unrealized, open_count, priced) — the whole book
    marked to `price_of`, reconciling with the app's ledger + positions."""
    starting = _num(state.get("startingBankroll")) or STARTING_DEFAULT
    bankroll = _num(state.get("bankroll"))
    if bankroll is None:
        bankroll = starting
    realized = bankroll - starting

    # Aggregate OPEN trades per ticker on average cost.
    pos = {}
    for t in (state.get("trades") or []):
        if not isinstance(t, dict) or t.get("status") != "open":
            continue
        tic = (t.get("ticker") or "").upper()
        sh = _num(t.get("shares"))
        ep = _num(t.get("entryPrice"))
        if not tic or sh is None or sh <= 0 or ep is None:
            continue
        d = -1.0 if t.get("direction") == "short" else 1.0
        nonlin = t.get("instrument") in ("option", "leveraged_etf")
        p = pos.setdefault(tic, {"shares": 0.0, "cost": 0.0, "dir": d, "nonlin": False, "pnl": 0.0})
        p["shares"] += sh
        p["cost"] += sh * ep
        p["dir"] = d
        if nonlin:
            p["nonlin"] = True
            p["pnl"] += _num(t.get("pnl")) or 0.0

    unreal = 0.0
    priced = 0
    excluded = 0
    for tic, p in pos.items():
        if p["nonlin"]:
            unreal += p["pnl"]; priced += 1; continue
        avg = p["cost"] / p["shares"] if p["shares"] else 0.0
        px = price_of(tic)          # USD (FX-normalized)
        if not _sane_mark(px, avg):
            excluded += 1              # no FX rate / absurd mark ⇒ can't value reliably
            continue
        unreal += p["shares"] * (px - avg) * p["dir"]
        priced += 1
    value = starting + realized + unreal
    return round(value, 2), round(realized, 2), round(unreal, 2), len(pos), priced, excluded


# ------------------------------- persistence ---------------------------------
def append_point(point):
    hist = []
    if os.path.exists(HIST):
        try:
            hist = json.load(open(HIST))
            if not isinstance(hist, list):
                hist = []
        except Exception:
            hist = []
    # One point per 30-min bucket: replace if the last shares this bucket, else append.
    if hist and hist[-1].get("t") == point["t"]:
        hist[-1] = point
    else:
        hist.append(point)
    if len(hist) > MAX_POINTS:
        hist = hist[-MAX_POINTS:]
    os.makedirs(os.path.dirname(HIST), exist_ok=True)
    json.dump(hist, open(HIST, "w"), separators=(",", ":"))
    return len(hist)


def push_supabase(point):
    if not URL or not KEY:
        print("  (no Supabase creds — skipped push)")
        return
    body = [{
        "t": point["t"], "value": point["value"], "cash": point["cash"],
        "unrealized": point["unrealized"], "realized": point["realized"],
        "open_count": point["openCount"], "priced": point["priced"],
        "source": point["source"], "updated_at": now_iso(),
    }]
    try:
        req = urllib.request.Request(
            URL + "/rest/v1/bot_equity?on_conflict=t",
            data=json.dumps(body).encode(),
            headers={"apikey": KEY, "Authorization": "Bearer " + KEY,
                     "Content-Type": "application/json",
                     "Prefer": "resolution=merge-duplicates,return=minimal"},
            method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"  Supabase bot_equity: HTTP {r.status}")
    except urllib.error.HTTPError as e:
        print(f"  Supabase bot_equity: HTTP {e.code} {e.read().decode()[:160]}")
    except Exception as e:
        print(f"  Supabase bot_equity: {e}")


def main():
    if not os.path.exists(STATE):
        print(f"No {STATE} — nothing to value."); return 0
    state = json.load(open(STATE))

    tickers = sorted({(t.get("ticker") or "").upper() for t in (state.get("trades") or [])
                      if isinstance(t, dict) and t.get("status") == "open" and t.get("ticker")})
    base = base_price_map()
    fx = load_fx()
    live = live_quotes(tickers) if tickers else {}
    source = "live+master" if live else "master"
    print(f"Valuing {len(tickers)} open tickers · live {len(live)} · master {len(base)} · fx {len(fx)} ccy")
    price_of = make_price_of(base, live, fx)

    value, realized, unreal, opens, priced, excluded = value_book(state, price_of)
    point = {
        "t": bucket_iso(), "value": value, "cash": round((_num(state.get("bankroll")) or STARTING_DEFAULT), 2),
        "realized": realized, "unrealized": unreal, "openCount": opens, "priced": priced, "source": source,
    }
    n = append_point(point)
    print(f"✓ {point['t']} · value ${value:,.2f} (realized {realized:+,.2f}, unrealized {unreal:+,.2f}) · "
          f"{priced}/{opens} priced ({excluded} excluded: foreign/anomaly) · history {n} pts")
    push_supabase(point)
    return 0


if __name__ == "__main__":
    sys.exit(main())
