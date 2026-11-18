"""Loads 30 days of synthetic demo data through the collector's own Store methods, for screenshots.
Every person, repo, and ID is fake. It replaces members and team groups, so point it only at a scratch database.

    python3 dev/seed_demo.py postgresql://spend:spend@127.0.0.1:15433/spend 2026-10-20 2026-11-18
"""

from __future__ import annotations

import argparse
import random
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "collector"))

import collector as c  # noqa: E402

if not c.SCHEMA_SQL.exists():
    c.SCHEMA_SQL = ROOT / "db" / "schema.sql"

RNG = random.Random(7)

PEOPLE = [
    # email, name, git login, group, daily IDE events, cents per event
    ("alice@example.com", "Alice Example", "alice-example", "Platform", 70, 9.0),
    ("bob@example.com", "Bob Example", "bob-example", "Platform", 45, 7.0),
    ("carol@example.com", "Carol Example", "carol-example", "Web", 60, 11.0),
    ("dave@example.com", "Dave Example", "dave-example", "Web", 30, 6.0),
    ("erin@example.com", "Erin Example", "erin-example", "Data", 50, 8.0),
    ("frank@example.com", "Frank Example", "frank-example", "Data", 20, 5.0),
]
REPOS = {
    "github.com/example-org/web-app": 1.0,
    "github.com/example-org/api-server": 0.8,
    "github.com/example-org/data-pipeline": 0.5,
    "github.com/example-org/mobile-app": 0.35,
    "github.com/example-org/docs-site": 0.15,
}
MODELS = ["claude-4.5-sonnet", "gpt-5", "composer-1", "claude-4.5-opus", "gemini-2.5-pro"]
BUGBOT_SA = ("sa_demo_bugbot", "Demo Bugbot Account")
SECURITY_SA = ("sa_demo_security", "Demo Security Account")
CHANGE_SA = ("sa_demo_change", "Demo Change Monitor Account")
CI_SA = ("sa_demo_ci", "demo-ci-runner")
DOCS_SA = ("sa_demo_docs", "demo-docs-sync")


def fake_id(prefix: str = "") -> str:
    return prefix + str(uuid.UUID(int=RNG.getrandbits(128), version=4))


AUTOMATIONS = [  # automation id, owner email, runs per day
    (fake_id(), "alice@example.com", 4),
    (fake_id(), "carol@example.com", 3),
    (fake_id(), "erin@example.com", 2),
]


def event(ts_ms: int, cents: float, **fields) -> dict:
    tokens = int(cents * RNG.uniform(800, 1500))
    return {
        "timestamp": str(ts_ms), "usageEventId": fake_id("ue_"), "kind": "Usage-based",
        "maxMode": False, "isTokenBasedCall": True, "isChargeable": True, "isHeadless": False,
        "requestsCosts": 1.0, "cursorTokenFee": 0, "chargedCents": round(cents, 2),
        "tokenUsage": {"inputTokens": tokens, "outputTokens": tokens // 8, "cacheReadTokens": tokens * 3,
                       "cacheWriteTokens": 0, "totalCents": round(cents, 2)},
        "serviceAccountId": "null", "cloudAgentId": "null", "automationId": "null", **fields,
    }


def at(day: date, start_hour: int = 8, end_hour: int = 19) -> int:
    base = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return int((base + timedelta(seconds=RNG.uniform(start_hour, end_hour) * 3600)).timestamp() * 1000)


def busy(day: date) -> float:
    return 0.25 if day.weekday() >= 5 else 1.0


def build(start: date, end: date) -> tuple[list[dict], list[dict]]:
    events, reviews = [], []
    pr_numbers = {repo: RNG.randint(400, 1800) for repo in REPOS}
    day = start
    while day <= end:
        b = busy(day)
        for email, _, login, _, per_day, cents in PEOPLE:
            for _ in range(int(per_day * b * RNG.uniform(0.6, 1.3))):
                events.append(event(at(day), RNG.expovariate(1 / cents), userEmail=email, model=RNG.choice(MODELS),
                                    conversationId=fake_id()))
            for _ in range(RNG.choices([0, 1, 2], [3, 2, 1])[0] if b == 1 else 0):
                agent = fake_id("bc-")
                for _ in range(RNG.randint(4, 12)):
                    events.append(event(at(day), RNG.uniform(20, 90), userEmail=email, model=RNG.choice(MODELS[:3]),
                                        cloudAgentId=agent, isHeadless=True))
        for automation, owner, runs in AUTOMATIONS:
            for _ in range(max(1, int(runs * b))):
                agent = fake_id("bc-")
                for _ in range(RNG.randint(3, 8)):
                    events.append(event(at(day, 0, 24), RNG.uniform(15, 60), userEmail=owner, model="claude-4.5-sonnet",
                                        cloudAgentId=agent, automationId=automation, isHeadless=True))
        for _ in range(int(3 * b) + 1):
            agent = fake_id("bc-")
            for _ in range(RNG.randint(3, 9)):
                events.append(event(at(day, 0, 24), RNG.uniform(20, 70), serviceAccountId=CI_SA[0],
                                    serviceAccountName=CI_SA[1], model="composer-1", cloudAgentId=agent, isHeadless=True))
        for _ in range(int(12 * b)):
            events.append(event(at(day, 0, 24), RNG.uniform(1, 6), serviceAccountId=DOCS_SA[0],
                                serviceAccountName=DOCS_SA[1], model="gpt-5", isHeadless=True))
        for repo, weight in REPOS.items():
            for _ in range(int(RNG.uniform(4, 9) * weight * b)):
                pr_numbers[repo] += RNG.randint(0, 1)
                author = RNG.choices([p[2] for p in PEOPLE] + ["dependabot[bot]"], [5, 4, 6, 2, 4, 2, 1])[0]
                ts = at(day, 6, 22)
                events.append(event(ts, RNG.uniform(30, 140), model="github_bugbot", serviceAccountId=BUGBOT_SA[0],
                                    serviceAccountName=BUGBOT_SA[1], repo=repo, prNumber=pr_numbers[repo], prAuthor=author))
                reviews.append({
                    "request_id": fake_id(), "repo": repo, "pr_number": pr_numbers[repo], "pr_author": author,
                    "timestamp": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                    "commit_sha": "0" * 40, "bugs_found": RNG.randint(0, 3), "cost_cents": None, "billed": None,
                    "dry_run": False, "publication_status": "posted", "bugs": [],
                })
        for _ in range(int(RNG.uniform(10, 18) * b)):
            events.append(event(at(day, 6, 22), RNG.uniform(15, 35), model="", kind="Included in Business",
                                serviceAccountId=SECURITY_SA[0], serviceAccountName=SECURITY_SA[1],
                                automationManagedType="SECURITY_REVIEWER", conversationId=fake_id()))
        for _ in range(RNG.randint(0, 2)):
            events.append(event(at(day, 0, 24), RNG.uniform(10, 30), model="", kind="Included in Business",
                                serviceAccountId=CHANGE_SA[0], serviceAccountName=CHANGE_SA[1],
                                automationManagedType="CHANGE_MONITOR", conversationId=fake_id()))
        for _ in range(int(6 * b)):
            events.append(event(at(day), RNG.uniform(3, 12), userEmail=RNG.choice(PEOPLE)[0], model="grok-bot-demo"))
        day += timedelta(days=1)
    return events, reviews


def record_runs(store: c.Store, end_ms: int) -> None:
    """Hourly scheduled passes ending now, so "Data age" and "Collector runs" look like a healthy stack."""
    now = datetime.now(timezone.utc)
    for i in range(12, 0, -1):
        started = now - timedelta(hours=i - 1, minutes=2)
        for job, rows in (("usage-scheduled", RNG.randint(180, 420)), ("bugbot", RNG.randint(5, 30))):
            run = store.start_run(job, end_ms - c.HOUR_MS * 3, end_ms)
            store.finish_run(run, "ok", rows, rows // 3)
            with store.conn.transaction():
                store.conn.execute("UPDATE collector_runs SET started_at = %s, finished_at = %s WHERE id = %s",
                                   (started, started + timedelta(seconds=RNG.randint(20, 90)), run))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dsn")
    ap.add_argument("start", type=date.fromisoformat)
    ap.add_argument("end", type=date.fromisoformat)
    args = ap.parse_args()
    events, reviews = build(args.start, args.end)
    start_ms, end_ms = c.day_start_ms(args.start), c.day_start_ms(args.end) + c.DAY_MS - 1
    with c.Store(args.dsn) as store:
        store.migrate()
        store.replace_members([{"id": f"user_demo_{i}", "email": p[0], "name": p[1], "role": "member", "isRemoved": False}
                               for i, p in enumerate(PEOPLE)])
        groups = sorted({p[3] for p in PEOPLE})
        store.replace_directory_groups([({"id": f"group_demo_{g.lower()}", "name": g}, [{"email": p[0]} for p in PEOPLE if p[3] == g])
                                        for g in groups])
        result = c.WindowResult({c.event_key(e): e for e in events}, len(events), len(events), True)
        new, deleted = store.upsert_usage(result, start_ms, end_ms)
        store.upsert_bugbot(reviews)
        record_runs(store, end_ms)
        store.advance_watermark("usage", end_ms)
    print(f"events={len(events)} new={new} stale_deleted={deleted} bugbot_reviews={len(reviews)}")


if __name__ == "__main__":
    main()
