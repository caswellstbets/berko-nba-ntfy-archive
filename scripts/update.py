"""Append new messages from Berk's ntfy topic to this repo.

Run every 10 minutes by .github/workflows/update.yml. ntfy only keeps messages
for about 12 hours, so this repo is the permanent record:

- notifications.jsonl: every message, exactly as ntfy sent it. Lines are only
  ever appended, never edited or removed.
- README.md: the same messages, readable, regenerated from the jsonl.
- CHANGELOG.md: one entry per run that found something. It also flags any
  message that vanished from ntfy before its expiry time, which usually means
  it was deleted on purpose.
- verifications.jsonl: for each pick, what real sportsbooks were offering
  when it was archived (see verify.py). Also append-only.

The git history of this repo is the audit trail: if a line were ever removed
from notifications.jsonl, the commit that did it would show it.
"""

import datetime
import json
import os
import urllib.request

from verify import summarize, verify_messages

TOPIC = "berko-nba-20260414-172920"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSONL = os.path.join(ROOT, "notifications.jsonl")
README = os.path.join(ROOT, "README.md")
CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")
STATE = os.path.join(ROOT, "state.json")
VERIFICATIONS = os.path.join(ROOT, "verifications.jsonl")

# Messages already archived before odds checks existed still get one check if
# they're this recent (their game may not have started yet).
BACKFILL_SECONDS = 3 * 3600

# Don't flag a message as deleted if it was about to expire anyway.
EXPIRY_MARGIN_SECONDS = 30 * 60


def utc(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def fetch_ntfy():
    url = f"https://ntfy.sh/{TOPIC}/json?poll=1&since=all"
    req = urllib.request.Request(url, headers={"User-Agent": "berko-nba-ntfy-archive"})
    with urllib.request.urlopen(req, timeout=30) as res:
        lines = res.read().decode("utf-8").splitlines()
    return [m for m in (json.loads(l) for l in lines if l.strip()) if m.get("event") == "message"]


def load_jsonl(path=JSONL):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def append_jsonl(path, rows):
    with open(path, "a") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def load_state():
    if not os.path.exists(STATE):
        return {"flagged_missing": []}
    with open(STATE) as f:
        return json.load(f)


def write_readme(messages, verifications):
    out = [
        "# berko-nba ntfy archive",
        "",
        f"Permanent, append-only record of the ntfy topic `{TOPIC}`, checked every 10 minutes.",
        "Raw messages are in `notifications.jsonl`; `CHANGELOG.md` lists what each check added or",
        "found missing, and the commit history shows any change ever made to the files.",
        "",
        "**Odds checks:** when a new pick is archived, its line is looked up at real sportsbooks",
        "(`verifications.jsonl`). DraftKings comes from ESPN's free feed; the exact book named in the",
        "alert is checked too when an Odds API key is set. Checks happen minutes after the alert, so",
        "a mismatch can mean the line moved, not that it never existed.",
        "",
    ]
    checks = {}
    for v in verifications:
        checks.setdefault(v["message_id"], []).append(v)
    for m in messages:
        out += [f"## {m.get('title', '')}", f"*{utc(m['time'])} · id `{m['id']}`*", "", "```", m.get("message", ""), "```", ""]
        for v in checks.get(m["id"], []):
            out.append(f"- Odds check: {summarize(v)}")
        if m["id"] in checks:
            out.append("")
    with open(README, "w") as f:
        f.write("\n".join(out))


def prepend_changelog(lines):
    header = "# Changelog\n\nNewest first. Written automatically each time a check finds something.\n\n"
    body = ""
    if os.path.exists(CHANGELOG):
        with open(CHANGELOG) as f:
            body = f.read()
        body = body[len(header):] if body.startswith(header) else body
    entry = f"## {utc(datetime.datetime.now(datetime.timezone.utc).timestamp())}\n\n" + "\n".join(lines) + "\n\n"
    with open(CHANGELOG, "w") as f:
        f.write(header + entry + body)


def main():
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    saved = load_jsonl()
    state = load_state()
    live = fetch_ntfy()

    saved_ids = {m["id"] for m in saved}
    live_ids = {m["id"] for m in live}
    fresh = sorted((m for m in live if m["id"] not in saved_ids), key=lambda m: m["time"])

    missing = [
        m for m in saved
        if m["id"] not in live_ids
        and m["id"] not in state["flagged_missing"]
        and m.get("expires", 0) > now + EXPIRY_MARGIN_SECONDS
    ]

    verified_ids = {v["message_id"] for v in load_jsonl(VERIFICATIONS)}
    to_verify = fresh + [
        m for m in saved if m["id"] not in verified_ids and now - m["time"] < BACKFILL_SECONDS
    ]
    new_checks = verify_messages(to_verify, state) if to_verify else []

    if not fresh and not missing and not new_checks:
        print("No changes.")
        return

    log = []
    for m in fresh:
        log.append(f"- Added `{m['id']}` ({utc(m['time'])}): {m.get('title', '')}")
    for m in missing:
        log.append(
            f"- ⚠️ `{m['id']}` ({utc(m['time'])}) disappeared from ntfy before it was due to expire "
            f"({utc(m['expires'])}), so it was probably deleted: {m.get('title', '')}. "
            "It is still kept here."
        )
        state["flagged_missing"].append(m["id"])

    for v in new_checks:
        log.append(f"- Odds check `{v['message_id']}`: {summarize(v)}")

    if fresh:
        append_jsonl(JSONL, fresh)
    if new_checks:
        append_jsonl(VERIFICATIONS, new_checks)
    if fresh or new_checks:
        write_readme(saved + fresh, load_jsonl(VERIFICATIONS))
    if missing or new_checks:
        with open(STATE, "w") as f:
            json.dump(state, f, indent=2)
            f.write("\n")
    prepend_changelog(log)
    print("\n".join(log))


if __name__ == "__main__":
    main()
