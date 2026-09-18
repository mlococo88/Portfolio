#!/usr/bin/env python3
"""
Ledger alert checker — runs on a GitHub Actions schedule.

Reads holdings + alert rules from the private Gist the app syncs to,
fetches quotes from Alpaca, and pushes a notification through ntfy.sh
when a rule trips. Fired alerts are written back to the Gist so the
app shows them as fired and they don't repeat until re-armed.

Required environment variables (set as GitHub repository secrets):
  GIST_TOKEN     GitHub token with Gist read/write (same one the app uses)
  GIST_ID        the Gist ID shown in the app's Settings
  ALPACA_KEY     Alpaca key ID
  ALPACA_SECRET  Alpaca secret key
  NTFY_TOPIC     your private ntfy topic name
Optional:
  NTFY_SERVER    default https://ntfy.sh
  TEST           "1" sends a test notification and exits
  CLOSE_SUMMARY  "1" to also send a daily summary just after the 4 PM close
"""
import json, os, sys, urllib.request, urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
GH = "https://api.github.com"


def env(name, default=None):
    v = os.environ.get(name, default)
    if v is None or v == "":
        sys.exit(f"missing environment variable {name}")
    return v


def http(url, headers=None, data=None, method=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def money(n):
    return ("-" if n < 0 else "") + "${:,.2f}".format(abs(n))


def signed(n):
    return ("+" if n >= 0 else "-") + "${:,.2f}".format(abs(n))


def pct(n):
    return ("+" if n >= 0 else "-") + "{:.2f}%".format(abs(n))


# ---------------------------------------------------------------- test mode
now = datetime.now(ET)
if os.environ.get("TEST") == "1":
    topic = env("NTFY_TOPIC"); server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    http(f"{server}/{topic}", {"Title": "Ledger", "Priority": "high", "Tags": "white_check_mark"},
         f"Test notification sent {now:%-I:%M %p} ET — alerts are wired up.".encode(), "POST")
    print("test notification sent to topic", topic)
    sys.exit(0)

# ---------------------------------------------------------------- market hours
minutes = now.hour * 60 + now.minute
weekday = now.weekday() < 5
in_session = weekday and 240 <= minutes < 1200  # 4:00 AM – 8:00 PM ET
if not in_session and os.environ.get("FORCE") != "1":
    print(f"{now:%a %H:%M} ET — outside 4 AM–8 PM, nothing to do")
    sys.exit(0)

# ---------------------------------------------------------------- read gist
tok = env("GIST_TOKEN"); gid = env("GIST_ID")
gh_headers = {"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json"}
gist = json.loads(http(f"{GH}/gists/{gid}", gh_headers))
f = gist["files"].get("ledger.json")
if not f:
    sys.exit("Gist has no ledger.json — push from the app first")
content = f["content"] if not f.get("truncated") else http(f["raw_url"], gh_headers).decode()
S = json.loads(content)
holdings = S.get("holdings", [])
alerts = S.get("alerts", [])
cash = float(S.get("cash", 0))
pending = [a for a in alerts if a.get("t", "").startswith("step") or not a.get("fired")]
want_summary = os.environ.get("CLOSE_SUMMARY") == "1" and 960 <= minutes < 1020 and S.get("lastSummary") != now.strftime("%Y-%m-%d")
if not pending and not want_summary:
    print("no pending alerts; nothing to check")
    sys.exit(0)
if not holdings:
    print("no holdings in gist")
    sys.exit(0)

# ---------------------------------------------------------------- quotes
ak = env("ALPACA_KEY"); asec = env("ALPACA_SECRET")
a_headers = {"APCA-API-KEY-ID": ak, "APCA-API-SECRET-KEY": asec}
syms = ",".join(h["t"] for h in holdings)
open_session = 570 <= minutes < 960


def snapshots(feed):
    try:
        return json.loads(http(f"https://data.alpaca.markets/v2/stocks/snapshots?symbols={syms}&feed={feed}", a_headers))
    except urllib.error.HTTPError as e:
        print(f"alpaca {feed}: HTTP {e.code}")
        return {}


iex = snapshots("iex")
sip = {} if open_session else snapshots("delayed_sip")
today = now.strftime("%Y-%m-%d")
Q = {}
for h in holdings:
    s = h["t"]
    best = None
    for src in (iex.get(s), sip.get(s)):
        if not src or not src.get("latestTrade"):
            continue
        lt = src["latestTrade"]
        t = datetime.fromisoformat(lt["t"].replace("Z", "+00:00"))
        db, pdb = src.get("dailyBar"), src.get("prevDailyBar")
        if db and datetime.fromisoformat(db["t"].replace("Z", "+00:00")).astimezone(ET).strftime("%Y-%m-%d") == today:
            pc = pdb["c"] if pdb else lt["p"]
        elif db:
            pc = db["c"]
        else:
            pc = pdb["c"] if pdb else lt["p"]
        cand = {"c": lt["p"], "pc": pc, "t": t}
        if best is None or cand["t"] > best["t"]:
            best = cand
    if best:
        best["d"] = best["c"] - best["pc"]
        best["dp"] = best["d"] / best["pc"] * 100 if best["pc"] else 0
        Q[s] = best

# ---------------------------------------------------------------- valuation
inv = cost = day = 0.0
for h in holdings:
    sh = sum(l["q"] for l in h["lots"]); cb = sum(l["q"] * l["p"] for l in h["lots"])
    q = Q.get(h["t"])
    inv += (q["c"] * sh) if q else cb
    cost += cb
    if q:
        day += q["d"] * sh
total = inv + cash
print(f"{now:%a %H:%M} ET — account {money(total)}, day {signed(day)}")

# ---------------------------------------------------------------- evaluate
messages = []
anchored = False
stamp = now.strftime("%b %-d, %-I:%M %p")
for a in pending:
    t = a.get("t"); v = float(a.get("v", 0)); s = a.get("s")
    hit = None
    if t in ("step_usd", "step_pct"):
        q = Q.get(s)
        if not q:
            continue
        if a.get("anchor") is None:
            a["anchor"] = q["c"]; anchored = True
            continue
        mv = q["c"] - a["anchor"]
        ok = abs(mv) >= v if t == "step_usd" else abs(mv) / a["anchor"] * 100 >= v
        if ok:
            amt = money(abs(mv)) if t == "step_usd" else f"{abs(mv) / a['anchor'] * 100:.2f}%"
            hit = f"{s} {'up' if mv > 0 else 'down'} {amt} to {money(q['c'])} (from {money(a['anchor'])})"
            a["anchor"] = q["c"]; a["last"] = stamp; a["count"] = a.get("count", 0) + 1
    elif t == "total_above" and total > v:
        hit = f"Account total is {money(total)}, above {money(v)}"
    elif t == "total_below" and total < v:
        hit = f"Account total is {money(total)}, below {money(v)}"
    elif s and s in Q:
        q = Q[s]
        if t == "above" and q["c"] > v:
            hit = f"{s} is {money(q['c'])}, above {money(v)}"
        elif t == "below" and q["c"] < v:
            hit = f"{s} is {money(q['c'])}, below {money(v)}"
        elif t == "daypct_up" and q["dp"] > v:
            hit = f"{s} is up {q['dp']:.2f}% today at {money(q['c'])}"
        elif t == "daypct_dn" and q["dp"] < -v:
            hit = f"{s} is down {-q['dp']:.2f}% today at {money(q['c'])}"
    if hit:
        if not t.startswith("step"):
            a["fired"] = stamp
        messages.append(hit)

if want_summary:
    base = total - day
    messages.append(f"Close: {money(total)} · today {signed(day)} ({pct(day / base * 100 if base else 0)}) · total gain {signed(inv - cost)}")
    S["lastSummary"] = today

if not messages and not anchored:
    print("no alerts tripped")
    sys.exit(0)

# ---------------------------------------------------------------- notify
topic = env("NTFY_TOPIC"); server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
if not messages:
    print("anchored new repeating alert(s)")
for m in messages:
    http(f"{server}/{topic}", {"Title": "Ledger", "Priority": "high", "Tags": "chart_with_upwards_trend"}, m.encode(), "POST")
    print("sent:", m)

# ---------------------------------------------------------------- write fired state back
S["updatedAt"] = int(now.timestamp() * 1000)
body = json.dumps({"files": {"ledger.json": {"content": json.dumps(S, indent=1)}}}).encode()
http(f"{GH}/gists/{gid}", {**gh_headers, "Content-Type": "application/json"}, body, "PATCH")
print("gist updated")
