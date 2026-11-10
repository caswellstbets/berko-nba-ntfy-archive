"""Check Berk's picks against real sportsbook odds at the time they're archived.

For each pick in a new message (matchup, side, line, price, book), this looks
up what books were actually offering right then, from two free, keyless feeds:

- Action Network's public odds feed: BetMGM, FanDuel, DraftKings, Caesars,
  theScore Bet (formerly ESPN BET), Fanatics, BetRivers and bet365. Prices
  are for one state per book (NJ, NY or IL) and can differ slightly elsewhere.
- Pinnacle's public guest feed, main and alternate lines.

The book Berk named is checked first (when a feed covers it); every other book
is recorded too, as a check on whether the line existed in the market at all.

Checks run when the archive job sees the message, usually 0-15 minutes after
the alert, so each record says how long after the alert it was taken. A
mismatch can mean the line moved in that time, not that it never existed.
"""

import datetime
import json
import re
import urllib.request

ACTION_NETWORK = "https://api.actionnetwork.com/web/v2/scoreboard/nba?bookIds={ids}&date={date}&periods=event"
PINNACLE_MATCHUPS = "https://guest.api.arcadia.pinnacle.com/0.1/leagues/487/matchups"
PINNACLE_MARKETS = "https://guest.api.arcadia.pinnacle.com/0.1/leagues/487/markets/straight"

# Action Network book ids -> display names.
AN_BOOKS = {
    75: "BetMGM",
    69: "FanDuel",
    68: "DraftKings",
    1005: "Caesars",
    4601: "theScore Bet",
    2821: "Fanatics",
    71: "BetRivers",
    79: "bet365",
}

# Book names as they appear in alerts -> the name used above.
BOOK_ALIASES = {"espn bet": "theScore Bet", "william hill": "Caesars"}

# Different feeds spell a few teams differently; normalize to Berk's style.
TEAM_ALIASES = {"WSH": "WAS", "GS": "GSW", "NO": "NOP", "NY": "NYK", "SA": "SAS", "UTAH": "UTA", "PHO": "PHX", "BRK": "BKN"}

# The feed occasionally has junk (e.g. a full-game total of 7.5).
MIN_NBA_TOTAL = 150

PICK_RE = re.compile(r"^(?:([A-Z]{2,4}) @ ([A-Z]{2,4}): )?(.+?) at ([+-]\d+)$")
BOOK_RE = re.compile(r"Book to check: ([^|]+?)\s*\|")
MATCHUP_RE = re.compile(r"^([A-Z]{2,4}) @ ([A-Z]{2,4})$")
ET = datetime.timezone(datetime.timedelta(hours=-5))


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (berko-nba-ntfy-archive)"})
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read().decode("utf-8"))


def team(abbr):
    return TEAM_ALIASES.get(abbr, abbr)


def book_name(name):
    return BOOK_ALIASES.get(name.lower(), name)


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


def describe_market(selection):
    """'Over 215.5' -> ('total', 'over', 215.5); 'IND +4.5' -> ('spread', 'IND', 4.5); 'IND ML' -> ('ml', 'IND', None)."""
    m = re.match(r"^(Over|Under) (\d+(?:\.\d)?)$", selection)
    if m:
        return "total", m.group(1).lower(), float(m.group(2))
    m = re.match(r"^([A-Z]{2,4}) ([+-]\d+(?:\.\d)?)$", selection)
    if m:
        return "spread", team(m.group(1)), float(m.group(2))
    m = re.match(r"^([A-Z]{2,4}) (?:ML|Moneyline)$", selection)
    if m:
        return "ml", team(m.group(1)), None
    return None, None, None


def implied(american):
    return 100 / (american + 100) if american > 0 else -american / (-american + 100)


def verdict(claim_line, claim_price, found_line, found_price):
    """Compare the claimed line/price to what a book actually had."""
    if found_price is None:
        return "not offered"
    if claim_line is not None and found_line != claim_line:
        return "different line"
    gap = implied(found_price) - implied(claim_price)  # > 0 means the book's price was worse
    if gap <= 0.005:
        return "same line and odds"
    if gap <= 0.02:
        return "same line, odds slightly worse"
    return "same line, odds noticeably worse"


def pick_outcome(options, line):
    """From one side's offered (line, price, is_alt) options, take the claimed line if offered, else the main line."""
    exact = [o for o in options if line is not None and o[0] == line]
    main = [o for o in options if not o[2]]
    return (exact or main or [None])[0]


# ---- Action Network ----

def an_game(away, home, when):
    date = datetime.datetime.fromtimestamp(when, ET).strftime("%Y%m%d")
    data = get_json(ACTION_NETWORK.format(ids=",".join(map(str, AN_BOOKS)), date=date))
    for g in data.get("games", []):
        abbr = {t["id"]: team(t["abbr"]) for t in g["teams"]}
        if abbr.get(g["away_team_id"]) == team(away) and abbr.get(g["home_team_id"]) == team(home):
            return g, abbr
    return None, None


def an_lines(game, abbr, kind, side, line):
    """{book name: (line, price, is_alt)} for the claimed side, from every Action Network book."""
    found = {}
    key = {"total": "total", "spread": "spread", "ml": "moneyline"}[kind]
    for book_id, markets in (game.get("markets") or {}).items():
        name = AN_BOOKS.get(int(book_id))
        if not name:
            continue
        options = []
        for o in markets.get("event", {}).get(key, []):
            if o.get("is_live"):
                continue
            if kind == "total":
                if o["side"] != side or (o.get("value") or 0) < MIN_NBA_TOTAL:
                    continue
            else:
                team_id = game["away_team_id"] if o["side"] == "away" else game["home_team_id"]
                if abbr.get(team_id) != side:
                    continue
            options.append((None if kind == "ml" else o.get("value"), int(o["odds"]), bool(o.get("is_alt_market"))))
        chosen = pick_outcome(options, line)
        if chosen:
            found[name] = chosen
    return found


# ---- Pinnacle ----

def pinnacle_line(game, abbr, kind, side, line):
    """(line, price, is_alt) for the claimed side at Pinnacle, or None."""
    names = {t["id"]: t["full_name"] for t in game["teams"]}
    away_name, home_name = names[game["away_team_id"]], names[game["home_team_id"]]
    matchups = get_json(PINNACLE_MATCHUPS)

    def nickname(full):
        return full.split()[-1]

    matchup = next(
        (
            m for m in matchups
            if m.get("type") == "matchup" and not m.get("parentId")
            and {p["alignment"]: nickname(p["name"]) for p in m.get("participants", [])}
            == {"away": nickname(away_name), "home": nickname(home_name)}
        ),
        None,
    )
    if not matchup or matchup.get("isLive"):
        return None
    if kind == "total":
        designation = side
    else:
        designation = "away" if abbr.get(game["away_team_id"]) == side else "home"
    pin_type = {"total": "total", "spread": "spread", "ml": "moneyline"}[kind]
    options = []
    for m in get_json(PINNACLE_MARKETS):
        if m.get("matchupId") != matchup["id"] or m.get("period") != 0 or m.get("type") != pin_type:
            continue
        for p in m.get("prices", []):
            if p.get("designation") == designation:
                options.append((p.get("points"), int(p["price"]), bool(m.get("isAlternate"))))
    return pick_outcome(options, line)


def verify_messages(messages, state):
    """Return one verification record per pick in these messages."""
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    records = []
    for msg in messages:
        for pick in parse_picks(msg):
            rec = {
                "message_id": msg["id"],
                "alert_time": msg["time"],
                "checked_time": int(now),
                "minutes_after_alert": round((now - msg["time"]) / 60, 1),
                "pick": pick,
                "checks": [],
                "note": None,
            }
            kind, side, line = describe_market(pick["selection"])
            try:
                game, abbr = an_game(pick["away"], pick["home"], msg["time"]) if kind else (None, None)
                if not kind:
                    rec["note"] = "couldn't read this kind of bet"
                elif not game:
                    rec["note"] = "game not found in the odds feed"
                elif game.get("status") != "scheduled":
                    rec["note"] = "game had already started when checked, so pregame odds were gone"
                else:
                    found = an_lines(game, abbr, kind, side, line)
                    try:
                        pin = pinnacle_line(game, abbr, kind, side, line)
                        if pin:
                            found["Pinnacle"] = pin
                    except Exception as err:
                        print(f"Pinnacle check failed: {err}")
                    named = book_name(pick["book"])
                    for name in sorted(found, key=lambda n: n != named):
                        found_line, found_price, is_alt = found[name]
                        rec["checks"].append({
                            "book": name,
                            "named_book": name == named,
                            "line": found_line,
                            "price": found_price,
                            "alt_line": is_alt,
                            "verdict": verdict(line, pick["price"], found_line, found_price),
                        })
                    if not rec["checks"]:
                        rec["note"] = "no odds available for this game"
                    elif not any(c["named_book"] for c in rec["checks"]):
                        rec["note"] = f"no free odds feed covers {pick['book']}; other books shown"
            except Exception as err:
                rec["note"] = f"check failed: {err}"
            records.append(rec)
    return records


def fmt_found(check):
    if check["price"] is None:
        return "not offered"
    line = "" if check["line"] is None else f"{check['line']:g} "
    alt = " (alt line)" if check.get("alt_line") else ""
    return f"{line}{check['price']:+d}{alt}"


def summarize(rec):
    p = rec["pick"]
    claim = f"{p['away']} @ {p['home']} {p['selection']} {p['price']:+d} ({p['book']})"
    if not rec["checks"]:
        return f"{claim}: not checked, {rec['note']}"
    named = [c for c in rec["checks"] if c.get("named_book")]
    others = [c for c in rec["checks"] if not c.get("named_book")]
    parts = [f"{c['book']} had {fmt_found(c)} → {c['verdict']}" for c in named]
    if rec["note"]:
        parts.append(rec["note"])
    if others:
        parts.append("elsewhere: " + ", ".join(f"{c['book']} {fmt_found(c)}" for c in others))
    return f"{claim}, checked {rec['minutes_after_alert']:g} min after alert: " + "; ".join(parts)


if __name__ == "__main__":
    # Feed health check (run from the Actions tab with "check_feeds"): verify a
    # made-up pick on the next game that hasn't started, without saving anything.
    now = datetime.datetime.now(datetime.timezone.utc)
    for days in range(4):
        date = (now + datetime.timedelta(days=days)).astimezone(ET).strftime("%Y%m%d")
        games = get_json(ACTION_NETWORK.format(ids=",".join(map(str, AN_BOOKS)), date=date)).get("games", [])
        game = next((g for g in games if g.get("status") == "scheduled"), None)
        if game:
            abbr = {t["id"]: t["abbr"] for t in game["teams"]}
            away, home = abbr[game["away_team_id"]], abbr[game["home_team_id"]]
            msg = {"id": "feed-check", "time": int(now.timestamp()), "title": f"NBA New Pick: {away} +0.5 at -110",
                   "message": f"{away} @ {home}\nBook to check: BetMGM | Model size: 0 units"}
            for rec in verify_messages([msg], {}):
                print(summarize(rec))
            break
    else:
        print("No upcoming games found in the next few days.")
