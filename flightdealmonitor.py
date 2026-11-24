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

import urllib.parse

import requests
from primp import Client
from fast_flights import FlightQuery, Passengers, create_query, fetch_flights_html
from selectolax.lexbor import LexborHTMLParser

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "routes.json").read_text())
RESULTSDIR = ROOT / "results"
STATEFILE = ROOT / ".state" / "lows.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"


# ---------------------------------------------------------------- scanning

def dig(obj, *path):
    """obj[a][b][c]..., or None if any step is missing."""
    for key in path:
        try:
            obj = obj[key]
        except (IndexError, KeyError, TypeError):
            return None
        if obj is None:
            return None
    return obj


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
            # raw_decode reads the first complete JSON value and ignores whatever
            # Google appends after it (that trailing text caused "Extra data" errors)
            payload, _ = json.JSONDecoder().raw_decode(node.text().split("data:", 1)[1].lstrip())
            break
        except Exception as exc:  # noqa: BLE001
            lasterr = exc
            time.sleep(3 * (attempt + 1))
    else:
        raise RuntimeError(f"{origin}-{dest} {depart}/{ret}: {lasterr}")

    fare = readfare(payload)
    if fare is None:
        # Google often leaves the flights off the first page and sends them in a
        # second request; ask for that second step ourselves
        second = secondstep(origin, dest, depart, ret)
        fare = readfare(second) if second is not None else None
        if fare is None:
            return None
        SECOND["filled"] += 1

    fare.update({"depart": depart, "return": ret, "url": q.url()})
    return fare


def readfare(payload):
    """Cheapest fare and its itinerary detail from one Google Flights payload, or None."""
    # Google sometimes leaves fields out; dig() returns None instead of raising
    # "list index out of range"
    best = None
    for section in (dig(payload, 2, 0), dig(payload, 3, 0)):
        for row in section or []:
            try:
                rowprice = row[1][0][1]
                legs = row[0][2]
                if best is None or rowprice < best["price"]:
                    best = {
                        "price": rowprice,
                        "airlines": row[0][1],
                        "stops": len(legs) - 1,
                        "minutes": row[0][9] if isinstance(row[0][9], int) else sum(l[11] or 0 for l in legs),
                    }
            except (IndexError, TypeError, KeyError):
                continue  # one malformed itinerary row does not spoil the fare

    # Google's own lowest price for the dates; fall back to the cheapest listed
    # itinerary when the price summary is missing
    price = dig(payload, 5, 1, 1)
    if price is None and best is not None:
        price = best["price"]
    if price is None:
        return None
    return {
        "price": price,
        "typical": dig(payload, 5, 2, 1),
        "typicallow": dig(payload, 5, 4, 1),
        "typicalhigh": dig(payload, 5, 5, 1),
        "airlines": best["airlines"] if best else [],
        "stops": best["stops"] if best else None,
        "minutes": best["minutes"] if best else None,
    }


# ------------------------------------------------------ second-step lookup
#
# For many date pairs the Google Flights page arrives empty and the browser
# fetches the flights with a second request (GetShoppingResults).  We send that
# same request.  Without the token Google's page code adds in a real browser,
# Google returns a reduced set of fares, so some cheaper itineraries can still be
# missing; Google Flights' own price tracking covers those.

SHOPPINGURL = ("https://www.google.com/_/FlightsFrontendUi/data/"
               "travel.frontend.flights.FlightsFrontendService/GetShoppingResults")
SECOND = {"client": None, "tried": 0, "filled": 0, "blocked": False, "skipped": 0}


def secondstep(origin, dest, depart, ret):
    """Payload with the most itineraries from the second-step request, or None."""
    if SECOND["blocked"]:
        SECOND["skipped"] += 1
        return None
    if SECOND["client"] is None:
        SECOND["client"] = Client(impersonate="chrome_145", impersonate_os="macos", referer=True, cookie_store=True)
        SECOND["client"].get("https://www.google.com/travel/flights", params={"hl": "en", "curr": CONFIG["currency"]})

    def leg(a, b, day):
        return [[[[a, 0]]], [[[b, 0]]], None, 0, None, None, day, None, None, None, None, None, None, None, 3]

    filters = [None, None, 1, None, [], 1, [CONFIG["passengers"], 0, 0, 0], None, None, None, None, None, None,
               [leg(origin, dest, depart), leg(dest, origin, ret)], None, None, None, 1]
    inner = json.dumps([[], filters, 0, 0, 0, 1], separators=(",", ":"))
    body = "f.req=" + urllib.parse.quote(json.dumps([None, inner], separators=(",", ":")))
    SECOND["tried"] += 1
    try:
        r = SECOND["client"].post(
            SHOPPINGURL + f"?hl=en&curr={CONFIG['currency']}&rt=c",
            headers={"content-type": "application/x-www-form-urlencoded;charset=UTF-8", "x-same-domain": "1",
                     "x-goog-ext-259736195-jspb": json.dumps(["en-US", "US", CONFIG["currency"], 2, None, [300],
                                                               None, None, 7, []], separators=(",", ":"))},
            content=body.encode())
    except Exception as exc:  # noqa: BLE001
        print(f"WARN second step {origin}-{dest} {depart}/{ret}: {exc}", file=sys.stderr)
        return None
    if r.status_code == 429:
        # Google is limiting us: stop asking for the rest of this route so we do
        # not make it worse; the count is reported in the email
        print(f"WARN second step limited by Google (HTTP 429) at {origin}-{dest} {depart}", file=sys.stderr)
        SECOND["blocked"] = True
        return None
    if r.status_code != 200:
        print(f"WARN second step {origin}-{dest} {depart}/{ret}: HTTP {r.status_code}", file=sys.stderr)
        return None
    best, bestrows = None, -1
    for payload in shoppingpayloads(r.text):
        rows = len(dig(payload, 2, 0) or []) + len(dig(payload, 3, 0) or [])
        if rows > bestrows:
            best, bestrows = payload, rows
    return best


def shoppingpayloads(text):
    """Each result payload inside a GetShoppingResults response."""
    dec = json.JSONDecoder()
    s = text.split("\n", 1)[1] if text.startswith(")]}'") else text
    i = 0
    while True:
        while i < len(s) and s[i].isspace():
            i += 1
        j = i
        while j < len(s) and s[j].isdigit():  # each frame starts with its length
            j += 1
        if j == i:
            return
        while j < len(s) and s[j].isspace():
            j += 1
        try:
            frame, i = dec.raw_decode(s, j)
        except ValueError:
            return
        for entry in frame:
            if isinstance(entry, list) and len(entry) > 2 and entry[0] == "wrb.fr" and isinstance(entry[2], str):
                try:
                    yield json.loads(entry[2])
                except ValueError:
                    continue


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

    RESULTSDIR.mkdir(exist_ok=True)
    if total and failures / total > 0.4:
        # too many failed lookups to trust: record that instead of prices, and let
        # the report name this route in the email (the run itself stays green)
        (RESULTSDIR / f"{key}.json").write_text(json.dumps(
            {"key": key, "scanned": today.isoformat(), "lookups": total, "failures": failures,
             "notscanned": True, "pairs": []}, indent=1))
        print(f"{key}: {failures}/{total} lookups failed, not reporting prices today")
        return

    (RESULTSDIR / f"{key}.json").write_text(json.dumps(
        {"key": key, "scanned": today.isoformat(), "lookups": total, "failures": failures,
         "secondfilled": SECOND["filled"], "secondskipped": SECOND["skipped"], "pairs": pairs},
        indent=1))
    print(f"{key}: {len(pairs)} priced date pairs from {total} lookups ({failures} failed); "
          f"second step filled {SECOND['filled']} of {SECOND['tried']} tried, {SECOND['skipped']} skipped after Google limited it")


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

    sections, alerts, limited = [], [], []
    newlows = {}

    for route in CONFIG["routes"]:
        f = RESULTSDIR / f"{route['key']}.json"
        if not f.exists():
            alerts.append(f"{route['label']}: no scan results (job failed?)")
            continue
        data = json.loads(f.read_text())
        if data.get("notscanned"):
            alerts.append(f"{route['label']}: not scanned today ({data['failures']} of {data['lookups']} price checks failed)")
            # keep the stored lows so a bad day does not wipe the history
            if route["key"] in lows:
                newlows[route["key"]] = lows[route["key"]]
            continue
        if data.get("secondskipped"):
            limited.append(f"{route['label']} ({data['secondskipped']})")
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

    if limited:
        sections.append("<p style=\"color:#888\">Google limited the second lookup today, so these routes have fewer "
                        "dates priced than usual (dates skipped in brackets): " + ", ".join(limited) + ".</p>")
    return alerts, weekly, sections


def sendemail(subject, body):
    user, pw = os.environ["GMAILUSER"], os.environ["GMAILAPPPASSWORD"]
    # ALERTRECIPIENT may hold several addresses separated by commas
    to = [a.strip() for a in os.environ["ALERTRECIPIENT"].split(",") if a.strip()]
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, ", ".join(to)
    msg.attach(MIMEText(body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.sendmail(user, to, msg.as_string())


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
