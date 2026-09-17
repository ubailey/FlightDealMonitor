# FlightDealMonitor

Daily airfare and package-deal monitor.  Same skeleton as MortgageRateMonitor:
Python, GitHub Actions, Gmail alerts.  No API key needed; prices come straight
from Google Flights, so every carrier Google lists is covered (Delta, American,
United, JetBlue, Frontier, Copa, Avianca, Turkish, Qatar, Etihad, Emirates, TAP,
and the rest).

## What it does each morning (8 AM ET)

1. For every route in `routes.json`, scans every departure day from 7 to 180
   days out, at each trip length listed, in parallel (one runner per route).
2. Records the cheapest fare per departure month in `.state/lows.json`.
3. Scans Travelzoo Top 20 (flight plus hotel packages), The Flight Deal and
   Fly4free for anything naming Atlanta or one of the destinations.
4. Emails when a route hits a new monthly low, a fare drops below Google's
   typical range, or a new published deal appears.  Every Monday it emails a
   full digest regardless.  Otherwise it stays silent.

Each fare in the email has a "book" link that opens that exact search in
Google Flights.

## Setup

Repository secrets (Settings, Secrets and variables, Actions):

| Secret | Value |
| --- | --- |
| `GMAILUSER` | sending Gmail address |
| `GMAILAPPPASSWORD` | 16-character Gmail app password |
| `ALERTRECIPIENT` | where the alert goes |

Then Actions tab, Daily Flight Deal Check, Run workflow.  The first run sets
the baseline (every month shows "first look") and sends the email so you can
see the layout.

## Tuning (all in `routes.json`)

- Add a route: one more line in `routes`.  Key must be unique, no spaces.
- Trip lengths: `triplengths` per route, nights.
- Window: `windowstartdays` and `windowenddays`.
- Sensitivity: `newlowdropdollars` and `newlowdroppercent` (a drop must beat
  whichever is larger to count as a new low).
- Digest day: `weeklydigestday`.
- Deal filters: `dealoriginkeywords`, `dealdestinationkeywords`, `dealfeeds`.

## Failure behaviour

A route whose lookups fail more than 20 percent of the time refuses to report
rather than emailing bad data, and GitHub emails you the failed run.  The
report job still runs on the routes that succeeded and names the missing ones.
