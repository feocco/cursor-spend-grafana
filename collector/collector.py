"""Cursor spend collector: Admin + Analytics API -> Postgres.

Commands (see README):
  serve                   sync now, then every SYNC_INTERVAL_SECONDS
  sync                    one scheduled pass (from the watermark minus OVERLAP_HOURS to now)
  backfill START END      usage events + Bugbot reviews for UTC days START..END inclusive
  reconcile START END     totals for START..END inclusive, on TIMEZONE and UTC days
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg
import requests

log = logging.getLogger("collector")

BASE_URL = os.environ.get("CURSOR_API_BASE_URL", "https://api.cursor.com").rstrip("/")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://spend:spend@postgres:5432/spend")
SYNC_INTERVAL_SECONDS = int(os.environ.get("SYNC_INTERVAL_SECONDS", "3600"))
INITIAL_LOOKBACK_HOURS = int(os.environ.get("INITIAL_LOOKBACK_HOURS", "48"))
OVERLAP_HOURS = float(os.environ.get("OVERLAP_HOURS", "2"))
BUGBOT_LOOKBACK_DAYS = int(os.environ.get("BUGBOT_LOOKBACK_DAYS", "3"))
RATE_SCALE = float(os.environ.get("RATE_SCALE", "1"))
SCHEMA_SQL = Path(__file__).with_name("schema.sql")
CHUNK_HOURS = 6
DEFAULT_RETRY_AFTER = 60.0

USAGE_PATH = "/teams/filtered-usage-events"
DAY_MS = 86_400_000
HOUR_MS = 3_600_000

# Seconds between calls per rate-limit bucket, at 75% of the documented per-team limits
# (60, 100, and 20 a minute). Calling at the full limit still drew 429s.
MIN_INTERVAL = {"usage": 60 / 45, "analytics": 60 / 75, "admin": 60 / 15}

# Fields that identify an event but never change after it is filed. Token counts and
# chargedCents grow while long agent runs continue, so they must stay out of the key.
NATURAL_KEY_FIELDS = (
    "timestamp", "userEmail", "serviceAccountId", "cloudAgentId", "automationId",
    "conversationId", "model", "kind", "maxMode", "isTokenBasedCall",
)


class ApiError(RuntimeError):
    pass


class CursorApi:
    def __init__(self, key: str, base_url: str = BASE_URL):
        if not key:
            raise SystemExit("CURSOR_TEAM_ADMIN_API_KEY is not set")
        self.session = requests.Session()
        self.session.auth = (key, "")
        self.base_url = base_url
        self.last_call: dict[str, float] = {}
        self.requests: Counter[str] = Counter()

    @staticmethod
    def bucket(path: str) -> str:
        if path == USAGE_PATH:
            return "usage"
        if path.startswith("/analytics/"):
            return "analytics"
        return "admin"

    def _throttle(self, bucket: str) -> None:
        wait = self.last_call.get(bucket, 0) + MIN_INTERVAL[bucket] * RATE_SCALE - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.last_call[bucket] = time.monotonic()

    def request(self, method: str, path: str, *, params=None, body=None, attempts: int = 6) -> dict:
        bucket = self.bucket(path)
        for attempt in range(1, attempts + 1):
            self._throttle(bucket)
            self.requests[bucket] += 1
            try:
                resp = self.session.request(method, self.base_url + path, params=params, json=body, timeout=180)
            except requests.RequestException as exc:
                delay = min(2 ** attempt, 60) * RATE_SCALE
                log.warning("%s %s network error (%s); retry %d in %.0fs", method, path, type(exc).__name__, attempt, delay)
                time.sleep(delay)
                continue
            if resp.status_code == 429:
                delay = float(resp.headers.get("Retry-After") or DEFAULT_RETRY_AFTER)
                log.warning("%s %s 429; honoring Retry-After %.0fs (attempt %d)", method, path, delay, attempt)
                time.sleep(delay)
                continue
            if resp.status_code >= 500:
                delay = min(2 ** attempt, 60) * RATE_SCALE
                log.warning("%s %s %d; retry %d in %.0fs", method, path, resp.status_code, attempt, delay)
                time.sleep(delay)
                continue
            if resp.status_code >= 400:
                raise ApiError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
            return resp.json()
        raise ApiError(f"{method} {path} failed after {attempts} attempts")

    def get(self, path: str, **params) -> dict:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: dict) -> dict:
        return self.request("POST", path, body=body)


def _clean(value):
    return None if value in (None, "", "null") else value


def event_key(event: dict) -> str:
    """usageEventId when the API sends one; otherwise a hash of the immutable identity fields."""
    usage_id = _clean(event.get("usageEventId"))
    if usage_id is not None:
        return f"id:{usage_id}"
    identity = [_clean(event.get(f)) for f in NATURAL_KEY_FIELDS]
    return "nk:" + hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


@dataclass
class WindowResult:
    events: dict[str, dict]
    api_total: int | None
    raw_rows: int
    complete: bool


def fetch_usage_window(api: CursorApi, start_ms: int, end_ms: int, max_passes: int = 3) -> WindowResult:
    """Pages shift while a live window is read, so a pass can return duplicates and skip rows.
    Re-read until the deduped set reaches the API's own count, merging later copies over earlier."""
    events: dict[str, dict] = {}
    api_total, raw_rows = None, 0
    for _ in range(max_passes):
        page = 1
        while True:
            data = api.post(USAGE_PATH, {"startDate": start_ms, "endDate": end_ms, "page": page, "pageSize": 1000})
            rows = data.get("usageEvents") or []
            raw_rows += len(rows)
            for row in rows:
                events[event_key(row)] = row
            api_total = data.get("totalUsageEventsCount", api_total)
            if not (data.get("pagination") or {}).get("hasNextPage"):
                break
            page += 1
        # Without the API's count a window can't be proven complete, and a complete window deletes rows.
        if api_total is not None and len(events) >= api_total:
            return WindowResult(events, api_total, raw_rows, True)
        log.info("window %s: %d unique < API total %d; re-reading", iso(start_ms), len(events), api_total)
    return WindowResult(events, api_total, raw_rows, False)


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def day_start_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def chunks(start_ms: int, end_ms: int, size_ms: int):
    """Inclusive [start, end] split into inclusive, non-overlapping chunks."""
    cur = start_ms
    while cur <= end_ms:
        nxt = min(cur + size_ms - 1, end_ms)
        yield cur, nxt
        cur = nxt + 1


class Store:
    def __init__(self, dsn: str = DATABASE_URL):
        self.conn = psycopg.connect(dsn, autocommit=False)

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.conn.close()

    def migrate(self) -> None:
        with self.conn.transaction():
            self.conn.execute("SELECT pg_advisory_xact_lock(42)")
            self.conn.execute(SCHEMA_SQL.read_text())

    def start_run(self, job: str, start_ms: int | None = None, end_ms: int | None = None) -> int:
        with self.conn.transaction():
            row = self.conn.execute(
                "INSERT INTO collector_runs (job, window_start, window_end) VALUES (%s, to_timestamp(%s / 1000.0), to_timestamp(%s / 1000.0)) RETURNING id",
                (job, start_ms, end_ms),
            ).fetchone()
        return row[0]

    def finish_run(self, run_id: int, status: str, fetched: int | None = None, new: int | None = None, error: str | None = None) -> None:
        self.conn.rollback()
        with self.conn.transaction():
            self.conn.execute(
                "UPDATE collector_runs SET finished_at = now(), status = %s, rows_fetched = %s, rows_new = %s, error = %s WHERE id = %s",
                (status, fetched, new, error, run_id),
            )

    def watermark(self, source: str) -> int | None:
        row = self.conn.execute(
            "SELECT (extract(epoch FROM complete_through) * 1000)::bigint FROM sync_watermarks WHERE source = %s", (source,)
        ).fetchone()
        self.conn.rollback()
        return row[0] if row else None

    def advance_watermark(self, source: str, through_ms: int) -> None:
        with self.conn.transaction():
            self.conn.execute(
                """INSERT INTO sync_watermarks (source, complete_through) VALUES (%s, to_timestamp(%s / 1000.0))
                   ON CONFLICT (source) DO UPDATE
                   SET complete_through = greatest(sync_watermarks.complete_through, EXCLUDED.complete_through), updated_at = now()""",
                (source, through_ms),
            )

    def upsert_usage(self, result: WindowResult, start_ms: int, end_ms: int) -> tuple[int, int]:
        """Returns (new_rows, stale_rows_deleted). Existing rows take the latest payload,
        so chargedCents that grew since the last pull overwrite the old value."""
        with self.conn.transaction():
            self.conn.execute("CREATE TEMP TABLE IF NOT EXISTS incoming (event_key text PRIMARY KEY, raw jsonb) ON COMMIT DELETE ROWS")
            with self.conn.cursor().copy("COPY incoming (event_key, raw) FROM STDIN") as cp:
                for key, ev in result.events.items():
                    cp.write_row((key, json.dumps(ev)))
            new = self.conn.execute(
                "SELECT count(*) FROM incoming i WHERE NOT EXISTS (SELECT 1 FROM usage_events u WHERE u.event_key = i.event_key)"
            ).fetchone()[0]
            self.conn.execute(
                """INSERT INTO usage_events (event_key, raw) SELECT event_key, raw FROM incoming
                   ON CONFLICT (event_key) DO UPDATE SET raw = EXCLUDED.raw, last_seen_at = now()"""
            )
            deleted = 0
            if result.complete:
                deleted = self.conn.execute(
                    """DELETE FROM usage_events u
                       WHERE u.ts BETWEEN to_timestamp(%s / 1000.0) AND to_timestamp(%s / 1000.0)
                         AND NOT EXISTS (SELECT 1 FROM incoming i WHERE i.event_key = u.event_key)""",
                    (start_ms, end_ms),
                ).rowcount
        return new, deleted

    def replace_members(self, members: list[dict]) -> None:
        """Panels join members by email, so keep one row per email and let the current member win."""
        by_email = {m["email"].lower(): m for m in sorted(members, key=lambda m: not m.get("isRemoved"))}
        with self.conn.transaction():
            self.conn.execute("DELETE FROM members")
            with self.conn.cursor().copy("COPY members (id, email, name, role, is_removed, raw) FROM STDIN") as cp:
                for m in by_email.values():
                    cp.write_row((m["id"], m["email"].lower(), m.get("name"), m.get("role"), m.get("isRemoved"), json.dumps(m)))

    def replace_directory_groups(self, groups: list[tuple[dict, list[dict]]]) -> None:
        with self.conn.transaction():
            self.conn.execute("DELETE FROM directory_groups")
            for group, members in groups:
                self.conn.execute(
                    "INSERT INTO directory_groups (id, name, raw) VALUES (%s, %s, %s)",
                    (group["id"], group.get("name"), json.dumps(group)),
                )
                for m in members:
                    self.conn.execute(
                        "INSERT INTO directory_group_members (group_id, email, raw) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                        (group["id"], m["email"].lower(), json.dumps(m)),
                    )

    def upsert_bugbot(self, reviews: list[dict]) -> int:
        with self.conn.transaction():
            for r in reviews:
                key = r.get("request_id") or hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest()
                self.conn.execute(
                    """INSERT INTO bugbot_reviews (request_id, raw, ts) VALUES (%s, %s, %s)
                       ON CONFLICT (request_id) DO UPDATE SET raw = EXCLUDED.raw, ts = EXCLUDED.ts, last_seen_at = now()""",
                    (key, json.dumps(r), r["timestamp"]),
                )
        return len(reviews)


def sync_usage(api: CursorApi, store: Store, start_ms: int, end_ms: int, job: str, watermark: str | None = None) -> str:
    """With a watermark source, it advances after each complete chunk and the pass stops at the
    first incomplete one, so the next pass resumes there instead of skipping a gap. Without one
    (backfill) it reads every chunk and the run ends incomplete if any chunk was."""
    run_id = store.start_run(job, start_ms, end_ms)
    fetched = new = 0
    status = "ok"
    try:
        for a, b in chunks(start_ms, end_ms, CHUNK_HOURS * HOUR_MS):
            result = fetch_usage_window(api, a, b)
            n, deleted = store.upsert_usage(result, a, b)
            fetched += len(result.events)
            new += n
            log.info(
                "usage %s..%s rows=%d unique=%d api_total=%s complete=%s new=%d stale_deleted=%d",
                iso(a), iso(b), result.raw_rows, len(result.events), result.api_total, result.complete, n, deleted,
            )
            if not result.complete:
                log.warning("usage %s..%s incomplete after retries", iso(a), iso(b))
                status = "incomplete"
                if watermark:
                    break
            elif watermark:
                store.advance_watermark(watermark, b)
        store.finish_run(run_id, status, fetched, new)
        return status
    except Exception as exc:
        store.finish_run(run_id, "error", fetched, new, str(exc)[:500])
        raise


def paged(fetch, items_key: str) -> list[dict]:
    out, page = [], 1
    while True:
        data = fetch(page)
        out.extend(data.get(items_key) or [])
        if not (data.get("pagination") or {}).get("hasNextPage"):
            return out
        page += 1


def sync_members(api: CursorApi, store: Store) -> list[dict]:
    members = api.get("/teams/members").get("teamMembers") or []
    store.replace_members(members)
    log.info("members loaded=%d active=%d", len(members), sum(not m.get("isRemoved") for m in members))
    return members


def sync_directory_groups(api: CursorApi, store: Store) -> None:
    groups = paged(lambda p: api.get("/teams/directory-groups", page=p, pageSize=200), "groups")
    with_members = [
        (g, paged(lambda p, gid=g["id"]: api.get(f"/teams/directory-groups/{gid}/members", page=p, pageSize=200), "members"))
        for g in groups
    ]
    store.replace_directory_groups(with_members)
    log.info("directory groups=%d members=%d", len(groups), sum(len(m) for _, m in with_members))


def sync_bugbot(api: CursorApi, store: Store, start: date, end: date) -> None:
    run_id = store.start_run("bugbot", day_start_ms(start), day_start_ms(end) + DAY_MS - 1)
    total = 0
    try:
        cur = start
        while cur <= end:
            last = min(cur + timedelta(days=29), end)
            reviews = paged(
                lambda p, a=cur, b=last: api.get(
                    "/analytics/team/bugbot-reviews", startDate=a.isoformat(), endDate=b.isoformat(), page=p, pageSize=250
                ),
                "data",
            )
            total += store.upsert_bugbot(reviews)
            log.info("bugbot %s..%s reviews=%d", cur, last, len(reviews))
            cur = last + timedelta(days=1)
        store.finish_run(run_id, "ok", total, None)
    except Exception as exc:
        store.finish_run(run_id, "error", total, None, str(exc)[:500])
        raise


def scheduled_sync(api: CursorApi, store: Store) -> None:
    before = Counter(api.requests)
    try:
        _scheduled_sync(api, store)
    finally:
        used = api.requests - before
        log.info("pass requests total=%d %s", sum(used.values()), " ".join(f"{k}={v}" for k, v in sorted(used.items())))


def _scheduled_sync(api: CursorApi, store: Store) -> None:
    now_ms = int(time.time() * 1000)
    sync_members(api, store)
    sync_directory_groups(api, store)
    sync_usage_incremental(api, store, now_ms)
    sync_bugbot_incremental(api, store, datetime.now(timezone.utc).date())


def sync_usage_incremental(api: CursorApi, store: Store, now_ms: int) -> None:
    """From the last complete window end minus OVERLAP_HOURS up to now. A gap of any length
    (laptop asleep, collector down) is caught up in CHUNK_HOURS chunks on the next pass."""
    wm = store.watermark("usage")
    start = wm - int(OVERLAP_HOURS * HOUR_MS) if wm is not None else now_ms - INITIAL_LOOKBACK_HOURS * HOUR_MS
    sync_usage(api, store, start, now_ms, "usage-scheduled", watermark="usage")


def sync_bugbot_incremental(api: CursorApi, store: Store, today: date) -> None:
    wm = store.watermark("bugbot")
    start = (datetime.fromtimestamp(wm / 1000, tz=timezone.utc).date() - timedelta(days=1)) if wm is not None \
        else today - timedelta(days=BUGBOT_LOOKBACK_DAYS)
    sync_bugbot(api, store, start, today)
    store.advance_watermark("bugbot", day_start_ms(today))


RECONCILE_SQL = """
WITH zones AS (
  SELECT DISTINCT tz FROM (VALUES (current_setting('TimeZone')), ('UTC')) t(tz)
), bounds AS (
  SELECT tz,
         (%(start)s::date)::timestamp AT TIME ZONE tz AS lo,
         (%(end)s::date + 1)::timestamp AT TIME ZONE tz AS hi
  FROM zones
)
SELECT b.tz,
       count(*) AS events,
       round(sum(e.charged_cents) / 100, 2) AS total_usd,
       round(coalesce(sum(e.charged_cents) FILTER (WHERE e.cloud_agent_id IS NOT NULL), 0) / 100, 2) AS cloud_agent_usd,
       round(coalesce(sum(e.charged_cents) FILTER (WHERE e.service_account_id IS NOT NULL), 0) / 100, 2) AS service_account_usd
FROM bounds b JOIN usage_events e ON e.ts >= b.lo AND e.ts < b.hi
GROUP BY b.tz ORDER BY b.tz = 'UTC', b.tz
"""


def reconcile(store: Store, start: date, end: date) -> None:
    rows = store.conn.execute(RECONCILE_SQL, {"start": start, "end": end}).fetchall()
    print("tz | events | total_usd | cloud_agent_usd | service_account_usd")
    for r in rows:
        print(" | ".join(str(x) for x in r))


def run_pass(api: CursorApi) -> None:
    """A fresh connection per pass, so a Postgres restart costs at most one pass."""
    with Store() as store:
        store.migrate()
        scheduled_sync(api, store)


def main(argv: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    if not argv:
        raise SystemExit(__doc__)
    cmd, args = argv[0], argv[1:]
    key = os.environ.get("CURSOR_TEAM_ADMIN_API_KEY", "")

    if cmd == "serve":
        api = CursorApi(key)
        while True:
            started = time.monotonic()
            try:
                run_pass(api)
            except Exception:
                log.exception("scheduled sync failed; next attempt in %ds", SYNC_INTERVAL_SECONDS)
            time.sleep(max(60, SYNC_INTERVAL_SECONDS - (time.monotonic() - started)))
    if cmd == "sync":
        run_pass(CursorApi(key))
        return
    with Store() as store:
        store.migrate()
        if cmd == "backfill":
            api = CursorApi(key)
            start, end = date.fromisoformat(args[0]), date.fromisoformat(args[1])
            status = sync_usage(api, store, day_start_ms(start), day_start_ms(end) + DAY_MS - 1, "usage-backfill")
            sync_bugbot(api, store, start, end)
            if status != "ok":
                raise SystemExit("backfill incomplete: some windows returned fewer events than the API's count; run it again")
        elif cmd == "reconcile":
            reconcile(store, date.fromisoformat(args[0]), date.fromisoformat(args[1]))
        else:
            raise SystemExit(f"unknown command {cmd}\n{__doc__}")


if __name__ == "__main__":
    main(sys.argv[1:])
