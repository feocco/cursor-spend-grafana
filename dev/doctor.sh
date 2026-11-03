#!/usr/bin/env bash
# Read-only health check. Exits 1 if a container is down, Grafana or the database doesn't answer, or the API key is rejected.
set -uo pipefail
cd "$(dirname "$0")/.."
ok=0
say() { printf '%-20s %s\n' "$1" "$2"; }

docker info >/dev/null 2>&1 && say docker up || { say docker "DOWN (start Docker)"; exit 1; }
for s in postgres collector grafana; do
  st=$(docker compose ps --format '{{.State}}' "$s" 2>/dev/null)
  say "$s" "${st:-not created}"; [[ "$st" == running ]] || ok=1
done
health=$(curl -sf -m 5 "${GRAFANA_URL:-http://127.0.0.1:3300}/api/health" | tr -d '\n ') || { health=unreachable; ok=1; }
say grafana_health "$health"
key=$(sed -n 's/^CURSOR_TEAM_ADMIN_API_KEY=//p' .env 2>/dev/null)
code=$(curl -s -o /dev/null -w '%{http_code}' -m 15 -K - https://api.cursor.com/teams/members <<<"user = \"$key:\"")
say api_key "HTTP $code$([[ $code == 200 ]] || echo ' (key missing or invalid)')"; [[ $code == 200 ]] || ok=1
docker compose exec -T postgres psql -U spend -d spend -tA -F ' | ' -c \
  "select 'time zone', current_setting('TimeZone'), null
   union all select 'events', count(*)::text, min(ts)::date || '..' || max(ts)::date from usage_events
   union all select 'last ok pass', max(finished_at)::text, null from collector_runs where status = 'ok' and job = 'usage-scheduled'" \
  | sed 's/^/db: /' || { say db "query failed"; ok=1; }
exit $ok
