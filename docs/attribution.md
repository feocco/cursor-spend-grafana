# Attribute Bugbot and security reviewer spend

This page explains which Cursor API calls tie review spend to a repo or a person, and what the dashboard does with them. It covers Bugbot and the security reviewer, one of Cursor's managed security agents.

| | Spend by repo | Spend by PR author |
| --- | --- | --- |
| Bugbot | Yes, from usage events | Yes, from usage events, as a Git host login |
| Security reviewer | No. It can only be totaled. | No. It can only be totaled. |

Every command below authenticates with HTTP Basic auth: the API key is the user name and the password is empty. Both commands worked as written with a team service account API key in place of `$CURSOR_ADMIN_API_KEY`. Keep keys in environment variables and out of shell history, logs, and tickets. Whether your team can use the Analytics API and service accounts depends on its plan.

## Bugbot

### Spend by repo and by PR author

Bugbot spend lives in usage events, which the dashboard reads from this call. Timestamps are epoch milliseconds and both bounds are inclusive:

```bash
curl -sS -u "$CURSOR_ADMIN_API_KEY:" -X POST "https://api.cursor.com/teams/filtered-usage-events" \
  -H "Content-Type: application/json" \
  -d '{"startDate": 1793491200000, "endDate": 1794095999999, "page": 1, "pageSize": 1000}'
```

Bugbot events have `model` set to `github_bugbot` and carry the repo, the PR number, the PR author, and the charge. The sample below has fake values and shows only the fields this page discusses. Real events include more.

```json
{
  "usageEvents": [
    {
      "timestamp": "1793548800000",
      "usageEventId": "ue_00000000",
      "model": "github_bugbot",
      "kind": "Usage-based",
      "serviceAccountId": "sa_00000001",
      "serviceAccountName": "Example Bugbot Account",
      "repo": "github.example.com/example-org/example-repo",
      "prNumber": 123,
      "prAuthor": "example-author",
      "chargedCents": 12.34
    }
  ],
  "totalUsageEventsCount": 1,
  "pagination": { "numPages": 1, "currentPage": 1, "pageSize": 1000, "hasNextPage": false, "hasPreviousPage": false }
}
```

- `repo` is `<git host>/<org>/<repo>`. The host is `github.com` or your GitHub Enterprise host.
- `prAuthor` is the PR author's Git host login, not an email, so it doesn't match Cursor members without your own login-to-email map. Bot accounts appear too.
- Bugbot events are billed to a Bugbot service account and have no `userEmail`, so they count toward "Service account spend" and not toward any person.

The "Bugbot spend by repo" and "Bugbot spend by PR author" panels group these events by `repo` and by `prAuthor` and sum `chargedCents`.

### Review activity by repo

`/analytics/team/bugbot-reviews` lists each review Bugbot posted. It takes `YYYY-MM-DD` dates, both inclusive, up to 30 days per request:

```bash
curl -sS -u "$CURSOR_ADMIN_API_KEY:" \
  "https://api.cursor.com/analytics/team/bugbot-reviews?startDate=2026-11-01&endDate=2026-11-07&page=1&pageSize=250"
```

Use it for activity, not spend. Its `cost_cents` and `billed` fields came back `null` on every review in our test. Its `repo` and `pr_number` use the same format as the usage events, so the two join on repo and PR number. The "Reviews" column in "Bugbot spend by repo" counts these reviews. The sample below has fake values:

```json
{
  "data": [
    {
      "request_id": "00000000-0000-0000-0000-000000000001",
      "timestamp": "2026-11-02T15:04:05.000Z",
      "repo": "github.example.com/example-org/example-repo",
      "repo_node_id": "R_example",
      "pr_author": "example-author",
      "pr_number": 123,
      "commit_sha": "0000000000000000000000000000000000000000",
      "bugs_found": 1,
      "cost_cents": null,
      "billed": null,
      "dry_run": false,
      "publication_status": "posted",
      "bugs": [
        {
          "comment_id": "example-comment",
          "title": "Example finding",
          "description": "Example description",
          "severity": "medium",
          "resolution_status": "open",
          "locations": [{ "file": "src/example.ts", "start_line": 1, "end_line": 2 }]
        }
      ]
    }
  ],
  "pagination": { "page": 1, "pageSize": 250, "totalItems": 1, "totalPages": 1, "hasNextPage": false, "hasPreviousPage": false },
  "params": { "metric": "bugbot-reviews", "teamId": 0, "startDate": "2026-11-01", "endDate": "2026-11-07", "page": 1, "pageSize": 250 }
}
```

## Security reviewer: total only

Security reviewer spend comes from the same `filtered-usage-events` call shown in the Bugbot section. Its events have `automationManagedType` set to `SECURITY_REVIEWER` and are billed to the team's security service account. The sample below has fake values and shows every field these events carried in our test:

```json
{
  "timestamp": "1793548800000",
  "usageEventId": "ue_00000002",
  "model": "",
  "kind": "Included in Business",
  "maxMode": false,
  "isTokenBasedCall": true,
  "isChargeable": true,
  "isHeadless": false,
  "requestsCosts": 1.0,
  "cursorTokenFee": 0,
  "tokenUsage": { "inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0, "cacheWriteTokens": 0, "totalCents": 0.0 },
  "serviceAccountId": "sa_00000002",
  "serviceAccountName": "Example Security Account",
  "automationManagedType": "SECURITY_REVIEWER",
  "conversationId": "example-conversation-0001",
  "chargedCents": 23.45
}
```

These events have no `cloudAgentId`, `automationId`, `repo`, `prNumber`, `prAuthor`, or `userEmail`. Cursor's data can't tie security reviewer spend to a repo or to the person who opened or pushed the PR. It can only be totaled. Each event's `conversationId` identifies a run, but no public Cursor API documents a way to look up its repo or PR.

On the dashboard, security reviewer spend appears as "Managed: security reviewer" in "Spend by category" and "Daily spend by category", and under its service account in "Spend per service account". It is not in "Cloud agent spend", "Spend per automation", or "Top cloud agent runs", because those need a `cloudAgentId` or an `automationId`. Events with `automationManagedType` set to `CHANGE_MONITOR` also lack those fields and get their own slice.

Creating one security reviewer agent per repo doesn't give spend by repo either. The events don't carry an `automationId`, so separate agents can't be told apart in usage data.

The Cloud Agents API doesn't help. `GET /v1/agents/{id}` needs a `cloudAgentId`, which these events lack. In our test, the same key got `404 agent_not_found` for cloud agents that people's automations had launched, and listing `/v1/agents` returned nothing, which suggests the endpoint only returns the caller's own agents.
