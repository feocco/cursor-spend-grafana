"""Runs every dashboard panel query through Grafana's /api/ds/query and prints its row count and first row.
Exits 1 if any panel errors. START and END are inclusive days in TIMEZONE, the zone the dashboard uses.

    python3 dev/check_panels.py 2026-10-25 2026-10-30
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

GRAFANA = os.environ.get("GRAFANA_URL", "http://127.0.0.1:3300")
AUTH = base64.b64encode(f"admin:{os.environ.get('GRAFANA_ADMIN_PASSWORD', 'admin')}".encode()).decode()
DASH = json.loads((Path(__file__).resolve().parent.parent / "grafana" / "dashboards" / "cursor-spend.json").read_text())


def query(target: dict, start_ms: int, end_ms: int) -> tuple[str | None, list[dict]]:
    body = {"from": str(start_ms), "to": str(end_ms), "queries": [target]}
    req = urllib.request.Request(f"{GRAFANA}/api/ds/query", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Basic {AUTH}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            res = json.load(resp)
    except urllib.error.HTTPError as exc:
        res = json.loads(exc.read() or b"{}")
    result = (res.get("results") or {}).get(target["refId"], {})
    rows = []
    for frame in result.get("frames") or []:
        names = [f["name"] for f in frame["schema"]["fields"]]
        rows += [dict(zip(names, vals)) for vals in zip(*(frame.get("data") or {}).get("values") or [])]
    return result.get("error") or res.get("message"), rows


def dashboard_tz() -> str:
    pg = next(t["datasource"] for p in DASH["panels"] for t in p["targets"])
    err, rows = query({"refId": "tz", "datasource": pg, "rawQuery": True, "format": "table", "rawSql": "SELECT current_setting('TimeZone')"}, 0, 0)
    if err or not rows:
        raise SystemExit(f"could not read the dashboard time zone: {err}")
    return next(iter(rows[0].values()))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("start", type=date.fromisoformat)
    ap.add_argument("end", type=date.fromisoformat)
    args = ap.parse_args()
    tz = dashboard_tz()
    zone = ZoneInfo(tz)
    start_ms = int(datetime.combine(args.start, time(), zone).timestamp() * 1000)
    end_ms = int(datetime.combine(args.end + timedelta(days=1), time(), zone).timestamp() * 1000) - 1
    print(f"{args.start}..{args.end} on {tz} days")
    failures = 0
    for panel in DASH["panels"]:
        for target in panel["targets"]:
            err, rows = query(target, start_ms, end_ms)
            failures += bool(err)
            first = json.dumps(rows[0], default=str)[:160] if rows else ""
            print(f"{'ERROR' if err else 'ok':5} {panel['title'][:45]:45} rows={len(rows):<5} {err or first}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
