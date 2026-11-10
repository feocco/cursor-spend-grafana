# Cursor spend in Grafana

> **Unofficial example.** This project is an educational example of using Cursor's Admin and Analytics APIs. It is not an official Cursor product and not a chargeback or billing tool. Figures are estimates and can differ from Cursor's billing dashboard and invoices, for example because of plan-included usage or time zone boundaries. Verify them against Cursor's official billing before making any financial decision. The code is provided as is, without warranty or support.

A Grafana dashboard of Cursor team spend: per person, per team group, per service account, per automation, and split by cloud agent. A collector pulls the Cursor Admin and Analytics APIs every hour into Postgres, so history survives beyond what the APIs return in one call.

You need Docker and one Team Admin API key.

## Run it

1. Copy the example settings and fill in the two required values.

	```bash
	cp .env.example .env
	```

	`CURSOR_TEAM_ADMIN_API_KEY` is a Team Admin key from the Cursor dashboard. `TIMEZONE` is an IANA name such as `America/Los_Angeles`. It sets where each day starts on the dashboard.

2. Start the stack.

	```bash
	docker compose up -d --build
	```

3. Open http://127.0.0.1:3300 and sign in as `admin` with password `admin`, or with `GRAFANA_ADMIN_PASSWORD` if you set it.

Keep ports 3300 and 15432 bound to 127.0.0.1, as `docker-compose.yml` has them, and don't expose them to a network. The default passwords are only safe on your own machine: Grafana's `admin`, Postgres's `spend` superuser, and the read-only `grafana_ro` role can each read every person's email and spend. Before the first start on a shared host, set `GRAFANA_ADMIN_PASSWORD` in `.env`, and change the Postgres passwords: `POSTGRES_PASSWORD` in `docker-compose.yml` with a matching `DATABASE_URL` in `.env`, and `grafana_ro` in `db/schema.sql` and `grafana/provisioning/datasources/datasources.yml`.

The first pass reads the last 48 hours and takes a few minutes. To load older history, run a backfill for a range of UTC days, inclusive:

```bash
docker compose run --rm collector backfill 2026-10-01 2026-10-31
```

## What runs

There are three services. `postgres` stores everything in the `pgdata` volume. `collector` runs one pass at start and then one every hour. `grafana` serves the dashboard, which it loads from files in `grafana/`.

Each pass does these steps in order. Every step is safe to re-run.

1. `GET /teams/members` replaces the member list.
2. `GET /teams/directory-groups` and each group's members replace the team groups. An empty list is fine.
3. `POST /teams/filtered-usage-events` reads usage events from the watermark minus 2 hours up to now, in 6-hour chunks.
4. `GET /analytics/team/bugbot-reviews` reads Bugbot reviews from the day before the last pass through today. They give review counts per repo. Bugbot spend comes from the usage events.

The watermark is the end of the last chunk the collector read completely. A chunk is complete when its de-duplicated event count equals the API's `totalUsageEventsCount`. The watermark never moves past an incomplete chunk, so a collector that was down for days catches up on its next pass. The 2-hour overlap picks up charges that grew after the last read.

Events are keyed by `usageEventId`, or by a hash of their unchanging fields when the API omits it. A re-read overwrites the stored event, so a grown charge updates its row instead of adding one. Once a chunk is complete, the collector deletes stored events in it that the API no longer returns.

The collector spaces its calls at 75% of the API rate limits, for example 45 usage requests a minute against a limit of 60. On a 429 it waits for `Retry-After`. The limits are per team, so other scripts using the same team's key slow it down. A backfill reads up to 1,000 events per request, so at 45 requests a minute it loads at most about 2.7 million events an hour.

## Operate it

| Task | Command |
| --- | --- |
| Check health | `dev/doctor.sh`, or the dashboard's "Data age" stat. Above 70 minutes means the collector is behind. |
| Run a pass now | `docker compose run --rm collector sync` |
| Load or repair a range | `docker compose run --rm collector backfill START END` |
| Compare totals with the Cursor dashboard | `docker compose run --rm collector reconcile START END` |
| Rotate the key | Edit `.env`, then run `docker compose up -d`. |
| Back up | `docker compose exec -T postgres pg_dump -U spend spend > spend-$(date +%F).sql` |

`docker compose down -v` deletes the history volume. To stop the stack, run `docker compose stop`.

## Know the limits

- Usage events don't say which repo a cloud agent or IDE request worked on, so that spend can't be split by repo. Bugbot usage events do carry the repo, PR number, and PR author, so "Bugbot spend by repo" and "Bugbot spend by PR author" sum their charges. The PR author is a Git host login, not an email.
- Cursor-managed automations, such as the security reviewer, get their own slice in "Spend by category", keyed on `automationManagedType`. Security reviewer events carry no repo, PR, cloud agent, automation, or person, so its spend can only be totaled. See [docs/attribution.md](docs/attribution.md) for the API calls and response fields behind both.
- The API returns automation IDs, not names.
- Team groups use today's membership. Someone in two groups counts in both.
- To add a manager rollup from an org chart CSV, see [docs/manager-rollup.md](docs/manager-rollup.md).

## Change it

- Panel SQL lives in `grafana/build_dashboard.py`. Edit it, then run `python3 grafana/build_dashboard.py`. Grafana reloads the generated `grafana/dashboards/cursor-spend.json` within 10 seconds.
- The schema is `db/schema.sql`. The collector applies it on every pass, so every statement must be safe to re-run.
- `dev/` holds the checks. `dev/test_collector.py` runs the collector against a fake API with synthetic data and a scratch database:

	```bash
	docker compose run --rm -v ./dev:/app/dev --entrypoint python collector dev/test_collector.py
	```

	`python3 dev/check_panels.py START END` runs every panel's query through Grafana and fails if any panel errors.

## License

MIT. See [LICENSE](LICENSE).
