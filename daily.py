#!/usr/bin/env python3
"""
Ledger nightly report — runs on a GitHub Actions schedule at 8 PM Eastern.

Reads the portfolio from the private Gist, fetches today's closes and any
after-hours prices from Alpaca, and pushes a written summary to your phone
through ntfy. If ANTHROPIC_API_KEY is set, the summary is written by Claude
from the day's facts; otherwise it is assembled from a template.

Required secrets:  GIST_TOKEN, GIST_ID, ALPACA_KEY, ALPACA_SECRET, NTFY_TOPIC
Optional secrets:  ANTHROPIC_API_KEY
Optional vars:     CLAUDE_MODEL (default claude-sonnet-5), NTFY_SERVER
"""
import json, os, sys, urllib.request, urllib.error
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
GH = "https://api.github.com"
HOLIDAYS = {"2026-01-01","2026-01-19","2026-02-16","2026-04-03","2026-05-25","2026-06-19","2026-07-03","2026-09-07","2026-11-26","2026-12-25",
            "2027-01-01","2027-01-18","2027-02-15","2027-03-26","2027-05-31","2027-06-18","2027-07-05","2027-09-06","2027-11-25","2027-12-24"}


def env(name, default=None):
    v = os.environ.get(name, default)
    if v is None or v == "":
        sys.exit(f"missing environment variable {name}")
    return v


def http(url, headers=None, data=None, method=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def money(n): return ("-" if n < 0 else "") + "${:,.2f}".format(abs(n))
def money0(n): return ("-" if n < 0 else "") + "${:,.0f}".format(abs(n))
def signed(n): return ("+" if n >= 0 else "-") + "${:,.2f}".format(abs(n))
def pct(n): return ("+" if n >= 0 else "-") + "{:.2f}%".format(abs(n))


now = datetime.now(ET)
today = now.strftime("%Y-%m-%d")
force = os.environ.get("FORCE") == "1"
if not force and (now.weekday() >= 5 or today in HOLIDAYS):
    print("market was closed today; no report"); sys.exit(0)
if not force and now.hour < 20:
    print(f"{now:%H:%M} ET — before the 8 PM reporting window"); sys.exit(0)

# ---------------------------------------------------------------- gist
tok = env("GIST_TOKEN"); gid = env("GIST_ID")
gh_headers = {"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json"}
gist = json.loads(http(f"{GH}/gists/{gid}", gh_headers))
f = gist["files"].get("ledger.json") or sys.exit("Gist has no ledger.json")
S = json.loads(f["content"] if not f.get("truncated") else http(f["raw_url"], gh_headers).decode())
if not force and S.get("lastReport") == today:
    print("report already sent today"); sys.exit(0)
holdings = S.get("holdings", []); cash = float(S.get("cash", 0)); log = sorted(S.get("log", []), key=lambda p: p["d"])
if not holdings:
    print("no holdings"); sys.exit(0)

# ---------------------------------------------------------------- prices
a_headers = {"APCA-API-KEY-ID": env("ALPACA_KEY"), "APCA-API-SECRET-KEY": env("ALPACA_SECRET")}
syms = ",".join(h["t"] for h in holdings)


def snapshots(feed):
    try:
        return json.loads(http(f"https://data.alpaca.markets/v2/stocks/snapshots?symbols={syms}&feed={feed}", a_headers))
    except urllib.error.HTTPError as e:
        print(f"alpaca {feed}: HTTP {e.code}"); return {}


sip = snapshots("delayed_sip"); iex = snapshots("iex")


def et_date(iso): return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ET).strftime("%Y-%m-%d")


rows = []
for h in holdings:
    s = h["t"]; sh = sum(l["q"] for l in h["lots"]); cb = sum(l["q"] * l["p"] for l in h["lots"])
    x = sip.get(s) or iex.get(s) or {}
    db, pdb = x.get("dailyBar"), x.get("prevDailyBar")
    if db and et_date(db["t"]) == today:
        close, prev = db["c"], (pdb["c"] if pdb else db["o"])
    elif db:
        close, prev = db["c"], (pdb["c"] if pdb else db["c"])
    else:
        continue
    # after-hours: freshest trade after 4 PM from either feed
    ah = None
    for src in (sip.get(s), iex.get(s)):
        lt = (src or {}).get("latestTrade")
        if not lt: continue
        t = datetime.fromisoformat(lt["t"].replace("Z", "+00:00")).astimezone(ET)
        if t.strftime("%Y-%m-%d") == today and t.hour * 60 + t.minute > 960 and (ah is None or t > ah["t"]):
            ah = {"p": lt["p"], "t": t}
    rows.append({"t": s, "name": h.get("name") or s, "sh": sh, "basis": cb, "close": close, "prev": prev,
                 "chg": close - prev, "pct": (close - prev) / prev * 100 if prev else 0,
                 "value": close * sh, "day": (close - prev) * sh, "gain": close * sh - cb,
                 "ah": ah["p"] if ah else None, "ah_pct": (ah["p"] - close) / close * 100 if ah else None,
                 "ah_when": ah["t"].strftime("%-I:%M %p") if ah else None,
                 "bought_today": [l for l in h["lots"] if l.get("d") == today]})

inv = sum(r["value"] for r in rows); cost = sum(r["basis"] for r in rows); day = sum(r["day"] for r in rows)
total = inv + cash; prev_total = total - day
ah_total = sum((r["ah"] if r["ah"] else r["close"]) * r["sh"] for r in rows) + cash

# week / month to date from the app's own value log
def value_on_or_before(d):
    pts = [p for p in log if p["d"] <= d]
    return pts[-1]["v"] if pts else None


wk_start = (now - timedelta(days=now.weekday() + 1)).strftime("%Y-%m-%d")   # last Sunday
mo_start = now.replace(day=1).strftime("%Y-%m-%d")
v_wk = value_on_or_before(wk_start); v_mo = value_on_or_before((now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m-%d"))

fired_today = [a for a in S.get("alerts", []) if (a.get("firedD") == today) or (a.get("last", "").startswith(now.strftime("%b %-d,")))]
sold_today = [x for x in S.get("sold", []) if x.get("d") == today]

facts = {
    "date": now.strftime("%A, %B %-d"),
    "account_total": money(total), "day_change": signed(day), "day_pct": pct(day / prev_total * 100 if prev_total else 0),
    "total_gain": signed(inv - cost), "total_gain_pct": pct((inv - cost) / cost * 100 if cost else 0), "cash": money(cash),
    "week_to_date": signed(total - v_wk) if v_wk else None, "month_to_date": signed(total - v_mo) if v_mo else None,
    "holdings": [{"symbol": r["t"], "name": r["name"], "close": money(r["close"]), "day_change": signed(r["chg"]) + " (" + pct(r["pct"]) + ")",
                  "position_day_change": signed(r["day"]), "position_value": money(r["value"]), "weight": f"{r['value'] / total * 100:.0f}%",
                  "total_gain": signed(r["gain"]),
                  "after_hours": (money(r["ah"]) + " (" + pct(r["ah_pct"]) + ") as of " + r["ah_when"]) if r["ah"] else "no after-hours trades reported",
                  "bought_today": [f"{l['q']:.3f} shares at {money(l['p'])}" for l in r["bought_today"]]} for r in rows],
    "after_hours_account": money(ah_total) if any(r["ah"] for r in rows) else None,
    "sold_today": [f"{x['q']:.3f} {x['t']} at {money(x['p'])}, realized {signed(x['gain'])}" for x in sold_today],
    "alerts_fired_today": [f"{a.get('s', 'Account')} {a['t']} {a['v']}" for a in fired_today],
}

# ---------------------------------------------------------------- narrative
def template(F):
    L = [f"Close  {F['account_total']}   {F['day_change']} ({F['day_pct']})"]
    if F["after_hours_account"]:
        L.append(f"After hours  {F['after_hours_account']}")
    L.append("")
    for r in rows:
        line = f"{r['t']:<5} {money(r['close'])}  {pct(r['pct'])}  {signed(r['day'])}"
        if r["ah"]:
            line += f"\n      after hrs {money(r['ah'])} ({pct(r['ah_pct'])})"
        L.append(line)
    buys = [f"{r['t']} {l}" for r in rows for l in F['holdings'][rows.index(r)]['bought_today']]
    if buys or F["sold_today"] or F["alerts_fired_today"]:
        L.append("")
        for x in buys: L.append("Bought  " + x)
        for x in F["sold_today"]: L.append("Sold  " + x)
        if F["alerts_fired_today"]: L.append(f"Alerts fired  {len(F['alerts_fired_today'])}")
    L.append("")
    if F["week_to_date"]: L.append(f"Week   {F['week_to_date']}")
    if F["month_to_date"]: L.append(f"Month  {F['month_to_date']}")
    L.append(f"Total gain  {F['total_gain']} ({F['total_gain_pct']})")
    L.append(f"Cash  {F['cash']}")
    return "\n".join(L)


def claude_narrative(F):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key: return None
    model = os.environ.get("CLAUDE_MODEL") or "claude-sonnet-5"
    prompt = ("You write the end-of-day phone notification for the owner of a small personal stock portfolio. "
              "Using only the facts below, produce PLAIN TEXT (no markdown, no bullets, no emojis) laid out as short lines, in this order:\n"
              "1. One line: what the account did today (closing total, dollar and percent change).\n"
              "2. One line per holding: symbol, close, day change, what it did to the position; add after-hours if any traded.\n"
              "3. If anything was bought or sold, one line each.\n"
              "4. One line: where the week and month stand, and total gain.\n"
              "5. One closing sentence that names what drove the day, in a calm matter-of-fact voice.\n"
              "Keep every line under 70 characters if possible. Put a blank line between sections. "
              "No advice, predictions, or commentary on what they should do. Use the figures exactly as given.\n\nFACTS:\n" + json.dumps(F, indent=1))
    body = json.dumps({"model": model, "max_tokens": 500, "messages": [{"role": "user", "content": prompt}]}).encode()
    try:
        r = json.loads(http("https://api.anthropic.com/v1/messages", {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}, body, "POST"))
        text = "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text").strip()
        return text or None
    except Exception as e:
        print("claude narrative failed, using template:", e); return None


text = claude_narrative(facts) or template(facts)
print(text)

# ---------------------------------------------------------------- push
topic = env("NTFY_TOPIC"); server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
title = f"Ledger · {facts['account_total']} ({facts['day_pct']})"
http(f"{server}/{topic}", {"Title": title, "Priority": "default", "Tags": "moneybag", "Markdown": "no"}, text.encode("utf-8"), "POST")
print("sent")
if not force:  # remember so a second (late) scheduled run doesn't repeat it
    S["lastReport"] = today
    body = json.dumps({"files": {"ledger.json": {"content": json.dumps(S, indent=1)}}}).encode()
    try:
        http(f"{GH}/gists/{gid}", {**gh_headers, "Content-Type": "application/json"}, body, "PATCH")
    except Exception as e:
        print("could not record lastReport:", e)
