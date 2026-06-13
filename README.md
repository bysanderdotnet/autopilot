# autopilot

Private AI coding control plane. Lets one trusted user run automated
[Google Jules](https://jules.google) coding sessions on selected GitHub
repositories: create a Jules session, wait for it to finish, detect the PR it
opens, wait for GitHub CI + mergeability, merge only when safe, then start the
next run if daily quota remains.

Not a public SaaS — a private automation dashboard for repos controlled by the
owner, served at `autopilot.bysander.net` behind Cloudflare Access. First agent
is Google Jules; the design keeps an `AgentProvider` seam so other agents can be
added later behind the same orchestration layer.

> **Status: pre-implementation.** This repo holds the agent harness and this
> spec. Application code is not built yet. **This README is the complete build
> reference / source of truth** (the former `autopilot_implementation_instructions.md`
> has been folded in here).

---

## Table of contents

1. [Core design](#1-core-design)
2. [Architecture](#2-architecture)
3. [Security model](#3-security-model)
4. [External API facts](#4-external-api-facts)
5. [GitHub App setup](#5-github-app-setup)
6. [Jules setup](#6-jules-setup)
7. [Cloudflare project setup](#7-cloudflare-project-setup)
8. [`wrangler.jsonc` skeleton](#8-wranglerjsonc-skeleton)
9. [Repository structure](#9-repository-structure)
10. [Database schema](#10-database-schema)
11. [Run states](#11-run-states)
12. [Agent abstraction](#12-agent-abstraction)
13. [Jules provider](#13-jules-provider)
14. [GitHub client](#14-github-client)
15. [PR safety module](#15-pr-safety-module)
16. [Workflow](#16-workflow)
17. [Scheduler tick](#17-scheduler-tick)
18. [Worker API routes](#18-worker-api-routes)
19. [UI requirements](#19-ui-requirements)
20. [Prompt templates](#20-prompt-templates)
21. [Failure handling](#21-failure-handling)
22. [Quota model](#22-quota-model)
23. [Locking model](#23-locking-model)
24. [Local development](#24-local-development)
25. [Testing requirements](#25-testing-requirements)
26. [Build phases](#26-build-phases)
27. [Acceptance criteria (MVP)](#27-acceptance-criteria-mvp)
28. [Non-goals](#28-non-goals)
29. [Cautions](#29-cautions)
30. [Working in this repo (harness)](#30-working-in-this-repo-harness)

---

## 1. Core design

**Do not** implement as "start one Jules session every hour" — that creates
overlapping AI work and merge conflicts. Implement a **per-repository sequential
runner**; at most **one active AI coding run per repo** at a time:

```text
repo enabled
  -> no active run?
  -> daily quota available?
  -> create one Jules session
  -> wait for Jules to finish
  -> detect PR
  -> wait for CI and mergeability
  -> merge or fail safely
  -> release repository lock
  -> maybe start next run later
```

The 5-minute cron is only a wake-up tick — **not** "start a run every 5 minutes".
The scheduler checks locks, quota, cooldown, run windows, and enabled configs
before starting anything.

---

## 2. Architecture

```text
Browser
  -> autopilot.bysander.net
  -> Cloudflare Access login
  -> Cloudflare Worker API + static React assets
  -> D1 database
  -> Cloudflare Workflows
  -> Google Jules API
  -> GitHub API
```

Cloudflare products:

- **Access** — protects `autopilot.bysander.net` (the login layer; no public API-key login page).
- **Worker** — serves the React UI and `/api/*` routes.
- **Workflows** — durable execution for each AI coding run (minutes→days).
- **D1** — repo configs, run logs, locks, quotas, PR metadata, settings.
- **Cron Trigger** — wakes the scheduler every 5 min (UTC).
- **Secrets** — Jules + GitHub credentials.

---

## 3. Security model

### 3.1 Access protection

Cloudflare Access application:

- Type: **Self-hosted and private**
- Hostname: `autopilot.bysander.net`
- Policy: allow only the owner's email
- Session duration: ~24h
- Require 2FA via the identity provider

Inside the Worker, still validate the Access JWT for every API route. Use the
`Cf-Access-Jwt-Assertion` header, validate with `jose` against the team domain +
audience, then check the payload email against `ALLOWED_ADMIN_EMAIL`.

Required env / secrets for auth:

```text
TEAM_DOMAIN=https://<your-team>.cloudflareaccess.com
POLICY_AUD=<Cloudflare Access application AUD tag>
ALLOWED_ADMIN_EMAIL=<owner email>
```

### 3.2 Secrets

Store as Cloudflare Worker secrets (never `vars`, D1, or code):

```text
JULES_API_KEY
GITHUB_APP_ID
GITHUB_APP_PRIVATE_KEY_B64
GITHUB_APP_INSTALLATION_ID
ALLOWED_ADMIN_EMAIL
POLICY_AUD
TEAM_DOMAIN
```

```bash
npx wrangler secret put JULES_API_KEY
npx wrangler secret put GITHUB_APP_ID
npx wrangler secret put GITHUB_APP_PRIVATE_KEY_B64
npx wrangler secret put GITHUB_APP_INSTALLATION_ID
npx wrangler secret put ALLOWED_ADMIN_EMAIL
npx wrangler secret put POLICY_AUD
npx wrangler secret put TEAM_DOMAIN
```

GitHub App private key as base64:

```bash
# Linux
base64 -w 0 private-key.pem | npx wrangler secret put GITHUB_APP_PRIVATE_KEY_B64
# macOS
base64 < private-key.pem | tr -d '\n' | npx wrangler secret put GITHUB_APP_PRIVATE_KEY_B64
```

Never return secrets from API routes. Never log secrets. Redact all
authorization headers and API keys in errors.

### 3.3 Merge safety

Autopilot may merge a PR **only when all** of these hold:

1. PR URL came from a tracked Jules session output.
2. PR belongs to the configured GitHub owner/repo.
3. PR is open.
4. PR is not a draft.
5. PR has label `autopilot` or `autopilot:jules`.
6. PR head branch looks Jules-created, or is linked to the tracked session.
7. GitHub says `mergeable === true`.
8. PR is not behind in a way that blocks merging.
9. Required CI/checks are passing.
10. PR head SHA unchanged between inspection and merge.
11. Repo config has `auto_merge_enabled = true`.
12. The run has not been manually cancelled.

Merge via the merge endpoint with the `sha` field set to the PR head SHA.

### 3.4 Human PR isolation

Never touch human-created PRs. A PR is Autopilot-owned **only if**:

```text
tracked run has pr_url = this PR URL
AND PR has label autopilot or autopilot:jules
AND repository matches run.repo_config_id
```

Do not merge or close any PR that does not match this rule.

---

## 4. External API facts

### 4.1 Google Jules API

Docs: [overview](https://developers.google.com/jules/api) ·
[REST quickstart](https://jules.google/docs/api/reference/) ·
[sessions](https://developers.google.com/jules/api/reference/rest/v1alpha/sessions) ·
[sources](https://developers.google.com/jules/api/reference/rest/v1alpha/sources) ·
[activities.list](https://developers.google.com/jules/api/reference/rest/v1alpha/sessions.activities/list)

- API is alpha/experimental. Auth via `X-Goog-Api-Key` / `x-goog-api-key` header. Keys created in the Jules web app settings.
- Built around `Source` (connected repo), `Session` (unit of work), `Activity`.
- `POST https://jules.googleapis.com/v1alpha/sessions` — create a session.
- `GET  /v1alpha/sources` — list connected sources.
- `GET  /v1alpha/sessions` — list sessions.
- `GET  /v1alpha/{name=sessions/*}` — get one session.
- `GET  /v1alpha/{parent=sessions/*}/activities` — list session activities.
- `POST /v1alpha/{session=sessions/*}:approvePlan` — approve a plan.
- `POST /v1alpha/{session=sessions/*}:sendMessage` — send a message.
- `automationMode: "AUTO_CREATE_PR"` makes Jules auto-create a PR when changes are ready.
- `sourceContext.githubRepoContext.startingBranch` is required for GitHub context.
- Session states: `QUEUED`, `PLANNING`, `AWAITING_PLAN_APPROVAL`, `AWAITING_USER_FEEDBACK`, `IN_PROGRESS`, `PAUSED`, `FAILED`, `COMPLETED`.
- Session outputs can include a `pullRequest` with `url`, `title`, `description`.
- **Limitation:** no documented delete/cancel endpoint and no native schedule CRUD. Treat stuck sessions as abandoned; clean up associated GitHub PRs when safe.

### 4.2 Cloudflare stack

Docs: [Workflows](https://developers.cloudflare.com/workflows/) ·
[Workers API/bindings](https://developers.cloudflare.com/workflows/build/workers-api/) ·
[trigger](https://developers.cloudflare.com/workflows/build/trigger-workflows/) ·
[sleep/retry](https://developers.cloudflare.com/workflows/build/sleeping-and-retrying/) ·
[rules/idempotency](https://developers.cloudflare.com/workflows/build/rules-of-workflows/) ·
[Access self-hosted](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/) ·
[Access JWT validation](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/) ·
[D1](https://developers.cloudflare.com/d1/get-started/) ·
[D1 Worker API](https://developers.cloudflare.com/d1/worker-api/) ·
[secrets](https://developers.cloudflare.com/workers/configuration/secrets/) ·
[wrangler config](https://developers.cloudflare.com/workers/wrangler/configuration/) ·
[cron](https://developers.cloudflare.com/workers/configuration/cron-triggers/) ·
[React+Vite on Workers](https://developers.cloudflare.com/workers/framework-guides/web-apps/react/)

- Workflows: durable multi-step jobs (minutes/hours/days); bindings in `wrangler.jsonc` `workflows` array; trigger via `env.MY_WORKFLOW.create({ id, params })`; sleep via `step.sleep(...)`; retry failed `step.do(...)`. Steps must be idempotent (may be retried).
- D1: repo config, run history, locks, counters. Worker secrets for keys/tokens, not plaintext `vars`.
- `wrangler.jsonc` recommended for new projects. Cron managed in Wrangler config; cron is UTC.
- React SPA + Worker API via Workers Assets + Cloudflare Vite plugin.

### 4.3 GitHub API

Docs: [App installation auth](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation) ·
[installation token](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app) ·
[pulls](https://docs.github.com/en/rest/pulls/pulls) ·
[check runs](https://docs.github.com/en/rest/checks/runs) ·
[commit statuses](https://docs.github.com/en/rest/commits/statuses) ·
[required checks](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/collaborating-on-repositories-with-code-quality-features/troubleshooting-required-status-checks)

- Use a GitHub **App installation token**, not a PAT. Installation tokens expire after 1h → generate on demand.
- `GET /repos/{owner}/{repo}/pulls/{pull_number}` — PR state + mergeability. `mergeable` may be `null` while GitHub computes it.
- `GET /repos/{owner}/{repo}/commits/{ref}/check-runs` — check runs for the PR head SHA.
- `GET /repos/{owner}/{repo}/commits/{ref}/status` — combined commit statuses.
- Required checks must pass against the latest commit SHA before merge. Successful conclusions: `success`, `skipped`, `neutral`.
- `PUT /repos/{owner}/{repo}/pulls/{pull_number}/merge` — merge; pass head `sha`. `merge_method`: `merge` | `squash` | `rebase`.
- `PATCH /repos/{owner}/{repo}/pulls/{pull_number}` with `state: "closed"` — close abandoned PRs.

---

## 5. GitHub App setup

Create a private GitHub App, e.g. `bysander-autopilot`; install only on selected
repositories.

Permissions:

```text
Repository metadata: read
Pull requests: read/write
Contents: read/write
Checks: read
Commit statuses: read
Issues: read/write     # PR labels/comments use the issue APIs
```

Settings:

- Webhook: not required for MVP (poll first).
- Callback URL: not required unless OAuth is added later.
- Private key → `GITHUB_APP_PRIVATE_KEY_B64`; App ID → `GITHUB_APP_ID`; Installation ID → `GITHUB_APP_INSTALLATION_ID`.

MVP uses polling, not webhooks, because the app sits behind Cloudflare Access.
Webhooks can be added later via a separate service-auth-protected endpoint.

---

## 6. Jules setup

1. Configure the Jules GitHub integration in the Jules web app.
2. Generate a Jules API key in Jules settings → store as `JULES_API_KEY`.
3. `GET /v1alpha/sources` to list repos Jules can access.
4. `POST /v1alpha/sessions` with `automationMode: "AUTO_CREATE_PR"` to run sessions that auto-open PRs.

Session creation payload:

```json
{
  "prompt": "Implement the next small safe improvement for this repository. Keep the change focused. Do not perform broad rewrites.",
  "sourceContext": {
    "source": "sources/github/OWNER/REPO",
    "githubRepoContext": { "startingBranch": "main" }
  },
  "automationMode": "AUTO_CREATE_PR",
  "requirePlanApproval": false,
  "title": "Autopilot run: OWNER/REPO"
}
```

For unattended operation use `requirePlanApproval: false`. If `true`, Autopilot
must stop for manual approval or explicitly call `approvePlan` — do not silently
auto-approve unless the repo config says so.

---

## 7. Cloudflare project setup

```bash
npm create cloudflare@latest autopilot -- --framework=react
cd autopilot
npm install
npm install jose @octokit/auth-app @octokit/request zod
npm install -D vitest @testing-library/react @testing-library/jest-dom jsdom
```

D1 database:

```bash
npx wrangler d1 create autopilot-db --location weur --jurisdiction eu --binding DB --update-config
```

Migrations:

```bash
mkdir -p migrations
npx wrangler d1 migrations create autopilot-db init
npx wrangler d1 migrations apply autopilot-db --local
npx wrangler d1 migrations apply autopilot-db --remote
```

Worker types:

```bash
npx wrangler types
```

---

## 8. `wrangler.jsonc` skeleton

Use `wrangler.jsonc`, not `wrangler.toml`.

```jsonc
{
  "$schema": "./node_modules/wrangler/config-schema.json",
  "name": "autopilot",
  "main": "worker/index.ts",
  "compatibility_date": "2026-06-12",
  "compatibility_flags": ["nodejs_compat"],
  "assets": {
    "directory": "./dist/client",
    "not_found_handling": "single-page-application"
  },
  "observability": { "enabled": true },
  "d1_databases": [
    {
      "binding": "DB",
      "database_name": "autopilot-db",
      "database_id": "<fill-from-wrangler-output>"
    }
  ],
  "workflows": [
    {
      "name": "autopilot-runner",
      "binding": "AUTOPILOT_RUNNER",
      "class_name": "AutopilotRunnerWorkflow"
    }
  ],
  "triggers": { "crons": ["*/5 * * * *"] },
  "secrets": {
    "required": [
      "JULES_API_KEY",
      "GITHUB_APP_ID",
      "GITHUB_APP_PRIVATE_KEY_B64",
      "GITHUB_APP_INSTALLATION_ID",
      "ALLOWED_ADMIN_EMAIL",
      "POLICY_AUD",
      "TEAM_DOMAIN"
    ]
  },
  "vars": { "APP_ENV": "production", "PUBLIC_APP_NAME": "Autopilot" }
}
```

- `compatibility_date` must be ≥ `2024-10-22` for Workflow bindings.
- Cron is a wake-up tick, not "start a run every 5 minutes".
- Cron is UTC; store repo time windows with explicit timezone (e.g. `Europe/Brussels`).

---

## 9. Repository structure

```text
autopilot/
  src/
    App.tsx
    main.tsx
    routes/
    components/
      Layout.tsx  RepoList.tsx  RepoConfigForm.tsx
      RunHistory.tsx  RunDetail.tsx  SafetyStatus.tsx
    lib/
      apiClient.ts  types.ts
  worker/
    index.ts
    workflows/AutopilotRunnerWorkflow.ts
    api/
      routes.ts  auth.ts  validators.ts
    agents/
      AgentProvider.ts
      jules/JulesProvider.ts  jules/julesTypes.ts
    github/
      GitHubClient.ts  githubAuth.ts  prSafety.ts
    scheduler/
      tick.ts  quota.ts  locks.ts  policies.ts
    db/
      schema.ts  repositories.ts  runs.ts  locks.ts  events.ts
    util/
      errors.ts  redaction.ts  time.ts  ids.ts
  migrations/0001_init.sql
  test/
  wrangler.jsonc
  package.json
  README.md
```

---

## 10. Database schema

`migrations/0001_init.sql`:

```sql
CREATE TABLE IF NOT EXISTS repo_configs (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),

  enabled INTEGER NOT NULL DEFAULT 0,

  agent_kind TEXT NOT NULL DEFAULT 'jules',
  display_name TEXT NOT NULL,

  github_owner TEXT NOT NULL,
  github_repo TEXT NOT NULL,
  github_default_branch TEXT NOT NULL DEFAULT 'main',

  jules_source_name TEXT,

  prompt_template TEXT NOT NULL,
  title_template TEXT NOT NULL DEFAULT 'Autopilot run: {{owner}}/{{repo}} #{{sequence}}',

  daily_session_limit INTEGER NOT NULL DEFAULT 10,
  daily_attempt_limit INTEGER NOT NULL DEFAULT 10,
  min_delay_minutes INTEGER NOT NULL DEFAULT 30,
  max_runtime_minutes INTEGER NOT NULL DEFAULT 120,
  timezone TEXT NOT NULL DEFAULT 'Europe/Brussels',
  run_window_start TEXT, -- HH:MM, nullable
  run_window_end TEXT,   -- HH:MM, nullable

  require_plan_approval INTEGER NOT NULL DEFAULT 0,
  auto_approve_plan INTEGER NOT NULL DEFAULT 0,
  auto_merge_enabled INTEGER NOT NULL DEFAULT 0,
  close_failed_prs INTEGER NOT NULL DEFAULT 1,
  merge_method TEXT NOT NULL DEFAULT 'squash',

  UNIQUE(agent_kind, github_owner, github_repo, github_default_branch)
);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  repo_config_id TEXT NOT NULL REFERENCES repo_configs(id),

  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  started_at TEXT,
  finished_at TEXT,

  trigger_kind TEXT NOT NULL, -- manual | cron | retry
  state TEXT NOT NULL,

  workflow_instance_id TEXT,

  agent_kind TEXT NOT NULL DEFAULT 'jules',
  agent_session_name TEXT,
  agent_session_id TEXT,
  agent_session_url TEXT,
  agent_session_state TEXT,

  github_owner TEXT NOT NULL,
  github_repo TEXT NOT NULL,
  base_branch TEXT NOT NULL,

  pr_url TEXT,
  pr_number INTEGER,
  pr_head_sha TEXT,
  pr_state TEXT,
  pr_mergeable INTEGER,
  pr_is_draft INTEGER,

  quota_date TEXT NOT NULL,
  sequence_number INTEGER NOT NULL,

  failure_reason TEXT,
  last_error TEXT,

  FOREIGN KEY(repo_config_id) REFERENCES repo_configs(id)
);

CREATE INDEX IF NOT EXISTS idx_runs_repo_config_id ON runs(repo_config_id);
CREATE INDEX IF NOT EXISTS idx_runs_state ON runs(state);
CREATE INDEX IF NOT EXISTS idx_runs_quota_date ON runs(repo_config_id, quota_date);
CREATE INDEX IF NOT EXISTS idx_runs_agent_session_name ON runs(agent_session_name);
CREATE INDEX IF NOT EXISTS idx_runs_pr_url ON runs(pr_url);

CREATE TABLE IF NOT EXISTS repo_locks (
  repo_config_id TEXT PRIMARY KEY REFERENCES repo_configs(id),
  run_id TEXT NOT NULL REFERENCES runs(id),
  acquired_at TEXT NOT NULL DEFAULT (datetime('now')),
  expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_counters (
  repo_config_id TEXT NOT NULL REFERENCES repo_configs(id),
  quota_date TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  sessions_created INTEGER NOT NULL DEFAULT 0,
  successful_merges INTEGER NOT NULL DEFAULT 0,
  failures INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(repo_config_id, quota_date)
);

CREATE TABLE IF NOT EXISTS run_events (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  level TEXT NOT NULL DEFAULT 'info',
  event_type TEXT NOT NULL,
  message TEXT NOT NULL,
  data_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_run_events_run_id ON run_events(run_id);

CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Never hand-edit an applied migration; add a new one.

---

## 11. Run states

```text
queued
lock_acquired
quota_reserved
creating_agent_session
agent_queued
agent_planning
agent_waiting_for_approval
agent_waiting_for_feedback
agent_running
agent_failed
agent_completed
no_pr_created
pr_detected
pr_annotated
waiting_for_ci
waiting_for_mergeability
merge_ready
merged
closed_failed_pr
timed_out
abandoned
manual_review_required
cancelled
failed
```

---

## 12. Agent abstraction

The project is `autopilot`, not `jules-autopilot` — design for multiple agents.
`worker/agents/AgentProvider.ts`:

```ts
export type AgentKind = 'jules';

export type CreateAgentSessionInput = {
  repoConfigId: string;
  owner: string;
  repo: string;
  sourceName: string;
  branch: string;
  prompt: string;
  title: string;
  autoCreatePr: boolean;
  requirePlanApproval: boolean;
};

export type AgentSession = {
  name: string;
  id: string;
  url?: string;
  state?: string;
  outputs?: AgentSessionOutput[];
};

export type AgentSessionOutput = {
  type: 'pull_request';
  url: string;
  title?: string;
  description?: string;
};

export interface AgentProvider {
  kind: AgentKind;
  listSources(): Promise<unknown[]>;
  createSession(input: CreateAgentSessionInput): Promise<AgentSession>;
  getSession(sessionName: string): Promise<AgentSession>;
  listActivities(sessionName: string): Promise<unknown[]>;
  approvePlan(sessionName: string): Promise<void>;
  sendMessage(sessionName: string, prompt: string): Promise<void>;
}
```

Implement `JulesProvider` first.

---

## 13. Jules provider

`worker/agents/jules/JulesProvider.ts`.

```ts
const JULES_BASE_URL = 'https://jules.googleapis.com/v1alpha';

headers: {
  'content-type': 'application/json',
  'x-goog-api-key': env.JULES_API_KEY,
}
```

Methods:

```text
GET  /sources?pageSize=100
GET  /sessions?pageSize=100
GET  /{name=sessions/*}
GET  /{parent=sessions/*}/activities?pageSize=100
POST /sessions
POST /{session=sessions/*}:approvePlan
POST /{session=sessions/*}:sendMessage
```

- Paginate sources, sessions, activities.
- Redact the Jules API key from all thrown errors.
- HTTP 401/403 fatal/non-retryable; 429/5xx retryable.
- Extract PRs from `session.outputs[].pullRequest.url`; inspect activities only as a fallback (prefer `session.outputs`).
- Do not try to delete/cancel sessions (undocumented).

---

## 14. GitHub client

`worker/github/githubAuth.ts`:

- Decode `GITHUB_APP_PRIVATE_KEY_B64`.
- Use `@octokit/auth-app` (or manually generate the JWT).
- Generate an installation token from the installation ID.
- Cache the installation token in memory until near expiry; do **not** persist to D1.

`worker/github/GitHubClient.ts` methods:

```ts
getRepository(owner, repo)
getPullRequest(owner, repo, pullNumber)
updatePullRequest(owner, repo, pullNumber, input)
addLabels(owner, repo, issueNumber, labels)
createIssueComment(owner, repo, issueNumber, body)
listCheckRunsForRef(owner, repo, ref)
getCombinedStatusForRef(owner, repo, ref)
mergePullRequest(owner, repo, pullNumber, input)
```

Headers:

```text
Accept: application/vnd.github+json
Authorization: Bearer <installation-token>
X-GitHub-Api-Version: 2026-03-10
```

Merge request:

```text
PUT /repos/{owner}/{repo}/pulls/{pull_number}/merge
{
  "commit_title": "Autopilot: <PR title>",
  "commit_message": "Merged by Autopilot after Jules completed and required checks passed.",
  "sha": "<current PR head SHA>",
  "merge_method": "squash"
}
```

Close abandoned PR:

```text
PATCH /repos/{owner}/{repo}/pulls/{pull_number}
{ "state": "closed" }
```

---

## 15. PR safety module

`worker/github/prSafety.ts`:

```ts
export async function evaluatePrSafety(input: {
  github: GitHubClient;
  owner: string;
  repo: string;
  pullNumber: number;
  expectedPrUrl: string;
  expectedLabel: string;
  expectedBaseBranch: string;
}): Promise<PrSafetyResult>;

type PrSafetyResult = {
  canMerge: boolean;
  reason?: string;
  pull: {
    number: number;
    state: string;
    draft: boolean;
    mergeable: boolean | null;
    mergeable_state?: string;
    headSha: string;
    baseRef: string;
    labels: string[];
  };
  checks: {
    pending: string[];
    failing: string[];
    successful: string[];
    unknown: string[];
  };
};
```

Rules:

```text
if PR closed                 -> cannot merge
if draft                     -> cannot merge
if base branch != config     -> cannot merge
if label missing             -> cannot merge
if mergeable === null        -> wait and poll again
if mergeable === false       -> cannot merge (maybe update branch if enabled later)
if any checks pending        -> wait
if any check failed          -> wait (Jules may fix it)
if failed checks past timeout -> manual_review_required or close PR per config
if combined status failure   -> wait/fail
if all checks success/skipped/neutral AND mergeable true -> can merge
```

MVP: branch protection is still enforced by the merge endpoint; inspect checks
first to avoid noisy merge attempts. Do not auto-update branches in MVP.

---

## 16. Workflow

`worker/workflows/AutopilotRunnerWorkflow.ts`.

```ts
type AutopilotWorkflowParams = {
  runId: string;
  repoConfigId: string;
  triggerKind: 'manual' | 'cron' | 'retry';
};
```

Steps:

```text
1. Load repo config
2. Acquire per-repo lock
3. Reserve quota
4. Create Jules session
5. Poll Jules session until terminal/timeout
6. Extract PR URL
7. Annotate PR with label/comment
8. Wait for CI and mergeability
9. Merge PR or mark for manual review/failure
10. Release lock
11. Update next-run metadata
```

Pseudo-code:

```ts
export class AutopilotRunnerWorkflow extends WorkflowEntrypoint<Env, AutopilotWorkflowParams> {
  async run(event: WorkflowEvent<AutopilotWorkflowParams>, step: WorkflowStep) {
    const { runId, repoConfigId } = event.payload;

    const config = await step.do('load repo config', async () => {
      return await loadRepoConfig(this.env.DB, repoConfigId);
    });

    await step.do('acquire repo lock', async () => {
      await acquireRepoLockOrThrow(this.env.DB, repoConfigId, runId, config.max_runtime_minutes);
    });

    try {
      await step.do('reserve quota', async () => {
        await reserveDailyQuotaOrThrow(this.env.DB, repoConfigId, runId, config);
      });

      const session = await step.do('create jules session', {
        retries: { limit: 3, delay: '30 seconds', backoff: 'exponential' },
        timeout: '2 minutes'
      }, async () => {
        return await createAndPersistJulesSession(this.env, runId, config);
      });

      let sessionState = session.state;
      let prUrl: string | undefined;
      const deadline = Date.now() + config.max_runtime_minutes * 60_000;

      for (let i = 0; i < 240; i++) {
        if (Date.now() > deadline) break;

        const latest = await step.do(`poll jules session ${i}`, {
          retries: { limit: 3, delay: '20 seconds', backoff: 'linear' },
          timeout: '2 minutes'
        }, async () => {
          return await pollAndPersistJulesSession(this.env, runId, session.name);
        });

        sessionState = latest.state;
        prUrl = extractPullRequestUrl(latest);

        if (sessionState === 'FAILED') {
          await markRunFailed(this.env.DB, runId, 'jules_session_failed');
          return;
        }
        if (sessionState === 'COMPLETED') break;

        await step.sleep(`wait before next Jules poll ${i}`, '2 minutes');
      }

      if (sessionState !== 'COMPLETED') {
        await step.do('mark timed out', async () => { await markRunTimedOut(this.env.DB, runId); });
        return;
      }
      if (!prUrl) {
        await step.do('mark no PR created', async () => { await markRunNoPrCreated(this.env.DB, runId); });
        return;
      }

      const pr = await step.do('register and annotate PR', async () => {
        return await registerAndAnnotatePr(this.env, runId, config, prUrl);
      });

      for (let i = 0; i < 240; i++) {
        const safety = await step.do(`evaluate PR safety ${i}`, {
          retries: { limit: 3, delay: '20 seconds', backoff: 'linear' },
          timeout: '2 minutes'
        }, async () => {
          return await evaluateAndPersistPrSafety(this.env, runId, config, pr.number);
        });

        if (safety.canMerge) {
          await step.do('merge PR', async () => {
            await mergePrSafely(this.env, runId, config, pr.number, safety.pull.headSha);
          });
          return;
        }
        if (isTerminalPrFailure(safety)) {
          await step.do('mark manual review required', async () => {
            await markManualReviewRequired(this.env.DB, runId, safety.reason ?? 'terminal_pr_failure');
          });
          return;
        }
        await step.sleep(`wait before next PR check ${i}`, '2 minutes');
      }

      await step.do('mark PR wait timed out', async () => {
        await markManualReviewRequired(this.env.DB, runId, 'pr_wait_timed_out');
      });
    } finally {
      await step.do('release repo lock', async () => {
        await releaseRepoLock(this.env.DB, repoConfigId, runId);
      });
    }
  }
}
```

Rules: external API calls inside `step.do`; every `step.do` idempotent; persist
intermediate state to D1; deterministic step names in loops; handle retryable vs
non-retryable explicitly; auth failures are non-retryable.

---

## 17. Scheduler tick

Cron calls `scheduled()` in `worker/index.ts` every 5 minutes. Responsibilities:

```text
1. Load enabled repo configs.
2. Skip repo if already locked.
3. Skip repo if daily quota exhausted.
4. Skip repo if outside configured run window.
5. Skip repo if min_delay_minutes has not elapsed since last terminal run.
6. Create a run row with state queued.
7. Trigger AUTOPILOT_RUNNER workflow with a deterministic ID.
```

Deterministic workflow ID: `autopilot:<repo_config_id>:<run_id>`. If Workflow
creation returns "already exists", do not create a duplicate run.

```ts
export async function schedulerTick(env: Env) {
  const configs = await listEnabledRepoConfigs(env.DB);
  for (const config of configs) {
    const decision = await shouldStartRun(env.DB, config, new Date());
    if (!decision.start) continue;

    const run = await createQueuedRun(env.DB, config, 'cron');
    await env.AUTOPILOT_RUNNER.create({
      id: `autopilot:${config.id}:${run.id}`.slice(0, 100),
      params: { runId: run.id, repoConfigId: config.id, triggerKind: 'cron' }
    });
  }
}
```

---

## 18. Worker API routes

All `/api/*` routes require validated Cloudflare Access identity.

```text
GET    /api/me
GET    /api/health
GET    /api/jules/sources
GET    /api/repos
POST   /api/repos
GET    /api/repos/:id
PATCH  /api/repos/:id
DELETE /api/repos/:id
POST   /api/repos/:id/enable
POST   /api/repos/:id/disable
POST   /api/repos/:id/run-now
GET    /api/repos/:id/runs
GET    /api/runs/:id
POST   /api/runs/:id/cancel
POST   /api/runs/:id/close-pr
POST   /api/runs/:id/retry
GET    /api/runs/:id/events
```

Do **not** expose endpoints for arbitrary GitHub operations. Every action maps
to a configured repo and tracked run.

---

## 19. UI requirements

Simple private dashboard.

**Home / dashboard:** enabled repos, active runs, runs today, daily quota used,
failed/stuck runs, recent merges, emergency global pause toggle.

**Repository list:** owner/repo, branch, agent kind, enabled/disabled, daily
limit, active run state, last run result, auto-merge on/off.

**Add repository:** load Jules sources → select source/repo → select branch →
prompt template → daily quota → max runtime → run window → auto-merge mode →
save **disabled by default** → require explicit "Enable automation".

**Repository detail:** config form, enable/disable, run-now, active run panel,
run history, latest PR, quota usage, last errors.

**Run detail:** run state, Jules session URL, GitHub PR URL, event timeline,
current checks, mergeability, failure reason; manual actions: cancel local
tracking, retry run, close tracked PR, mark manual review.

---

## 20. Prompt templates

Each repo config has a `prompt_template`. Variables:

```text
{{owner}} {{repo}} {{branch}} {{date}} {{sequence}}
{{daily_limit}} {{previous_run_summary}} {{last_failure_reason}}
```

Default prompt:

```text
You are working on {{owner}}/{{repo}} on branch {{branch}}.

Make one small, safe, high-quality improvement to the repository.

Rules:
- Keep the change focused.
- Do not perform broad rewrites.
- Prefer maintainability, tests, documentation, bug fixes, or small UX improvements.
- Do not touch secrets, credentials, deployment tokens, or unrelated configuration.
- If there is nothing useful to do, make no code changes and explain why.
- Ensure the project builds and tests pass.
- Create a pull request when ready.
```

Later: per-repo backlog items, labels, issue picking, documentation-only mode,
test-fix mode.

---

## 21. Failure handling

**Jules session fails** (`FAILED`): mark `agent_failed`; store failure reason if
activity has `sessionFailed.reason`; count attempt against quota; if a PR exists,
do not close immediately unless `close_failed_prs = true` and PR is clearly tracked.

**Jules session hangs** (max runtime exceeded): mark `timed_out`; release lock;
if a tracked PR exists → comment that Autopilot timed out, close it if
`close_failed_prs = true`, else mark `manual_review_required`. Do not delete the
Jules session (undocumented).

**CI fails:** keep polling a while (Jules may keep fixing the PR). If still
failing at PR-wait timeout → `manual_review_required` by default; do not merge;
optionally close only if configured.

**Merge conflict** (`mergeable === false`): mark `manual_review_required` in MVP;
no auto branch-update; add "update branch" later.

**GitHub rate limits:** conservative polling (~2 min); back off on 403/429 with
rate-limit headers; never spam merge attempts.

---

## 22. Quota model

Failed Jules sessions consume Jules quota → count **attempts**, not only merges.

```text
daily_attempt_limit: max Jules sessions Autopilot may attempt per repo per local day
sessions_created:    actual Jules sessions created
successful_merges:   PRs merged
failures:            failed/timed-out/manual-review runs
```

Quota date uses the repo config timezone (default `Europe/Brussels`). Reserve
quota **before** calling Jules:

```text
1. Ensure daily counter row exists.
2. Atomically increment attempts only if attempts < daily_attempt_limit.
3. If no row changed, stop.
4. Only then create the Jules session.
```

If session creation fails before Jules accepts it, prefer **not** to decrement
`attempts` unless the API clearly returned no session and no quota-consuming work
happened.

---

## 23. Locking model

`repo_locks` prevents concurrent runs per repo.

```text
- repo_config_id is primary key.
- acquire lock before creating the Jules session.
- lock has expires_at to recover from broken workflows.
- scheduler skips locked repos unless the lock expired.
- workflow releases the lock in a finally step.
```

Acquire behavior:

```text
if no lock          -> insert lock
if lock expired     -> replace lock and add a warning event
if active lock      -> stop run as skipped/duplicate
```

---

## 24. Local development

`.dev.vars` example:

```text
JULES_API_KEY=...
GITHUB_APP_ID=...
GITHUB_APP_PRIVATE_KEY_B64=...
GITHUB_APP_INSTALLATION_ID=...
ALLOWED_ADMIN_EMAIL=you@example.com
POLICY_AUD=local-dev
TEAM_DOMAIN=https://example.cloudflareaccess.com
```

Dev-only auth bypass:

```text
if APP_ENV === 'development' and header x-dev-user-email matches ALLOWED_ADMIN_EMAIL -> allow
```

Never enable that bypass in production.

```bash
npm run dev
# scheduled handler:
npx wrangler dev --test-scheduled
curl "http://localhost:8787/__scheduled?cron=*/5+*+*+*+*"
```

---

## 25. Testing requirements

Vitest. Cover at least:

**Security:** Access JWT missing → 403; wrong email → 403; secrets never in
error responses; API key redaction works.

**Scheduler:** disabled repo does not start; locked repo does not start;
quota-exhausted repo does not start; repo outside run window does not start; repo
with quota and no lock starts exactly one Workflow.

**Workflow:** creates Jules session once; polls until completed; handles `FAILED`;
handles timeout; extracts PR URL from session outputs; releases lock on success
and failure.

**GitHub safety:** human PR without label never merged; tracked PR with failing
checks not merged; with pending checks not merged; with `mergeable === null` not
merged yet; with passing checks and `mergeable === true` merged with expected
SHA; SHA mismatch prevents merge.

**Database:** quota reservation atomic enough for duplicate scheduler ticks;
expired lock can be replaced; active lock cannot be replaced.

---

## 26. Build phases

Tracked as features in the harness (`./AGENTS.sh feature list`). One feature per
session/commit.

| Phase | Feature | Deliverable |
|---|---|---|
| 0 | F-00001 | Toolchain + Cloudflare scaffold, `wrangler.jsonc`, D1, `0001_init.sql`, verify commands |
| 1 | F-00002 | Private dashboard shell: Access JWT validation, D1, `/api/me` + `/api/health`, layout |
| 2 | F-00003 | Jules source browser: `JulesProvider`, `/api/jules/sources`, redacted errors |
| 3 | F-00004 | Repo config CRUD: add/edit from Jules source, enable/disable, stored in D1 |
| 4 | F-00005 | Manual run (no merge): run-now → Workflow creates Jules session, polls, events |
| 5 | F-00006 | PR tracking (no merge): extract PR, parse owner/repo/number, label + comment |
| 6 | F-00007 | CI-aware merge: GitHub App auth, mergeability + checks, merge only if safe w/ SHA |
| 7 | F-00008 | Scheduler: cron tick, per-repo lock, daily quota, min delay, run window |
| 8 | F-00009 | Cleanup + manual controls: cancel, retry, close PR, global pause, manual review |
| 9 | F-00010 | Multi-agent: provider registry, second provider behind `AgentProvider` (later) |

Start with auto-merge **disabled** per repo; enable manually after observing
behavior.

---

## 27. Acceptance criteria (MVP)

1. `autopilot.bysander.net` is protected by Cloudflare Access.
2. The Worker rejects unauthenticated API requests.
3. Secrets are Cloudflare secrets, not in code or D1.
4. The dashboard lists Jules-connected repositories.
5. A repository can be configured and enabled.
6. A manual run can create a Jules session.
7. The run is visible with event history.
8. The runner waits for the Jules session to finish.
9. The runner detects the PR Jules created.
10. The runner labels/comments only tracked PRs.
11. The runner can evaluate PR mergeability and CI.
12. The runner merges only safe, tracked PRs.
13. The scheduler respects daily quota.
14. The scheduler never starts two active runs for the same repo.
15. Stuck sessions time out and release the repo lock.
16. Human PRs are never merged or closed.
17. Tests cover scheduler, lock, quota, auth, and merge-safety logic.

---

## 28. Non-goals

Not in MVP: public user accounts; multi-tenant SaaS; billing; browser-stored API
keys; undocumented Jules schedule endpoints; scraping the Jules web UI;
auto-updating PR branches; GitHub webhooks; automatic issue selection; autonomous
large refactors; automatic deletion/cancellation of Jules sessions.

---

## 29. Cautions

This tool has authority to create AI coding sessions and merge code — treat it
like production infrastructure. Roll out gradually:

```text
one repo -> manual runs only -> auto-merge disabled -> observe
  -> then enable merge -> then enable scheduler -> then add more repos
```

The danger is not creating Jules sessions; it is giving a bot merge authority
without enough guardrails.

---

## 30. Working in this repo (harness)

The agent harness drives the workflow. Use the wrapper, not the `.agents/`
internals:

```sh
./AGENTS.sh init       # session start: status + next step
./AGENTS.sh verify     # definition of done
./AGENTS.sh handoff    # end-of-session checklist
./AGENTS.sh help       # all commands
```

- Agent rules + project identity: `AGENTS.md`. Curated rules: `./AGENTS.sh docs`.
- Scope / build tasks: `./AGENTS.sh feature list` (one in progress at a time).
- State lives in `.agents/agents.json` (CLI-owned — never hand-edit).
- Done means `./AGENTS.sh verify` is green; anything else is "unverified".

Entrypoints: `CLAUDE.md` / `GEMINI.md` (symlinks to `AGENTS.md`),
`.github/copilot-instructions.md`.
