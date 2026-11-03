"""Generates grafana/dashboards/cursor-spend.json. Edit panels here, then run:
    python3 grafana/build_dashboard.py
"""

import json
from pathlib import Path

PG = {"type": "grafana-postgresql-datasource", "uid": "spend-pg"}
HUMAN = "service_account_id IS NULL"
TF = "$__timeFilter(ts)"
# Postgres runs with timezone=$TIMEZONE, so this is local midnight, the same zone as the time picker.
DAY = "date_trunc('day', ts)"
CATEGORY = """CASE
    WHEN cloud_agent_id IS NOT NULL AND automation_id IS NOT NULL THEN 'Cloud agent: automation'
    WHEN cloud_agent_id IS NOT NULL AND service_account_id IS NOT NULL THEN 'Cloud agent: service account'
    WHEN cloud_agent_id IS NOT NULL THEN 'Cloud agent: person'
    WHEN model = 'github_bugbot' OR service_account_name = 'bugbot' THEN 'Bugbot'
    WHEN model LIKE 'grok-bot-%' THEN 'Grokbot'
    WHEN raw->>'automationManagedType' IS NOT NULL THEN 'Managed: ' || lower(replace(raw->>'automationManagedType', '_', ' '))
    WHEN service_account_id IS NOT NULL THEN 'Service account: non-cloud-agent'
    ELSE 'Local agent / IDE / CLI'
  END"""

DISCLAIMER = (
    "Unofficial example. This project is an educational example of using Cursor's Admin and Analytics APIs. "
    "It is not an official Cursor product and not a chargeback or billing tool. Figures are estimates and can differ from "
    "Cursor's billing dashboard and invoices, for example because of plan-included usage or time zone boundaries. Verify them "
    "against Cursor's official billing before making any financial decision. The code is provided as is, without warranty or support."
)
COLORS = {
    "Cloud agent: automation": "#f54e00",
    "Cloud agent: person": "#4e79a7",
    "Cloud agent: service account": "#edc948",
    "Bugbot": "#e040fb",
    "Grokbot": "#59a14f",
    "Service account: non-cloud-agent": "#76b7b2",
    "Local agent / IDE / CLI": "#9c755f",
    "Managed: vulnerability scanner": "#e15759",
    "Managed: security reviewer": "#212121",
    "Managed: change monitor": "#2f4b7c",
}


def color(matcher, name, hex_):
    return {"matcher": {"id": matcher, "options": name}, "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": hex_}}]}


# Managed: * labels come from the API, so a regex gives unknown managed types a color; later overrides win in Grafana.
CATEGORY_COLORS = [color("byRegexp", "^Managed: .*", "#bab0ac")] + [color("byName", k, v) for k, v in COLORS.items()]

panels = []


def add(kind, title, w, h, targets, description="", **extra):
    x = sum(p["gridPos"]["w"] for p in panels if p["gridPos"]["y"] == add.y)
    if x + w > 24:
        add.y += add.h
        x = 0
    add.h = h if x == 0 else max(add.h, h)
    panels.append({
        "id": len(panels) + 1, "type": kind, "title": title, "description": description,
        "gridPos": {"x": x, "y": add.y, "w": w, "h": h}, "targets": targets, **extra,
    })


add.y, add.h = 0, 0


def sql(query, fmt="table", ref="A"):
    return {"refId": ref, "datasource": PG, "rawQuery": True, "editorMode": "code", "format": fmt, "rawSql": query.strip()}


def usd(overrides=None, **custom):
    return {"defaults": {"unit": "currencyUSD", "decimals": 2, "custom": custom}, "overrides": overrides or []}


def stat(title, query, description="", unit="currencyUSD", w=5):
    add("stat", title, w, 4, [sql(query)], description,
        fieldConfig={"defaults": {"unit": unit, "decimals": 2 if unit == "currencyUSD" else 0, "color": {"mode": "fixed", "fixedColor": "#f54e00"}}, "overrides": []},
        options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": ""}, "colorMode": "value", "graphMode": "none", "textMode": "value"})


def table(title, query, w, h, description="", sort="USD"):
    money = {"matcher": {"id": "byRegexp", "options": ".*USD$"},
             "properties": [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 2}]}
    add("table", title, w, h, [sql(query)], description,
        fieldConfig={"defaults": {"custom": {}}, "overrides": [money]},
        options={"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": sort, "desc": True}] if sort else [], "footer": {"show": False}})


add("text", "", 24, 3, [], options={"mode": "markdown", "content": f"**{DISCLAIMER.replace('. ', '.** ', 1)}"})

stat("Total spend", f"SELECT sum(charged_cents) / 100 AS usd FROM usage_events WHERE {TF}",
     "Sum of chargedCents over all usage events in range. Usage included in the plan, such as Bugbot when the plan includes it, is charged $0 and counts as $0 here.")
stat("Cloud agent spend", f"SELECT sum(charged_cents) / 100 FROM usage_events WHERE {TF} AND cloud_agent_id IS NOT NULL",
     "Events carrying a cloudAgentId (includes automations, which run as cloud agents).")
stat("Service account spend", f"SELECT coalesce(sum(charged_cents), 0) / 100 FROM usage_events WHERE {TF} AND service_account_id IS NOT NULL")
stat("Events", f"SELECT count(*) FROM usage_events WHERE {TF}", unit="short")
stat("Data age (min)",
     "SELECT extract(epoch FROM now() - max(finished_at)) / 60 FROM collector_runs WHERE status = 'ok' AND job = 'usage-scheduled'",
     "Minutes since the last successful scheduled usage pass. The collector runs hourly; above ~70 means it is behind.", unit="short", w=4)

add("barchart", "Daily spend by category", 24, 8,
    [sql(f"SELECT {DAY} AS time, {CATEGORY} AS metric, sum(charged_cents) / 100 AS usd FROM usage_events WHERE {TF} GROUP BY 1, 2 ORDER BY 1", fmt="time_series")],
    "Days start at midnight in TIMEZONE from .env.",
    fieldConfig=usd(CATEGORY_COLORS),
    options={"stacking": "normal", "legend": {"showLegend": True, "displayMode": "list", "placement": "right"}, "xTickLabelRotation": 0, "showValue": "never"})

table("Spend per person", f"""
SELECT coalesce(nullif(m.name, ''), e.user_email) AS "Person", e.user_email AS "Email",
       round(sum(e.charged_cents) / 100, 2) AS "USD",
       round(sum(e.charged_cents) FILTER (WHERE e.cloud_agent_id IS NOT NULL) / 100, 2) AS "Cloud agent USD",
       count(*) AS "Events"
FROM usage_events e LEFT JOIN members m ON m.email = e.user_email
WHERE {TF.replace('ts', 'e.ts')} AND e.{HUMAN}
GROUP BY 1, 2 ORDER BY 3 DESC""", 12, 12, "Human users only. Automations that run as a person are included in that person's spend.")

table("Spend per team group", f"""
SELECT g.name AS "Group", count(gm.email) AS "Members", round(coalesce(sum(s.usd), 0), 2) AS "USD"
FROM directory_groups g LEFT JOIN directory_group_members gm ON gm.group_id = g.id
LEFT JOIN (SELECT user_email, sum(charged_cents) / 100 AS usd FROM usage_events WHERE {TF} AND {HUMAN} GROUP BY 1) s ON s.user_email = gm.email
GROUP BY g.id, g.name ORDER BY 3 DESC""", 12, 12,
      "Team groups from /teams/directory-groups (SCIM-synced or created in the dashboard), with today's membership. Someone in two groups counts in both, so the column can sum to more than total spend.")
panels[-1]["fieldConfig"]["defaults"]["noValue"] = "No team groups on this team"

table("Spend per service account", f"""
SELECT service_account_name AS "Service account", service_account_id AS "ID",
       round(sum(charged_cents) / 100, 2) AS "USD", count(DISTINCT automation_id) AS "Automations",
       count(DISTINCT cloud_agent_id) AS "Cloud agent runs", count(*) AS "Events"
FROM usage_events WHERE {TF} AND service_account_id IS NOT NULL
GROUP BY 1, 2 ORDER BY 3 DESC""", 12, 9)

table("Spend per automation", f"""
SELECT automation_id AS "Automation", coalesce(max(service_account_name), 'runs as ' || max(user_email)) AS "Runs as",
       round(sum(charged_cents) / 100, 2) AS "USD", count(DISTINCT cloud_agent_id) AS "Cloud agent runs", count(*) AS "Events"
FROM usage_events WHERE {TF} AND automation_id IS NOT NULL
GROUP BY 1 ORDER BY 3 DESC""", 12, 9,
      "The API exposes automation UUIDs only. Service-account automations get a dedicated service account, so its name identifies the automation.")

table("Bugbot reviews by repo (Analytics API)", f"""
SELECT repo AS "Repo", count(*) AS "Reviews", count(*) FILTER (WHERE billed) AS "Billed reviews",
       round(coalesce(sum(cost_cents), 0) / 100, 2) AS "Cost USD", round(coalesce(sum(cost_cents) FILTER (WHERE billed), 0) / 100, 2) AS "Billed USD"
FROM bugbot_reviews WHERE {TF}
GROUP BY 1 ORDER BY 2 DESC""", 12, 9,
      "From /analytics/team/bugbot-reviews. cost_cents is list cost; only billed reviews reach the invoice.", sort="Reviews")

add("piechart", "Spend by category", 12, 9,
    [sql(f"SELECT $__timeFrom()::timestamptz AS time, {CATEGORY} AS metric, sum(charged_cents) / 100 AS usd FROM usage_events WHERE {TF} GROUP BY 2 ORDER BY 3 DESC", fmt="time_series")],
    "All spend in range, each event in the first category it matches: a cloud agent run by an automation, by a service account, "
    "or by a person; Bugbot; Grokbot; a Cursor-managed automation outside a cloud agent, such as the vulnerability scanner; "
    "any other service account use; then a person's own requests in a local agent, the IDE, or the CLI.",
    fieldConfig=usd(CATEGORY_COLORS),
    options={"pieType": "donut", "legend": {"showLegend": True, "displayMode": "table", "placement": "right", "values": ["value", "percent"]},
             "reduceOptions": {"calcs": ["sum"], "fields": "", "values": False}})

table("Top cloud agent runs", f"""
SELECT cloud_agent_id AS "Cloud agent", coalesce(max(service_account_name), max(user_email)) AS "Launched by",
       max(automation_id) AS "Automation", round(sum(charged_cents) / 100, 2) AS "USD", count(*) AS "Events"
FROM usage_events WHERE {TF} AND cloud_agent_id IS NOT NULL
GROUP BY 1 ORDER BY 4 DESC LIMIT 50""", 24, 10)

table("Collector runs", """
SELECT started_at AS "Started", job AS "Job", status AS "Status", window_start AS "Window start", window_end AS "Window end",
       rows_fetched AS "Rows", rows_new AS "New", left(error, 120) AS "Error"
FROM collector_runs ORDER BY id DESC LIMIT 15""", 24, 8, sort=None)
panels[-1]["fieldConfig"] = {"defaults": {}, "overrides": []}

dashboard = {
    "uid": "cursor-spend", "title": "Cursor spend", "description": DISCLAIMER, "tags": ["cursor", "spend"],
    "timezone": "", "schemaVersion": 39, "version": 1, "editable": False,
    "refresh": "1h", "time": {"from": "now-7d", "to": "now"},
    "panels": panels,
}

out = Path(__file__).with_name("dashboards") / "cursor-spend.json"
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps(dashboard, indent=2) + "\n")
print(f"wrote {out} with {len(panels)} panels")
