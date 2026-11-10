# Attribute Bugbot and security reviewer spend

This page explains which Cursor API calls tie review spend to a repo or a person, and what the dashboard does with them. It covers Bugbot and the security reviewer, one of Cursor's managed security agents.

| | Spend by repo | Spend by PR author or pusher |
| --- | --- | --- |
| Bugbot | Yes, from the Analytics API | No |
| Security reviewer | Not from the API today. One agent per repo is a workaround. | No |

Every command below authenticates with HTTP Basic auth: the API key is the user name and the password is empty. Keep keys in environment variables and out of shell history, logs, and tickets. Analytics API endpoints and service accounts may need an Enterprise plan.

## Bugbot: spend by repo

The "Bugbot reviews by repo (Analytics API)" panel groups Bugbot reviews by repo. Its data comes from one call, which takes up to 30 days per request:

```bash
curl -sS -u "$CURSOR_ADMIN_API_KEY:" \
  "https://api.cursor.com/analytics/team/bugbot-reviews?startDate=2026-11-01&endDate=2026-11-07&page=1&pageSize=250"
```

Each review names its repo and PR. The sample below has fake values and shows only the fields this project reads. Real responses can include more.

```json
{
  "data": [
    {
      "request_id": "rv-00000000-0000-0000-0000-000000000001",
      "timestamp": "2026-11-02T15:04:05.000Z",
      "repo": "github.com/example-org/example-repo",
      "pr_number": 123,
      "dry_run": false,
      "billed": true,
      "cost_cents": 40
    }
  ],
  "pagination": { "hasNextPage": false }
}
```

`cost_cents` is the list cost of the review. Only reviews with `billed: true` reach the invoice. On plans that include Bugbot, Bugbot usage events are charged $0, so the per-repo cost comes from this endpoint rather than from usage events.

## Security reviewer: spend by agent, not yet by repo

The security reviewer runs on Cursor's Automations platform as a cloud agent. Its usage is billed to the team's usage pool, and it runs as a service account the team shares. Its spend therefore shows up in usage events under that service account, not under the person who opened or pushed the PR.

The dashboard reads usage events from this call. Timestamps are epoch milliseconds and both bounds are inclusive:

```bash
curl -sS -u "$CURSOR_ADMIN_API_KEY:" -X POST "https://api.cursor.com/teams/filtered-usage-events" \
  -H "Content-Type: application/json" \
  -d '{"startDate": 1793491200000, "endDate": 1794095999999, "page": 1, "pageSize": 1000}'
```

Events from Cursor-managed automations carry `automationManagedType`, along with `automationId` and `cloudAgentId`. The sample below has fake values, and the `automationManagedType` value is illustrative, so check the value your own events return.

```json
{
  "usageEvents": [
    {
      "timestamp": "1793548800000",
      "serviceAccountId": "sa_00000000",
      "serviceAccountName": "Example Automations Account",
      "cloudAgentId": "bc-00000000-0000-0000-0000-000000000001",
      "automationId": "00000000-0000-0000-0000-000000000002",
      "automationManagedType": "SECURITY_REVIEWER",
      "model": "example-model",
      "kind": "Usage-based",
      "isHeadless": true,
      "chargedCents": 123.45
    }
  ],
  "totalUsageEventsCount": 1,
  "pagination": { "currentPage": 1, "hasNextPage": false }
}
```

On the dashboard:

- "Spend by category" and "Daily spend by category" give each `automationManagedType` its own slice, such as "Managed: security reviewer". Events that also carry a `cloudAgentId` still count toward "Cloud agent spend".
- "Spend per automation" splits that spend by `automationId`, so each security agent you created gets its own row. The API returns the automation's UUID, not its name, so match the UUIDs to your agents yourself.

### Why there is no repo or user column

The documented usage event fields include no repo, PR, or PR author. Cursor's public APIs offer no security review endpoint like `bugbot-reviews`, and no Automations API that returns an agent's repo scope.

The security reviewer runs as the team's service account, so spend can't be split by the person who opened or pushed the PR. That person shows up in your Git host's data, not in Cursor's usage data.

### Workaround: one security agent per repo

To get spend by repo today, create a separate security reviewer agent for each repo and scope its trigger to that one repo. Each agent then has its own `automationId`, and "Spend per automation" becomes spend per repo once you map each UUID to its repo. This mapping stays correct only while each agent covers exactly one repo.

### Unverified: looking up the cloud agent

The Cloud Agents API returns an agent's repos, and a run's pushed branches and PR URLs. It is documented to accept a user API key or a service account API key, and the Admin API key isn't listed:

```bash
curl -sS -u "$CURSOR_API_KEY:" "https://api.cursor.com/v1/agents/bc-00000000-0000-0000-0000-000000000001"
```

The documented response includes `repos` (a list of `{ "url": "https://github.com/example-org/example-repo", "startingRef": "main" }`) and `latestRunId`. `GET /v1/agents/{id}/runs/{runId}` adds `git.branches[]` with `repoUrl` and `prUrl`. We haven't confirmed that this works for security reviewer agents, for three reasons:

- They run as the team's managed service account, and these endpoints are documented to return the caller's own agents.
- A reviewer that only comments on a PR pushes no branch, so `git.branches` is likely empty.
- If the security agents page shows no codebase for a run, expect the agent record to have no repo either.

Try it against one `cloudAgentId` from your usage events before building on it.
