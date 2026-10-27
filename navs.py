#!/usr/bin/env python3
"""
Ledger fund prices — mutual funds (e.g. SLMCX) that Alpaca has no data for.

Mutual funds price once a day (their NAV, published in the evening). This reads
the holdings from the private Gist, fetches each fund's latest NAVs and five
years of daily history from Yahoo Finance, and saves them to navs.json in the
same Gist. The app and the nightly report use navs.json for any holding that
has no live quote.

A holding counts as a fund if its symbol is five letters ending in X (the US
mutual-fund convention) or Alpaca returns no snapshot for it.

Runs on its own (workflow "Ledger fund prices") and at the start of daily.py.
Required secrets:  GIST_TOKEN, GIST_ID
Optional secrets:  ALPACA_KEY, ALPACA_SECRET (to spot funds that don't end in X)
"""
import json, os, re, sys, time, urllib.request, urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
GH = "https://api.github.com"
UA = {"User-Agent": "Mozilla/5.0 (compatible; ledger-navs)"}


def http(url, headers=None, data=None, method=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def is_fund_symbol(s): return bool(re.fullmatch(r"[A-Z]{4}X", s))


def alpaca_missing(syms):
    key, sec = os.environ.get("ALPACA_KEY"), os.environ.get("ALPACA_SECRET")
    if not key or not sec or not syms: return []
    try:
        snap = json.loads(http("https://data.alpaca.markets/v2/stocks/snapshots?symbols=" + ",".join(syms) + "&feed=iex",
                               {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}))
    except Exception as e:
        print("alpaca check skipped:", e); return []
    return [s for s in syms if not snap.get(s)]


def yahoo(sym):
    """Latest NAV, the two before it, and ~5 years of dividend-adjusted daily closes."""
    j = None
    for host in ("query1", "query2"):
        try:
            j = json.loads(http(f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}?range=5y&interval=1d&events=div", UA)); break
        except Exception as e:
            print(f"{sym}: {host} failed: {e}"); time.sleep(2)
    res = ((j or {}).get("chart") or {}).get("result") or []
    if not res: return None
    r = res[0]; meta = r.get("meta") or {}
    ts = r.get("timestamp") or []
    ind = r.get("indicators") or {}
    close = ((ind.get("quote") or [{}])[0] or {}).get("close") or []
    adj = ((ind.get("adjclose") or [{}])[0] or {}).get("adjclose") or close
    pts = [(datetime.fromtimestamp(t, ET).strftime("%Y-%m-%d"), c, a) for t, c, a in zip(ts, close, adj) if c and a]
    if not pts: return None
    # Yahoo sometimes repeats today's row; keep the last value per date
    byday = {}
    for d, c, a in pts: byday[d] = (c, a)
    # the newest NAV can show up in meta before it lands in the daily series
    mp, mt = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if mp and mt:
        md = datetime.fromtimestamp(mt, ET).strftime("%Y-%m-%d")
        if md > max(byday): byday[md] = (mp, mp * byday[max(byday)][1] / byday[max(byday)][0])
    days = sorted(byday)
    c = [byday[d][0] for d in days]
    return {"c": round(c[-1], 4), "d": days[-1],
            "pc": round(c[-2], 4) if len(c) > 1 else None, "ppc": round(c[-3], 4) if len(c) > 2 else None,
            "name": meta.get("longName") or meta.get("shortName") or "",
            "hist": {"d": days, "c": [round(byday[d][1], 4) for d in days]},
            "at": int(time.time())}


def run(gist=None, tok=None, gid=None):
    tok = tok or os.environ.get("GIST_TOKEN"); gid = gid or os.environ.get("GIST_ID")
    if not tok or not gid: sys.exit("missing GIST_TOKEN / GIST_ID")
    h = {"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json"}
    if gist is None: gist = json.loads(http(f"{GH}/gists/{gid}", h))

    def content(name):
        f = gist["files"].get(name)
        if not f: return None
        return json.loads(f["content"] if not f.get("truncated") else http(f["raw_url"], h).decode())

    S = content("ledger.json") or {}
    old = content("navs.json") or {}
    syms = [x["t"] for x in S.get("holdings", [])] + [x["t"] for x in S.get("sold", [])]
    syms = list(dict.fromkeys(s for s in syms if s))
    funds = [s for s in syms if is_fund_symbol(s) or s in old]
    funds += [s for s in alpaca_missing([s for s in syms if s not in funds]) if s not in funds]
    if not funds:
        print("no funds to price"); return old

    navs, changed = {}, False
    for s in funds:
        n = yahoo(s)
        if n:
            navs[s] = n; changed = changed or (old.get(s) or {}).get("d") != n["d"] or (old.get(s) or {}).get("c") != n["c"]
            print(f"{s}: {n['c']} on {n['d']} (prev {n['pc']}) {n['name']}")
        elif s in old:
            navs[s] = old[s]; print(f"{s}: no data, keeping {old[s].get('d')}")
        else:
            print(f"{s}: no data")
    if changed or set(navs) != set(old):
        body = json.dumps({"files": {"navs.json": {"content": json.dumps(navs, separators=(",", ":"))}}}).encode()
        http(f"{GH}/gists/{gid}", {**h, "Content-Type": "application/json"}, body, "PATCH")
        print("navs.json saved")
    else:
        print("navs.json unchanged")
    return navs


if __name__ == "__main__":
    run()
