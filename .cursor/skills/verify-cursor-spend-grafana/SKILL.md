---
name: verify-cursor-spend-grafana
description: Drive and prove the Cursor spend Grafana stack (Postgres, an hourly collector, provisioned Grafana on 127.0.0.1:3300). Use before claiming a change to the collector, schema, or dashboard works, and when reconciling spend against the Cursor dashboard.
---

# Verify cursor-spend-grafana

The user sees the Grafana dashboard at http://127.0.0.1:3300/d/cursor-spend. Postgres feeds it, and `collector/collector.py` fills Postgres from the Cursor Admin and Analytics APIs. Panel SQL lives in `grafana/build_dashboard.py`, never in the generated JSON.

## Launch

Run `docker compose up -d --build`. The stack is ready when `dev/doctor.sh` exits 0 and `collector_runs` has a `usage-scheduled` row with `status = 'ok'`. The first pass takes a few minutes. If doctor reports the API key as anything but HTTP 200, every live check below is invalid, so stop and ask for a key.

To test without touching a running stack, use another project name and other ports, for example `docker compose -p spend-check -f docker-compose.yml -f override.yml`, where `override.yml` sets `ports: !override` on `postgres` and `grafana`.

## Drive

| What | Command | Proves |
| --- | --- | --- |
| Collector logic | `docker compose run --rm -v ./dev:/app/dev --entrypoint python collector dev/test_collector.py` | Dedupe in both key modes, 429 handling, page drift, grown charges, the watermark, and recovery after Postgres drops connections. Uses database `spend_test` only. |
| Real data | `docker compose run --rm collector backfill START END` | Rows land, and `collector_runs` shows `ok`. |
| Totals | `docker compose run --rm collector reconcile START END` | Total, cloud agent, and service account USD on `TIMEZONE` days and on UTC days. |
| Every panel | `python3 dev/check_panels.py START END` | Each panel's SQL runs through Grafana's query API without error, on the dashboard's day boundary. |

START and END are inclusive dates for every command.

## Invariants worth checking

- The slices of "Spend by category" sum to "Total spend".
- "Cloud agent spend" equals the sum of the three "Cloud agent: ..." slices in "Spend by category" plus the "Managed: ..." events that carry a `cloudAgentId`. Security reviewer events carry none, so they sit only in their own "Managed: security reviewer" slice.
- "Bugbot spend by repo" and "Bugbot spend by PR author" each sum to the "Bugbot" slice of "Spend by category".
- `members` has one row per email. Panels join it by email.
- `select count(*) from usage_events where raw->>'serviceAccountId' = 'null' and service_account_id is not null` returns 0.

## Evidence

- Prove a number from the database (`reconcile`) and from Grafana (`check_panels.py`). A number seen in only one of the two is not proven.
- `dev/test_collector.py` uses synthetic data. Never present its numbers as team spend.
- `docker compose down -v` deletes the history volume. Use `docker compose stop`.
