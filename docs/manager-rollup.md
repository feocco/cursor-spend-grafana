# Add a manager rollup from an org chart CSV

The dashboard groups people by the team groups the Cursor API returns. If you also want each manager's spend to include everyone below them at every level, load an org chart into Postgres and add one panel. The collector needs no changes.

1. Export an org chart as `org_chart.csv` with two columns. Leave `manager_email` empty for the person at the top. Every manager needs a row of their own.

	```csv
	email,manager_email
	ceo@example.com,
	vp@example.com,ceo@example.com
	dev@example.com,vp@example.com
	```

2. Create the table and the recursive view. `org_closure` pairs every person with themselves and with every manager above them. `UNION` instead of `UNION ALL` makes the recursion stop even if the CSV has a reporting cycle.

	```bash
	docker compose exec -T postgres psql -U spend spend <<'SQL'
	CREATE TABLE IF NOT EXISTS org_chart (email text PRIMARY KEY, manager_email text);
	CREATE OR REPLACE VIEW org_closure AS
	WITH RECURSIVE closure (manager_email, member_email) AS (
	  SELECT email, email FROM org_chart
	  UNION
	  SELECT c.manager_email, o.email FROM closure c JOIN org_chart o ON o.manager_email = c.member_email
	)
	SELECT manager_email, member_email FROM closure;
	GRANT SELECT ON org_chart, org_closure TO grafana_ro;
	SQL
	```

3. Load the CSV. Emails must be lowercase to match usage events. Run the same command again whenever the org chart changes.

	```bash
	docker compose exec -T postgres psql -1 -U spend spend \
	  -c "TRUNCATE org_chart" -c "\copy org_chart FROM pstdin WITH (FORMAT csv, HEADER)" < org_chart.csv
	```

4. Add the panel to `grafana/build_dashboard.py`, after "Spend per team group", then run `python3 grafana/build_dashboard.py`.

	```python
	table("Manager rollup", f"""
	WITH person AS (SELECT user_email, sum(charged_cents) / 100 AS usd FROM usage_events WHERE {TF} AND {HUMAN} GROUP BY 1)
	SELECT coalesce(nullif(m.name, ''), oc.manager_email) AS "Manager", count(*) - 1 AS "People under",
	       round(sum(coalesce(p.usd, 0)), 2) AS "USD"
	FROM org_closure oc
	LEFT JOIN person p ON p.user_email = oc.member_email
	LEFT JOIN members m ON m.email = oc.manager_email
	WHERE oc.manager_email IN (SELECT manager_email FROM org_chart)
	GROUP BY oc.manager_email, m.name ORDER BY 3 DESC""", 12, 12,
	      "Each manager's own spend plus everyone below them in org_chart. People missing from the CSV are in no rollup.")
	```

A manager's USD equals their own spend plus the USD of each direct report. Service accounts are never in a rollup.
