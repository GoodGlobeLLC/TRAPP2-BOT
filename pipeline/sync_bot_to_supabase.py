#!/usr/bin/env python3
"""
sync_bot_to_supabase.py — push bot_training_data.json → Supabase bot_trades.

Server-side (GitHub Actions). Uses the SERVICE ROLE key. The bot file's `trades`
array IS the transaction history; each trade row is upserted by id so the
`bot_trades` table (and the bot_trades_closed / bot_signal_performance views)
stay current without opening the app.

Env (repo secrets):
  SUPABASE_URL          the BOT project URL
  SUPABASE_SERVICE_KEY  the service_role key
"""
import json, os, sys, urllib.request, urllib.error
from datetime import datetime, timezone

URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
TABLE = "bot_trades"
DATA = "data/bot_training_data.json"

def _req(method, path, body=None, headers=None):
    h = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    if headers: h.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

def main():
    if not URL or not KEY:
        print("✗ SUPABASE_URL / SUPABASE_SERVICE_KEY not set"); return 1
    if not os.path.exists(DATA):
        print(f"No {DATA} — nothing to sync."); return 0
    d = json.load(open(DATA))
    # Accept both the standalone training export and an XTRAPP-style dump.
    trades = d.get("trades")
    if trades is None and isinstance(d.get("bot"), dict):
        trades = d["bot"].get("bets")
    trades = trades or []
    now = datetime.now(timezone.utc).isoformat()

    rows = []
    for i, t in enumerate(trades):
        tid = t.get("id") or f"{t.get('ticker','TX')}-{t.get('entryDate','')}-{i}"
        rows.append({"id": tid, "trade": t, "updated_at": now})

    sent = 0
    for i in range(0, len(rows), 100):
        chunk = rows[i:i+100]
        st, body = _req("POST", f"/rest/v1/{TABLE}?on_conflict=id", chunk,
                        {"Prefer": "resolution=merge-duplicates,return=minimal"})
        if st in (200, 201, 204): sent += len(chunk)
        else: print(f"  upsert chunk {i} → HTTP {st}: {body[:200]}")
    print(f"✓ bot → Supabase: upserted {sent} trade(s)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
