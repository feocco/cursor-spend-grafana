-- Applied by the collector on every start. Every statement must be safe to re-run.

CREATE TABLE IF NOT EXISTS usage_events (
  event_key            text PRIMARY KEY,
  raw                  jsonb NOT NULL,
  ts                   timestamptz GENERATED ALWAYS AS (to_timestamp((raw->>'timestamp')::bigint / 1000.0)) STORED,
  user_email           text GENERATED ALWAYS AS (lower(raw->>'userEmail')) STORED,
  -- The API sends the string "null" for absent ids on human events.
  service_account_id   text GENERATED ALWAYS AS (nullif(nullif(raw->>'serviceAccountId', 'null'), '')) STORED,
  service_account_name text GENERATED ALWAYS AS (nullif(nullif(raw->>'serviceAccountName', 'null'), '')) STORED,
  cloud_agent_id       text GENERATED ALWAYS AS (nullif(nullif(raw->>'cloudAgentId', 'null'), '')) STORED,
  automation_id        text GENERATED ALWAYS AS (nullif(nullif(raw->>'automationId', 'null'), '')) STORED,
  conversation_id      text GENERATED ALWAYS AS (nullif(nullif(raw->>'conversationId', 'null'), '')) STORED,
  repo                 text GENERATED ALWAYS AS (nullif(nullif(raw->>'repo', 'null'), '')) STORED,
  model                text GENERATED ALWAYS AS (raw->>'model') STORED,
  kind                 text GENERATED ALWAYS AS (raw->>'kind') STORED,
  is_chargeable        boolean GENERATED ALWAYS AS ((raw->>'isChargeable')::boolean) STORED,
  is_headless          boolean GENERATED ALWAYS AS ((raw->>'isHeadless')::boolean) STORED,
  charged_cents        numeric GENERATED ALWAYS AS (coalesce((raw->>'chargedCents')::numeric, 0)) STORED,
  first_seen_at        timestamptz NOT NULL DEFAULT now(),
  last_seen_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS usage_events_ts_idx ON usage_events (ts);
CREATE INDEX IF NOT EXISTS usage_events_cloud_agent_idx ON usage_events (cloud_agent_id) WHERE cloud_agent_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS members (
  id         text PRIMARY KEY,
  email      text NOT NULL,
  name       text,
  role       text,
  is_removed boolean,
  raw        jsonb NOT NULL,
  loaded_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS directory_groups (
  id         text PRIMARY KEY,
  name       text,
  raw        jsonb NOT NULL,
  loaded_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS directory_group_members (
  group_id   text NOT NULL REFERENCES directory_groups (id) ON DELETE CASCADE,
  email      text NOT NULL,
  raw        jsonb NOT NULL,
  PRIMARY KEY (group_id, email)
);

CREATE TABLE IF NOT EXISTS bugbot_reviews (
  request_id   text PRIMARY KEY,
  raw          jsonb NOT NULL,
  ts           timestamptz NOT NULL,
  repo         text GENERATED ALWAYS AS (raw->>'repo') STORED,
  pr_number    integer GENERATED ALWAYS AS ((raw->>'pr_number')::integer) STORED,
  dry_run      boolean GENERATED ALWAYS AS ((raw->>'dry_run')::boolean) STORED,
  billed       boolean GENERATED ALWAYS AS ((raw->>'billed')::boolean) STORED,
  cost_cents   numeric GENERATED ALWAYS AS ((raw->>'cost_cents')::numeric) STORED,
  last_seen_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS bugbot_reviews_ts_idx ON bugbot_reviews (ts);

CREATE TABLE IF NOT EXISTS collector_runs (
  id            bigserial PRIMARY KEY,
  job           text NOT NULL,
  started_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz,
  window_start  timestamptz,
  window_end    timestamptz,
  rows_fetched  integer,
  rows_new      integer,
  status        text NOT NULL DEFAULT 'running',
  error         text
);

-- Scheduled passes resume from complete_through minus an overlap. It only moves forward,
-- and only past chunks that were read completely. Rewind it by hand to force a re-read.
CREATE TABLE IF NOT EXISTS sync_watermarks (
  source           text PRIMARY KEY,
  complete_through timestamptz NOT NULL,
  updated_at       timestamptz NOT NULL DEFAULT now()
);

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_ro') THEN
    CREATE ROLE grafana_ro LOGIN PASSWORD 'grafana_ro';
  END IF;
END $$;
GRANT USAGE ON SCHEMA public TO grafana_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana_ro;
