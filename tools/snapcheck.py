#!/usr/bin/env python3
"""Diagnostic: print what Alpaca's snapshot endpoint returns for each holding (prices only)."""
import json, os, urllib.request
GH = "https://api.github.com"
def http(url, headers=None):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=60) as r: return r.read()
gh = {"Authorization": "Bearer " + os.environ["GIST_TOKEN"], "Accept": "application/vnd.github+json"}
g = json.loads(http(f"{GH}/gists/{os.environ['GIST_ID']}", gh))
S = json.loads(g["files"]["ledger.json"]["content"])
syms = [h["t"] for h in S["holdings"] if not (len(h["t"]) == 5 and h["t"].endswith("X"))]
a = {"APCA-API-KEY-ID": os.environ["ALPACA_KEY"], "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET"]}
for feed in ("iex", "delayed_sip"):
    try:
        snap = json.loads(http(f"https://data.alpaca.markets/v2/stocks/snapshots?symbols={','.join(syms)}&feed={feed}", a))
    except Exception as e:
        print(feed, "ERROR", e); continue
    print("=====", feed)
    for s in syms:
        x = snap.get(s) or {}
        pick = lambda k, f: {kk: (x.get(k) or {}).get(kk) for kk in f}
        print(s, json.dumps({"trade": pick("latestTrade", ("p", "t", "c", "x")), "quote": pick("latestQuote", ("bp", "ap", "t")),
                             "minute": pick("minuteBar", ("c", "t")), "daily": pick("dailyBar", ("o", "c", "t")), "prev": pick("prevDailyBar", ("c", "t"))}))
