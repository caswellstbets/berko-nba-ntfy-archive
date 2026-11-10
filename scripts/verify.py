"""Check Berk's picks against real sportsbook odds at the time they're archived.

For each pick in a new message (matchup, side, line, price, book), this looks
up what books were actually offering right then:

- DraftKings, via ESPN's free odds feed. Always on. DraftKings is rarely the
  book Berk names, so this is a market check: does the line exist, and at
  roughly that price?
- The book Berk named (BetMGM, FanDuel, Pinnacle, ...), via The Odds API, but
  only if an ODDS_API_KEY repo secret is set. The free plan is 500 credits a
  month; each check costs 2, so calls are rationed (see odds_api_allowed).

Checks run when the archive job sees the message, usually 0-15 minutes after
the alert, so each record says how long after the alert it was taken. A
mismatch can mean the line moved in that time, not that it never existed.
"""

import datetime
import json
import os
import re
import urllib.parse
import urllib.request

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={date}"
ESPN_ODDS = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/events/{id}/competitions/{id}/odds"
ODDS_API = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"

# Berk's abbreviations -> ESPN's.
TEAM_ALIASES = {"WAS": "WSH", "GSW": "GS", "NOP": "NO", "NYK": "NY", "SAS": "SA", "UTA": "UTAH", "PHO": "PHX", "BRK": "BKN"}

# Book names as they appear in alerts -> The Odds API bookmaker keys.
ODDS_API_BOOKS = {
    "betmgm": "betmgm",
    "fanduel": "fanduel",
    "draftkings": "draftkings",
    "pinnacle": "pinnacle",
    "espn bet": "espnbet",
    "caesars": "williamhill_us",
    "betrivers": "betrivers",
    "fanatics": "fanatics",
}

# Keep this many Odds API credits in reserve, and between routine calls wait
# at least this long (a run with a brand-new pick always gets a call).
ODDS_API_RESERVE = 25
ODDS_API_MIN_GAP_SECONDS = 30 * 60

PICK_RE = re.compile(r"^(?:([A-Z]{2,4}) @ ([A-Z]{2,4}): )?(.+?) at ([+-]\d+)$")
BOOK_RE = re.compile(r"Book to check: ([^|]+?)\s*\|")
MATCHUP_RE = re.compile(r"^([A-Z]{2,4}) @ ([A-Z]{2,4})$")
ET = datetime.timezone(datetime.timedelta(hours=-5))


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "berko-nba-ntfy-archive"})
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read().decode("utf-8")), res.headers


def parse_picks(msg):
    """Pull every (away, home, side, price, book) out of one ntfy message."""
    if "NBA" not in msg.get("title", ""):
        return []
    picks = []
    blocks = [b.split("\n") for b in re.split(r"\n\s*\n", msg.get("message", ""))]
    for i, lines in enumerate(blocks):
        lines = [l.strip() for l in lines if l.strip()]
        book = next((m.group(1) for l in lines for m in [BOOK_RE.search(l)] if m), None)
        if not lines or not book:
            continue  # e.g. the "Other active picks" block, which isn't a fresh claim
        away = home = selection = price = None
        if i == 0 and MATCHUP_RE.match(lines[0]):
            # Single-pick alert: matchup on line 1, the pick itself in the title.
            away, home = MATCHUP_RE.match(lines[0]).groups()
            m = PICK_RE.match(msg["title"].split(": ", 1)[-1])
            if m:
                selection, price = m.group(3), int(m.group(4))
        else:
            for l in lines:
                m = PICK_RE.match(l)
                if m and m.group(1):
                    away, home, selection, price = m.group(1), m.group(2), m.group(3), int(m.group(4))
                    break
        if away and selection:
            picks.append({"away": away, "home": home, "selection": selection, "price": price, "book": book})
    return picks


def describe_market(selection, away, home):
    """'Over 215.5' -> ('total', 'over', 215.5); 'IND +4.5' -> ('spread', 'IND', 4.5); 'IND ML' -> ('ml', 'IND', None)."""
    m = re.match(r"^(Over|Under) (\d+(?:\.\d)?)$", selection)
    if m:
        return "total", m.group(1).lower(), float(m.group(2))
    m = re.match(r"^([A-Z]{2,4}) ([+-]\d+(?:\.\d)?)$", selection)
    if m:
        return "spread", m.group(1), float(m.group(2))
    m = re.match(r"^([A-Z]{2,4}) (?:ML|Moneyline)$", selection)
    if m:
        return "ml", m.group(1), None
    return None, None, None


def implied(american):
    return 100 / (american + 100) if american > 0 else -american / (-american + 100)


def verdict(claim_line, claim_price, found_line, found_price, same_book):
    """Compare the claimed line/price to what a book actually had."""
    if found_price is None:
        return "not offered"
    if claim_line is not None and found_line != claim_line:
        return "different line"
    gap = implied(found_price) - implied(claim_price)  # > 0 means the book's price was worse
    if gap <= 0.005:
        return "confirmed" if same_book else "line matches"
    if gap <= 0.02:
        return "close (price moved slightly)"
    return "price off"


def find_espn_event(away, home, when):
    away, home = TEAM_ALIASES.get(away, away), TEAM_ALIASES.get(home, home)
    date = datetime.datetime.fromtimestamp(when, ET).strftime("%Y%m%d")
    data, _ = get_json(ESPN_SCOREBOARD.format(date=date))
    for e in data.get("events", []):
        teams = {t["homeAway"]: t for t in e["competitions"][0]["competitors"]}
        if teams["away"]["team"]["abbreviation"] == away and teams["home"]["team"]["abbreviation"] == home:
            return e, teams
    return None, None


def check_draftkings(pick, kind, side, line, event, teams):
    data, _ = get_json(ESPN_ODDS.format(id=event["id"]))
    o = next((x for x in data.get("items", []) if x.get("provider", {}).get("name") == "DraftKings"), None)
    if not o:
        return None
    if kind == "total":
        found_line = o.get("overUnder")
        found_price = o.get("overOdds" if side == "over" else "underOdds")
    else:
        team = TEAM_ALIASES.get(side, side)
        is_away = teams["away"]["team"]["abbreviation"] == team
        team_odds = o.get("awayTeamOdds" if is_away else "homeTeamOdds") or {}
        if kind == "spread":
            home_spread = o.get("spread")
            found_line = None if home_spread is None else (-home_spread if is_away else home_spread)
            found_price = team_odds.get("spreadOdds")
        else:
            found_line, found_price = None, team_odds.get("moneyLine")
    found_price = int(found_price) if found_price is not None else None
    return {
        "source": "ESPN (DraftKings)",
        "book": "DraftKings",
        "line": found_line,
        "price": found_price,
        "verdict": verdict(line, pick["price"], found_line, found_price, pick["book"].lower() == "draftkings"),
    }


def fetch_odds_api(key):
    params = urllib.parse.urlencode({
        "apiKey": key,
        "bookmakers": ",".join(ODDS_API_BOOKS.values()),
        "markets": "spreads,totals",
        "oddsFormat": "american",
    })
    data, headers = get_json(f"{ODDS_API}?{params}")
    remaining = headers.get("x-requests-remaining")
    return data, int(float(remaining)) if remaining is not None else None


def check_odds_api(pick, kind, side, line, odds_data, teams):
    book_key = ODDS_API_BOOKS.get(pick["book"].lower())
    if not book_key or kind == "ml":
        return None
    away_name, home_name = teams["away"]["team"]["name"], teams["home"]["team"]["name"]
    game = next((g for g in odds_data if g["away_team"].endswith(away_name) and g["home_team"].endswith(home_name)), None)
    if not game:
        return None
    book = next((b for b in game.get("bookmakers", []) if b["key"] == book_key), None)
    found_line = found_price = None
    if book:
        market = next((m for m in book["markets"] if m["key"] == ("totals" if kind == "total" else "spreads")), None)
        if market:
            if kind == "total":
                want = side.capitalize()
            else:
                team = TEAM_ALIASES.get(side, side)
                want = game["away_team"] if teams["away"]["team"]["abbreviation"] == team else game["home_team"]
            outcome = next((o for o in market["outcomes"] if o["name"] == want), None)
            if outcome:
                found_line, found_price = outcome.get("point"), int(outcome["price"])
    return {
        "source": "The Odds API",
        "book": pick["book"],
        "line": found_line,
        "price": found_price,
        "verdict": verdict(line, pick["price"], found_line, found_price, True),
    }


def odds_api_allowed(state, has_new_pick, now):
    if not os.environ.get("ODDS_API_KEY"):
        return False
    remaining = state.get("odds_api_remaining")
    if remaining is not None and remaining < ODDS_API_RESERVE:
        return False
    return has_new_pick or now - state.get("odds_api_last_call", 0) >= ODDS_API_MIN_GAP_SECONDS


def verify_messages(messages, state):
    """Return one verification record per pick in these messages."""
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    jobs = [(m, p) for m in messages for p in parse_picks(m)]
    if not jobs:
        return []

    odds_data = None
    has_new_pick = any("New Pick" in m["title"] or "new" in m["title"].lower() for m, _ in jobs)
    if odds_api_allowed(state, has_new_pick, now):
        try:
            odds_data, remaining = fetch_odds_api(os.environ["ODDS_API_KEY"])
            state["odds_api_last_call"] = now
            if remaining is not None:
                state["odds_api_remaining"] = remaining
        except Exception as err:  # never let an odds problem stop archiving
            print(f"Odds API check failed: {err}")

    records = []
    for msg, pick in jobs:
        rec = {
            "message_id": msg["id"],
            "alert_time": msg["time"],
            "checked_time": int(now),
            "minutes_after_alert": round((now - msg["time"]) / 60, 1),
            "pick": pick,
            "checks": [],
            "note": None,
        }
        kind, side, line = describe_market(pick["selection"], pick["away"], pick["home"])
        try:
            event, teams = find_espn_event(pick["away"], pick["home"], msg["time"])
            if not kind:
                rec["note"] = "couldn't read this kind of bet"
            elif not event:
                rec["note"] = "game not found on ESPN"
            elif event["status"]["type"]["state"] != "pre":
                rec["note"] = "game had already started when checked, so pregame odds were gone"
            else:
                for check in (
                    check_draftkings(pick, kind, side, line, event, teams),
                    check_odds_api(pick, kind, side, line, odds_data, teams) if odds_data else None,
                ):
                    if check:
                        rec["checks"].append(check)
                if not rec["checks"]:
                    rec["note"] = "no odds available for this game"
        except Exception as err:
            rec["note"] = f"check failed: {err}"
        records.append(rec)
    return records


def summarize(rec):
    p = rec["pick"]
    claim = f"{p['away']} @ {p['home']} {p['selection']} {p['price']:+d} ({p['book']})"
    if rec["note"]:
        return f"{claim}: not checked, {rec['note']}"
    parts = []
    for c in rec["checks"]:
        found = "not offered" if c["price"] is None else f"{c['line'] if c['line'] is not None else ''} {c['price']:+d}".strip()
        parts.append(f"{c['book']} had {found} → {c['verdict']}")
    return f"{claim}, checked {rec['minutes_after_alert']:g} min after alert: " + "; ".join(parts)
