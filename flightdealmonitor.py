"""FlightDealMonitor

Two modes, both driven by routes.json:

  python flightdealmonitor.py scan --route ATLGUA
      Scans every departure day in the rolling window for one route,
      pulling live prices from Google Flights (every carrier Google lists).
      Writes results/ATLGUA.json.

  python flightdealmonitor.py report
      Merges results/*.json, compares against .state/lows.json, scans the
      deal publishers, emails when there is something worth seeing, and
      rewrites the state file.

Environment variables (GitHub secrets): GMAILUSER, GMAILAPPPASSWORD, ALERTRECIPIENT
"""

import argparse
import datetime as dt
import html
import json
import os
import random
import re
import smtplib
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from fast_flights import FlightQuery, Passengers, create_query, fetch_flights_html
from selectolax.lexbor import LexborHTMLParser

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "routes.json").read_text())
RESULTSDIR = ROOT / "results"
STATEFILE = ROOT / ".state" / "lows.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"


# ---------------------------------------------------------------- scanning

def fetchpair(origin, dest, depart, ret):
    """One Google Flights round-trip lookup.  Returns dict or None if no fares."""
    q = create_query(
        flights=[
            FlightQuery(date=depart, from_airport=origin, to_airport=dest),
            FlightQuery(date=ret, from_airport=dest, to_airport=origin),
        ],
        trip="round-trip",
        passengers=Passengers(adults=CONFIG["passengers"]),
        currency=CONFIG["currency"],
    )
    lasterr = None
    for attempt in range(3):
        try:
            page = LexborHTMLParser(fetch_flights_html(q))
            node = page.css_first(r"script.ds\:1")
            if node is None:
                raise RuntimeError("no data script in page")
            payload = json.loads(node.text().split("data:", 1)[1].rsplit(",", 1)[0])
            break
        except Exception as exc:  # noqa: BLE001
            lasterr = exc
            time.sleep(3 * (attempt + 1))
    else:
        raise RuntimeError(f"{origin}-{dest} {depart}/{ret}: {lasterr}")

    insights = payload[5]
    if not insights or not insights[1] or insights[1][1] is None:
        return None

    # cheapest listed itinerary, for airline / stops / duration detail
    best = None
    for section in (payload[2], payload[3]):
        if not section or not section[0]:
            continue
        for row in section[0]:
            price = row[1][0][1]
            legs = row[0][2]
            if best is None or price < best["price"]:
                best = {
                    "price": price,
                    "airlines": row[0][1],
                    "stops": len(legs) - 1,
                    "minutes": row[0][9] if isinstance(row[0][9], int) else sum(l[11] or 0 for l in legs),
                }

    return {
        "depart": depart,
        "return": ret,
        "price": insights[1][1],
        "typical": insights[2][1] if insights[2] else None,
        "typicallow": insights[4][1] if insights[4] else None,
        "typicalhigh": insights[5][1] if insights[5] else None,
        "airlines": best["airlines"] if best else [],
        "stops": best["stops"] if best else None,
        "minutes": best["minutes"] if best else None,
        "url": q.url(),
    }


def scanroute(key):
    route = next(r for r in CONFIG["routes"] if r["key"] == key)
    today = dt.date.today()
    start = today + dt.timedelta(days=CONFIG["windowstartdays"])
    end = today + dt.timedelta(days=CONFIG["windowenddays"])

    pairs, failures, total = [], 0, 0
    day = start
    while day <= end:
        for length in route["triplengths"]:
            total += 1
            ret = day + dt.timedelta(days=length)
            try:
                hit = fetchpair(route["from"], route["to"], day.isoformat(), ret.isoformat())
                if hit:
                    hit["nights"] = length
                    pairs.append(hit)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"WARN {exc}", file=sys.stderr)
            time.sleep(random.uniform(0.5, 1.0))
        day += dt.timedelta(days=1)

    if total and failures / total > 0.4:
        raise SystemExit(f"{key}: {failures}/{total} lookups failed, refusing to report on bad data")

    RESULTSDIR.mkdir(exist_ok=True)
    (RESULTSDIR / f"{key}.json").write_text(json.dumps(
        {"key": key, "scanned": today.isoformat(), "lookups": total, "failures": failures, "pairs": pairs},
        indent=1))
    print(f"{key}: {len(pairs)} priced date pairs from {total} lookups ({failures} failed)")


# ------------------------------------------------------------ deal feeds

def scandeals():
    """Return list of (feedname, title, link) whose title matches a keyword."""
    hits = []
    origins = [k.lower() for k in CONFIG["dealoriginkeywords"]]
    dests = [k.lower() for k in CONFIG["dealdestinationkeywords"]]
    for feed in CONFIG["dealfeeds"]:
        try:
            text = requests.get(feed["url"], headers={"User-Agent": UA}, timeout=30).text
        except Exception as exc:  # noqa: BLE001
            print(f"WARN feed {feed['name']}: {exc}", file=sys.stderr)
            continue
        items = []
        if feed["type"] == "rss":
            for block in re.findall(r"<item>(.*?)</item>", text, re.S):
                t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", block, re.S)
                l = re.search(r"<link>(.*?)</link>", block, re.S)
                if t:
                    items.append((html.unescape(t.group(1).strip()), l.group(1).strip() if l else feed["url"]))
        elif feed["type"] == "travelzoo":
            for h in re.findall(r"<h[23][^>]*>(.*?)</h[23]>", text, re.S):
                clean = " ".join(html.unescape(re.sub("<[^>]+>", "", h)).split())
                if clean and clean[0] in "$S0123456789":
                    items.append((clean, feed["url"]))
        for title, link in items:
            low = title.lower()
            # airfare feeds list deals by departure city, so they must name Atlanta;
            # Travelzoo packages are nationwide, so a destination match is enough
            if any(k in low for k in origins) or (feed["type"] == "travelzoo" and any(k in low for k in dests)):
                hits.append((feed["name"], title, link))
    return hits


# --------------------------------------------------------------- report

def loadstate():
    if STATEFILE.exists():
        return json.loads(STATEFILE.read_text())
    return {"lows": {}, "seendeals": []}


def buildreport():
    state = loadstate()
    lows = state["lows"]
    today = dt.date.today()
    weekly = today.strftime("%A") == CONFIG["weeklydigestday"]

    sections, alerts = [], []
    newlows = {}

    for route in CONFIG["routes"]:
        f = RESULTSDIR / f"{route['key']}.json"
        if not f.exists():
            alerts.append(f"{route['label']}: no scan results (job failed?)")
            continue
        data = json.loads(f.read_text())
        pairs = sorted(data["pairs"], key=lambda p: p["price"])
        if not pairs:
            continue

        # month buckets: cheapest per departure month, compared to stored low
        bymonth = {}
        for p in pairs:
            m = p["depart"][:7]
            if m not in bymonth:
                bymonth[m] = p
        routelows = lows.get(route["key"], {})
        monthlines = []
        for m, p in sorted(bymonth.items()):
            prev = routelows.get(m)
            flag = ""
            if prev is None:
                flag = "first look"
            elif p["price"] < prev - max(CONFIG["newlowdropdollars"], prev * CONFIG["newlowdroppercent"] / 100):
                flag = f"NEW LOW (was ${prev})"
                alerts.append(f"{route['label']} {m}: ${p['price']} (was ${prev})")
            if p["typicallow"] and p["price"] <= p["typicallow"]:
                flag = (flag + ", " if flag else "") + "below Google typical range"
                if "NEW LOW" not in flag and prev is not None:
                    alerts.append(f"{route['label']} {m}: ${p['price']} is below typical range")
            newlows.setdefault(route["key"], {})[m] = min(p["price"], prev) if prev else p["price"]
            monthlines.append(f"{m}: ${p['price']} ({p['depart']} to {p['return']}, {', '.join(p['airlines'])}){'  <b>' + flag + '</b>' if flag else ''}")

        top = pairs[:5]
        toplines = [
            f"${p['price']}  {p['depart']} to {p['return']} ({p['nights']} nights)  {', '.join(p['airlines'])}, "
            f"{'nonstop' if p['stops'] == 0 else str(p['stops']) + ' stop' + ('s' if p['stops'] != 1 else '')}, "
            f"{(p['minutes'] or 0) // 60}h{(p['minutes'] or 0) % 60:02d}  <a href=\"{p['url']}\">book</a>"
            for p in top
        ]
        typical = top[0]
        sections.append(
            f"<h3>{route['label']}</h3>"
            f"<p>Google typical range for this route right now: ${typical['typicallow']} to ${typical['typicalhigh']}</p>"
            "<p><b>Cheapest 5 date pairs</b><br>" + "<br>".join(toplines) + "</p>"
            "<p><b>Cheapest by departure month</b><br>" + "<br>".join(monthlines) + "</p>"
        )

    # published deals
    dealhits = scandeals()
    seen = set(state.get("seendeals", []))
    fresh = [d for d in dealhits if d[1] not in seen]
    if dealhits:
        sections.append("<h3>Published deals matching your destinations</h3><p>" + "<br>".join(
            f"{'<b>NEW</b> ' if d in fresh else ''}{d[0]}: <a href=\"{d[2]}\">{html.escape(d[1])}</a>" for d in dealhits) + "</p>")
    if fresh:
        alerts.extend(f"Deal: {d[1]}" for d in fresh)

    # persist state (only months still inside the window survive)
    STATEFILE.parent.mkdir(exist_ok=True)
    STATEFILE.write_text(json.dumps({
        "updated": today.isoformat(),
        "lows": newlows,
        "seendeals": sorted(seen | {d[1] for d in dealhits})[-300:],
    }, indent=1))

    return alerts, weekly, sections


def sendemail(subject, body):
    user, pw, to = os.environ["GMAILUSER"], os.environ["GMAILAPPPASSWORD"], os.environ["ALERTRECIPIENT"]
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText(body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.sendmail(user, [to], msg.as_string())


def report():
    alerts, weekly, sections = buildreport()
    if not alerts and not weekly:
        print("No new lows, no new deals, not digest day. No email.")
        return
    subject = "FlightDealMonitor: " + ("; ".join(alerts)[:150] if alerts else "weekly digest")
    body = (
        "<div style=\"font-family:Aptos Narrow,Arial,sans-serif;font-size:10pt\">"
        + ("<p><b>Alerts</b><br>" + "<br>".join(html.escape(a) for a in alerts) + "</p>" if alerts else "")
        + "".join(sections)
        + f"<p style=\"color:#888\">Scanned {dt.date.today()} for departures {CONFIG['windowstartdays']} to {CONFIG['windowenddays']} days out.</p></div>"
    )
    sendemail(subject, body)
    print(f"Email sent: {subject}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["scan", "report", "matrix"])
    ap.add_argument("--route")
    args = ap.parse_args()
    if args.mode == "scan":
        scanroute(args.route)
    elif args.mode == "report":
        report()
    else:
        print(json.dumps([r["key"] for r in CONFIG["routes"]]))
