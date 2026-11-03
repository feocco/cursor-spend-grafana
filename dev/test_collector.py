"""Collector end-to-end against an in-process fake Cursor API with synthetic data and a real
Postgres. Uses its own database, spend_test, which it drops and recreates.

    docker compose run --rm -v ./dev:/app/dev --entrypoint python collector dev/test_collector.py
"""

from __future__ import annotations

import json
import os
import random
import sys
import threading
import traceback
from datetime import date
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import psycopg

ADMIN_DSN = os.environ.get("DATABASE_URL", "postgresql://spend:spend@postgres:5432/spend")
TEST_DSN = ADMIN_DSN.rsplit("/", 1)[0] + "/spend_test"
os.environ.update(DATABASE_URL=TEST_DSN, CURSOR_API_BASE_URL="http://127.0.0.1:8765", RATE_SCALE="0")
sys.path.insert(0, "/app")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))

import collector as c  # noqa: E402

T0 = 1_761_350_400_000  # 2025-10-25T00:00:00Z
DAY1, DAY2 = date(2025, 10, 25), date(2025, 10, 26)
H = c.HOUR_MS
PAGE_CAP = 50


class FakeApi:
    """Synthetic team plus the API quirks the collector must survive: a 429 with Retry-After,
    page drift, "null" id strings, charges that grow, events that vanish upstream."""

    def __init__(self, with_ids: bool = True):
        rng = random.Random(7)
        self.members = [
            {"id": f"user_{i:02d}", "email": f"Person{i:02d}@example.com", "name": f"Person {i:02d}", "role": "member", "isRemoved": False}
            for i in range(12)
        ]
        self.members.append({"id": "user_old", "email": "person03@example.com", "name": "Old Person 03", "role": "member", "isRemoved": True})
        self.groups = {"grp_a": ["person00@example.com", "person01@example.com"], "grp_b": ["person01@example.com"]}
        self.events = []
        for i in range(400):
            ev = {
                "timestamp": str(T0 + i * 400_000), "userEmail": f"person{rng.randrange(12):02d}@example.com",
                "serviceAccountId": "null", "conversationId": f"conv-{i // 3}", "model": rng.choice(["gpt-5", "composer-2"]),
                "kind": "Usage-based", "maxMode": False, "isTokenBasedCall": True, "isChargeable": True, "isHeadless": False,
                "chargedCents": round(rng.uniform(1, 90), 4),
            }
            if i % 4 == 0:
                ev.update(cloudAgentId=f"bc-{i // 8}", isHeadless=True)
            if i % 20 == 0:
                ev.update(serviceAccountId="sa_ci", serviceAccountName="Nightly CI Agent", automationId="auto-1")
            if with_ids:
                ev["usageEventId"] = f"ue_{i:05d}"
            self.events.append(ev)
        self.reviews = [
            {"request_id": f"rv-{i}", "timestamp": "2025-10-25T12:00:00.000Z", "repo": "github.com/acme/app",
             "pr_number": i, "cost_cents": 40.0, "dry_run": False, "billed": True}
            for i in range(6)
        ]
        self.pending_429 = 0
        self.served_429 = 0
        self.drift_once = False
        self.short_after: int | None = None
        self.omit_total = False
        self.usage_requests = 0
        self.lock = threading.Lock()

    def window(self, start: int, end: int) -> list[dict]:
        return sorted((e for e in self.events if start <= int(e["timestamp"]) <= end), key=lambda e: -int(e["timestamp"]))

    def usage_page(self, body: dict) -> tuple[int, dict, dict]:
        with self.lock:
            if self.pending_429:
                self.pending_429 -= 1
                self.served_429 += 1
                return 429, {"message": "Rate limit exceeded"}, {"Retry-After": "1"}
            self.usage_requests += 1
            rows = self.window(body["startDate"], body["endDate"])
            total = len(rows)
            if self.short_after is not None and body["endDate"] > self.short_after and rows:
                rows = rows[:-1]
            page, size = body.get("page", 1), min(body.get("pageSize", 100), PAGE_CAP)
            chunk = rows[(page - 1) * size: page * size]
            if self.drift_once and page == 2 and len(rows) > size:
                self.drift_once = False
                chunk = rows[size - 1: 2 * size - 1]
            out = {"usageEvents": chunk, "pagination": {"currentPage": page, "hasNextPage": page * size < len(rows)}}
            return 200, out if self.omit_total else out | {"totalUsageEventsCount": total}, {}

    def get(self, path: str, q: dict) -> tuple[int, dict]:
        if path == "/teams/members":
            return 200, {"teamMembers": self.members}
        if path == "/teams/directory-groups":
            return 200, {"groups": [{"id": g, "name": g} for g in self.groups], "pagination": {"hasNextPage": False}}
        if path.startswith("/teams/directory-groups/"):
            emails = self.groups[path.split("/")[3]]
            return 200, {"members": [{"email": e} for e in emails], "pagination": {"hasNextPage": False}}
        if path == "/analytics/team/bugbot-reviews":
            page, size = int(q.get("page", 1)), 4
            return 200, {"data": self.reviews[(page - 1) * size: page * size], "pagination": {"hasNextPage": page * size < len(self.reviews)}}
        return 404, {"message": "not found"}

    def __enter__(self) -> FakeApi:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code: int, payload: dict, headers: dict | None = None):
                body = json.dumps(payload).encode()
                self.send_response(code)
                for k, v in {"Content-Type": "application/json", "Content-Length": str(len(body)), **(headers or {})}.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                url = urlparse(self.path)
                self.reply(*api.get(url.path, {k: v[0] for k, v in parse_qs(url.query).items()}))

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.reply(*api.usage_page(body))

        self.server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def scenario(fn):
    """Each scenario gets a fresh database; an exception fails the scenario, not the run."""
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS spend_test WITH (FORCE)")
        conn.execute("CREATE DATABASE spend_test")
    store = c.Store()
    try:
        store.migrate()
        fn(store)
    except BaseException as exc:  # noqa: BLE001  SystemExit from old collectors must fail a scenario, not the run
        traceback.print_exc()
        check(False, f"{fn.__name__} raised {type(exc).__name__}: {exc}")
    finally:
        store.conn.close()


def q(store: c.Store, sql: str, *args):
    row = store.conn.execute(sql, args).fetchone()
    store.conn.rollback()
    return row[0] if len(row) == 1 else row


def totals(store: c.Store) -> tuple[int, Decimal]:
    return q(store, "SELECT count(*), coalesce(sum(charged_cents), 0) FROM usage_events")


def source_cents(api: FakeApi) -> Decimal:
    return sum((Decimal(str(e["chargedCents"])) for e in api.events), Decimal(0))


def backfill(client: c.CursorApi, store: c.Store) -> None:
    c.sync_usage(client, store, c.day_start_ms(DAY1), c.day_start_ms(DAY2) + c.DAY_MS - 1, "usage-backfill")


def dedupe(with_ids: bool):
    label = "usageEventId" if with_ids else "natural key"

    def run(store: c.Store) -> None:
        with FakeApi(with_ids) as api:
            api.pending_429, api.drift_once = 1, True
            client = c.CursorApi("test-key", base_url=c.BASE_URL)
            backfill(client, store)
            n, s = totals(store)
            check(api.served_429 == 1 and not api.drift_once, f"[{label}] a 429 and a page drift were served")
            check((n, s) == (len(api.events), source_cents(api)), f"[{label}] backfill stores every event once ({n}/{len(api.events)}, {s})")
            backfill(client, store)
            check(totals(store) == (n, s), f"[{label}] re-running a window adds nothing")
            api.events[5]["chargedCents"] += 100
            backfill(client, store)
            check(totals(store) == (n, s + 100), f"[{label}] a grown charge updates its row")
            gone = api.events.pop(7)
            backfill(client, store)
            check(totals(store) == (n - 1, s + 100 - Decimal(str(gone["chargedCents"]))), f"[{label}] an event gone upstream is removed")
            sa = q(store, "SELECT count(*) FROM usage_events WHERE service_account_id IS NOT NULL")
            check(sa == sum(e["serviceAccountId"] != "null" for e in api.events), f"[{label}] \"null\" ids are stored as NULL ({sa} SA rows)")

    run.__name__ = f"dedupe_{label.replace(' ', '_')}"
    return run


def watermark(store: c.Store) -> None:
    with FakeApi() as api:
        client = c.CursorApi("test-key", base_url=c.BASE_URL)

        def stored_through(ms: int) -> bool:
            return q(store, "SELECT count(*) FROM usage_events") == len(api.window(0, ms))

        now = T0 + 12 * H
        c.sync_usage_incremental(client, store, now)
        check(store.watermark("usage") == now and stored_through(now), "first pass reads INITIAL_LOOKBACK_HOURS and sets the watermark to now")
        api.usage_requests, now = 0, now + H
        c.sync_usage_incremental(client, store, now)
        lo, hi = q(store, "SELECT window_start, window_end FROM collector_runs ORDER BY id DESC LIMIT 1")
        check(abs((hi - lo).total_seconds() - 3 * 3600) < 0.01 and api.usage_requests == 1, "next pass reads watermark - 2 h .. now in one request")
        now += 30 * H
        c.sync_usage_incremental(client, store, now)
        check(store.watermark("usage") == now and stored_through(now), "a 30 h gap is caught up on the next pass")

        store.conn.execute("TRUNCATE usage_events, sync_watermarks")
        store.conn.commit()
        api.short_after, now = T0 + 20 * H, T0 + 40 * H
        c.sync_usage_incremental(client, store, now)
        wm, status = store.watermark("usage"), q(store, "SELECT status FROM collector_runs ORDER BY id DESC LIMIT 1")
        check(wm is not None and wm < api.short_after and status == "incomplete", f"watermark stops before an incomplete chunk (run {status})")
        api.short_after = None
        c.sync_usage_incremental(client, store, now)
        check(store.watermark("usage") == now and stored_through(now), "next pass resumes at the incomplete chunk")

        c.sync_bugbot_incremental(client, store, DAY2)
        c.sync_bugbot_incremental(client, store, DAY2)
        n, cost = q(store, "SELECT count(*), sum(cost_cents) FROM bugbot_reviews")
        check((n, cost) == (len(api.reviews), 240), f"bugbot reviews paginate and re-runs are idempotent ({n}, {cost}c)")


def backfill_reports_gaps(store: c.Store) -> None:
    with FakeApi() as api:
        client = c.CursorApi("test-key", base_url=c.BASE_URL)
        api.short_after = c.day_start_ms(DAY1) + 6 * H
        status = c.sync_usage(client, store, c.day_start_ms(DAY1), c.day_start_ms(DAY2) + c.DAY_MS - 1, "usage-backfill")
        n, before_gap = totals(store)[0], len(api.window(0, api.short_after))
        check(status == "incomplete" and before_gap < n < len(api.events), f"a backfill with short windows ends incomplete and keeps reading ({status}, {n}/{len(api.events)})")
        api.short_after = None
        backfill(client, store)
        api.events.pop(7)
        api.omit_total = True
        status = c.sync_usage(client, store, c.day_start_ms(DAY1), c.day_start_ms(DAY2) + c.DAY_MS - 1, "usage-backfill")
        check(status == "incomplete" and totals(store)[0] == len(api.events) + 1, "a response without a total deletes nothing")


def scheduled_pass(store: c.Store) -> None:
    with FakeApi() as api:
        c.scheduled_sync(c.CursorApi("test-key", base_url=c.BASE_URL), store)
        check(q(store, "SELECT count(*) FROM (SELECT email FROM members GROUP BY 1 HAVING count(*) > 1) d") == 0,
              "a removed and a current member sharing an email load as one row")
        check(q(store, "SELECT name FROM members WHERE email = 'person03@example.com'") == "Person 03", "the current member wins")
        check(q(store, "SELECT count(*) FROM directory_group_members") == 3, "team groups load with no setup")
        check(q(store, "SELECT status FROM collector_runs WHERE job = 'usage-scheduled'") == "ok", "the scheduled pass finishes ok")


def survives_postgres_restart(store: c.Store) -> None:
    with FakeApi() as api:
        client = c.CursorApi("test-key", base_url=c.BASE_URL)
        c.run_pass(client)
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'spend_test' AND pid <> pg_backend_pid()")
        c.run_pass(client)
        ok = q(c.Store(), "SELECT count(*) FROM collector_runs WHERE status = 'ok' AND job = 'usage-scheduled'")
        check(ok == 2, f"the pass after Postgres drops every connection still succeeds ({ok} ok passes)")


if __name__ == "__main__":
    c.logging.basicConfig(level=c.logging.WARNING, format="%(levelname)s %(message)s", stream=sys.stdout)
    for s in (dedupe(False), dedupe(True), watermark, backfill_reports_gaps, scheduled_pass, survives_postgres_restart):
        print(f"== {s.__name__}")
        scenario(s)
    print(f"{len(failures)} FAILED" if failures else "ALL PASSED")
    sys.exit(1 if failures else 0)
