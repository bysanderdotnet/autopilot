# Autopilot — implementation instructions

**Project name:** `autopilot`  
**Target hostname:** `autopilot.bysander.net`  
**Deployment model:** private Cloudflare Worker + React UI, protected by Cloudflare Access  
**Primary first agent:** Google Jules  
**Future direction:** add other AI coding agents behind the same orchestration layer

Last researched: 2026-06-12.

---

## 1. What this project is

`autopilot` is a private AI coding control plane.

The first version should let one trusted user enable automated Google Jules coding runs for selected GitHub repositories. It should create Jules sessions, wait until they finish, detect the pull request Jules creates, wait for GitHub CI and mergeability, merge only when safe, and then start the next run only if the repository still has daily quota.

This is **not** a public SaaS product. It is a private automation dashboard for repositories controlled by Sander.

---

## 2. Core design decision

Do **not** implement this as “start one Jules session every hour”. That creates overlapping AI work and merge conflicts.

Implement it as a **per-repository sequential runner**:

```text
Repository enabled
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

Each repository must have at most **one active AI coding run** at a time.

---

## 3. Official API facts to rely on

### 3.1 Google Jules API

Official docs:

- Jules API overview: https://developers.google.com/jules/api
- Jules REST quickstart: https://jules.google/docs/api/reference/
- Jules sessions REST resource: https://developers.google.com/jules/api/reference/rest/v1alpha/sessions
- Jules sources REST resource: https://developers.google.com/jules/api/reference/rest/v1alpha/sources
- Jules activities list: https://developers.google.com/jules/api/reference/rest/v1alpha/sessions.activities/list

Current important facts:

- The Jules API is alpha/experimental.
- Authentication uses the `X-Goog-Api-Key` / `x-goog-api-key` header.
- Jules API keys are created in the Jules web app settings.
- The API is built around `Source`, `Session`, and `Activity`.
- A `Source` represents a connected repository.
- A `Session` represents a unit of Jules work.
- `POST https://jules.googleapis.com/v1alpha/sessions` creates a session.
- `GET https://jules.googleapis.com/v1alpha/sources` lists connected sources.
- `GET https://jules.googleapis.com/v1alpha/sessions` lists sessions.
- `GET https://jules.googleapis.com/v1alpha/{name=sessions/*}` gets one session.
- `GET https://jules.googleapis.com/v1alpha/{parent=sessions/*}/activities` lists session activities.
- `POST https://jules.googleapis.com/v1alpha/{session=sessions/*}:approvePlan` approves a plan.
- `POST https://jules.googleapis.com/v1alpha/{session=sessions/*}:sendMessage` sends a message to a session.
- `automationMode: "AUTO_CREATE_PR"` makes Jules automatically create a pull request when code changes are ready.
- `sourceContext.githubRepoContext.startingBranch` is required for GitHub repository context.
- Session states include `QUEUED`, `PLANNING`, `AWAITING_PLAN_APPROVAL`, `AWAITING_USER_FEEDBACK`, `IN_PROGRESS`, `PAUSED`, `FAILED`, and `COMPLETED`.
- Session outputs can include a `pullRequest` with `url`, `title`, and `description`.

Important limitation:

- The documented public API does **not** currently expose a session delete/cancel endpoint or native schedule CRUD endpoint. Treat stuck sessions as abandoned in Autopilot, and clean up associated GitHub PRs when safe.

### 3.2 Cloudflare stack

Official docs:

- Cloudflare Workflows overview: https://developers.cloudflare.com/workflows/
- Workflows from Workers / bindings: https://developers.cloudflare.com/workflows/build/workers-api/
- Trigger Workflows: https://developers.cloudflare.com/workflows/build/trigger-workflows/
- Sleeping and retrying: https://developers.cloudflare.com/workflows/build/sleeping-and-retrying/
- Rules of Workflows / idempotency: https://developers.cloudflare.com/workflows/build/rules-of-workflows/
- Cloudflare Access self-hosted app: https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/
- Cloudflare Access JWT validation: https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/
- D1 getting started: https://developers.cloudflare.com/d1/get-started/
- D1 Worker binding API: https://developers.cloudflare.com/d1/worker-api/
- Workers secrets: https://developers.cloudflare.com/workers/configuration/secrets/
- Wrangler configuration: https://developers.cloudflare.com/workers/wrangler/configuration/
- Cron triggers: https://developers.cloudflare.com/workers/configuration/cron-triggers/
- React + Vite on Workers: https://developers.cloudflare.com/workers/framework-guides/web-apps/react/

Current important facts:

- Workflows are suitable for durable multi-step jobs that may run for minutes, hours, or days.
- Workflow bindings are configured in `wrangler.jsonc` using a `workflows` array.
- Workflows can be triggered from Workers using `env.MY_WORKFLOW.create({ id, params })`.
- Workflows can sleep using `step.sleep(...)` and retry failed `step.do(...)` calls.
- Workflow steps should be idempotent because failed steps may be retried.
- D1 is appropriate for repo configuration, run history, locks, and counters.
- Worker secrets should be used for API keys and tokens. Do not store secrets in plaintext `vars`.
- Cloudflare recommends `wrangler.jsonc` for new projects.
- Cron triggers should be managed through Wrangler config when the Worker is managed with Wrangler.
- A React SPA can be combined with a Worker API using Cloudflare Workers Assets and the Cloudflare Vite plugin.

### 3.3 GitHub API

Official docs:

- GitHub App installation authentication: https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation
- Generate installation access token: https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app
- Pull requests REST API: https://docs.github.com/en/rest/pulls/pulls
- Check runs REST API: https://docs.github.com/en/rest/checks/runs
- Commit statuses REST API: https://docs.github.com/en/rest/commits/statuses
- Required status checks: https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/collaborating-on-repositories-with-code-quality-features/troubleshooting-required-status-checks

Current important facts:

- Use a GitHub App installation token rather than a personal access token.
- Installation tokens expire after one hour, so the Worker should generate them on demand.
- Use `GET /repos/{owner}/{repo}/pulls/{pull_number}` to inspect PR state and mergeability.
- GitHub computes PR mergeability and exposes it as the `mergeable` key. It may be `null` while GitHub is still computing.
- Use `GET /repos/{owner}/{repo}/commits/{ref}/check-runs` to inspect check runs for the PR head SHA.
- Use `GET /repos/{owner}/{repo}/commits/{ref}/status` for combined commit statuses.
- Required checks must pass against the latest commit SHA before a PR should be merged.
- Successful check conclusions include `success`, `skipped`, and `neutral`.
- Use `PUT /repos/{owner}/{repo}/pulls/{pull_number}/merge` to merge a PR.
- Pass the PR head `sha` when merging to avoid merging a changed PR by accident.
- Supported `merge_method` values are `merge`, `squash`, and `rebase`.
- Use `PATCH /repos/{owner}/{repo}/pulls/{pull_number}` with `state: "closed"` to close abandoned PRs.

---

## 4. Recommended architecture

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

Recommended Cloudflare products:

- **Cloudflare Access**: protects `autopilot.bysander.net`.
- **Cloudflare Worker**: serves the UI and API routes.
- **Cloudflare Workflows**: durable execution for each AI coding run.
- **Cloudflare D1**: stores repo configs, run logs, locks, quotas, PR metadata.
- **Cloudflare Cron Trigger**: wakes the scheduler periodically.
- **Cloudflare Secrets**: stores Jules and GitHub credentials.

Do not use a normal public API key login page. Access is the login layer.

---

## 5. Security model

### 5.1 Access protection

Create a Cloudflare Access application:

- Type: **Self-hosted and private**
- Hostname: `autopilot.bysander.net`
- Policy: allow only Sander’s email address
- Session duration: reasonable, e.g. 24 hours
- Require 2FA via the identity provider

Inside the Worker, still validate the Access JWT for API routes.

Required environment variables / secrets:

```text
TEAM_DOMAIN=https://<your-team>.cloudflareaccess.com
POLICY_AUD=<Cloudflare Access application AUD tag>
ALLOWED_ADMIN_EMAIL=<your email>
```

Use the `Cf-Access-Jwt-Assertion` header and validate it with `jose`. After validation, check the payload email against `ALLOWED_ADMIN_EMAIL`.

### 5.2 Secrets

Store these as Cloudflare Worker secrets:

```text
JULES_API_KEY
GITHUB_APP_ID
GITHUB_APP_PRIVATE_KEY_B64
GITHUB_APP_INSTALLATION_ID
ALLOWED_ADMIN_EMAIL
POLICY_AUD
TEAM_DOMAIN
```

Use commands like:

```bash
npx wrangler secret put JULES_API_KEY
npx wrangler secret put GITHUB_APP_ID
npx wrangler secret put GITHUB_APP_PRIVATE_KEY_B64
npx wrangler secret put GITHUB_APP_INSTALLATION_ID
npx wrangler secret put ALLOWED_ADMIN_EMAIL
npx wrangler secret put POLICY_AUD
npx wrangler secret put TEAM_DOMAIN
```

Recommended handling for the GitHub App private key:

```bash
base64 -w 0 private-key.pem | npx wrangler secret put GITHUB_APP_PRIVATE_KEY_B64
```

On macOS, use:

```bash
base64 < private-key.pem | tr -d '\n' | npx wrangler secret put GITHUB_APP_PRIVATE_KEY_B64
```

Never return secrets from API routes. Never log secrets. Redact all authorization headers and API keys in errors.

### 5.3 Merge safety

Autopilot may only merge a PR when all of these are true:

1. The PR URL came from a tracked Jules session output.
2. The PR belongs to the configured GitHub owner/repo.
3. The PR is open.
4. The PR is not a draft.
5. The PR has the label `autopilot` or `autopilot:jules`.
6. The PR head branch looks like it was created by Jules, or at least is linked to the tracked session.
7. GitHub says `mergeable === true`.
8. The PR is not behind in a way that blocks merging.
9. Required CI/checks are passing.
10. The PR head SHA has not changed between inspection and merge.
11. The repository config has `auto_merge_enabled = true`.
12. The run has not been manually cancelled.

Use the merge endpoint with the `sha` field set to the PR head SHA.

### 5.4 Human PR isolation

Never touch human-created PRs.

A PR is considered Autopilot-owned only if:

```text
tracked run has pr_url = this PR URL
AND PR has label autopilot or autopilot:jules
AND repository matches run.repo_config_id
```

Do not merge or close any PR that does not match this rule.

---

## 6. GitHub App setup

Create a private GitHub App named something like:

```text
bysander-autopilot
```

Install it only on selected repositories.

Recommended permissions:

```text
Repository metadata: read
Pull requests: read/write
Contents: read/write
Checks: read
Commit statuses: read
Issues: read/write
```

Why `Issues: read/write`? GitHub labels and comments on pull requests use issue APIs because pull requests are issue-like resources.

Required app settings:

- Webhook: not required for MVP; use polling first.
- Callback URL: not required unless OAuth is added later.
- Private key: generate and store as `GITHUB_APP_PRIVATE_KEY_B64`.
- App ID: store as `GITHUB_APP_ID`.
- Installation ID: store as `GITHUB_APP_INSTALLATION_ID`.

MVP should use polling instead of GitHub webhooks because the app is behind Cloudflare Access. Webhooks can be added later with a separate service-auth-protected endpoint.

---

## 7. Jules setup

1. Install/configure the Jules GitHub integration through the Jules web app.
2. Generate a Jules API key in Jules settings.
3. Store it as a Worker secret named `JULES_API_KEY`.
4. Use `GET /v1alpha/sources` to list repositories Jules can access.
5. Use `POST /v1alpha/sessions` with `automationMode: "AUTO_CREATE_PR"` to run coding sessions that create PRs automatically.

Example session creation payload:

```json
{
  "prompt": "Implement the next small safe improvement for this repository. Keep the change focused. Do not perform broad rewrites.",
  "sourceContext": {
    "source": "sources/github/OWNER/REPO",
    "githubRepoContext": {
      "startingBranch": "main"
    }
  },
  "automationMode": "AUTO_CREATE_PR",
  "requirePlanApproval": false,
  "title": "Autopilot run: OWNER/REPO"
}
```

For fully unattended operation, use `requirePlanApproval: false`. If this is set to `true`, Autopilot must either stop for manual approval or explicitly call `approvePlan`; do not silently auto-approve unless the repository config says so.

---

## 8. Cloudflare project setup

Recommended stack:

```text
TypeScript
React
Vite
Cloudflare Workers
Cloudflare Workflows
Cloudflare D1
Vitest
Hono optional, but useful for API routing
```

Initialize:

```bash
npm create cloudflare@latest autopilot -- --framework=react
cd autopilot
npm install
npm install jose @octokit/auth-app @octokit/request zod
npm install -D vitest @testing-library/react @testing-library/jest-dom jsdom
```

Create D1 database:

```bash
npx wrangler d1 create autopilot-db --location weur --jurisdiction eu --binding DB --update-config
```

Create migration:

```bash
mkdir -p migrations
npx wrangler d1 migrations create autopilot-db init
```

Apply locally:

```bash
npx wrangler d1 migrations apply autopilot-db --local
```

Apply remotely:

```bash
npx wrangler d1 migrations apply autopilot-db --remote
```

Generate Worker types:

```bash
npx wrangler types
```

---

## 9. `wrangler.jsonc` skeleton

Use `wrangler.jsonc`, not `wrangler.toml`, for new projects.

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
  "observability": {
    "enabled": true
  },
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
  "triggers": {
    "crons": [
      "*/5 * * * *"
    ]
  },
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
  "vars": {
    "APP_ENV": "production",
    "PUBLIC_APP_NAME": "Autopilot"
  }
}
```

Notes:

- `compatibility_date` must be at least `2024-10-22` for Workflow bindings.
- The cron trigger is just a wake-up tick. It does not mean “start a run every 5 minutes”. The scheduler must check locks, quota, cooldown, and enabled repo configs.
- Cron triggers are UTC. Store repo policy time windows with explicit timezone, e.g. `Europe/Brussels`.

---

## 10. Suggested repository structure

```text
autopilot/
  src/
    App.tsx
    main.tsx
    routes/
    components/
      Layout.tsx
      RepoList.tsx
      RepoConfigForm.tsx
      RunHistory.tsx
      RunDetail.tsx
      SafetyStatus.tsx
    lib/
      apiClient.ts
      types.ts
  worker/
    index.ts
    workflows/
      AutopilotRunnerWorkflow.ts
    api/
      routes.ts
      auth.ts
      validators.ts
    agents/
      AgentProvider.ts
      jules/
        JulesProvider.ts
        julesTypes.ts
    github/
      GitHubClient.ts
      githubAuth.ts
      prSafety.ts
    scheduler/
      tick.ts
      quota.ts
      locks.ts
      policies.ts
    db/
      schema.ts
      repositories.ts
      runs.ts
      locks.ts
      events.ts
    util/
      errors.ts
      redaction.ts
      time.ts
      ids.ts
  migrations/
    0001_init.sql
  test/
  wrangler.jsonc
  package.json
  README.md
```

---

## 11. Database schema

Create `migrations/0001_init.sql`.

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

Use these run states:

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

The project is called `autopilot`, not `jules-autopilot`, so the internal design should support multiple agents later.

Create `worker/agents/AgentProvider.ts`:

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

## 13. Jules provider implementation

Create `worker/agents/jules/JulesProvider.ts`.

Base URL:

```ts
const JULES_BASE_URL = 'https://jules.googleapis.com/v1alpha';
```

Authentication:

```ts
headers: {
  'content-type': 'application/json',
  'x-goog-api-key': env.JULES_API_KEY,
}
```

Methods:

```ts
GET /sources?pageSize=100
GET /sessions?pageSize=100
GET /{name=sessions/*}
GET /{parent=sessions/*}/activities?pageSize=100
POST /sessions
POST /{session=sessions/*}:approvePlan
POST /{session=sessions/*}:sendMessage
```

Important implementation details:

- Implement pagination for sources, sessions, and activities.
- Redact the Jules API key from all thrown errors.
- Treat HTTP 401/403 as fatal/non-retryable.
- Treat HTTP 429/5xx as retryable.
- Extract PRs from `session.outputs[].pullRequest.url`.
- Also inspect activities as a fallback, but prefer `session.outputs` when present.
- Do not try to delete or cancel sessions; not documented.

---

## 14. GitHub client implementation

Use a GitHub App installation token.

Create `worker/github/githubAuth.ts`:

- Decode `GITHUB_APP_PRIVATE_KEY_B64`.
- Use `@octokit/auth-app` or manually generate JWT.
- Generate an installation token using the installation ID.
- Cache the installation token in memory until near expiry, but do not persist it to D1.

Create `worker/github/GitHubClient.ts` with methods:

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

Use headers:

```ts
Accept: application/vnd.github+json
Authorization: Bearer <installation-token>
X-GitHub-Api-Version: 2026-03-10
```

Merge request:

```ts
PUT /repos/{owner}/{repo}/pulls/{pull_number}/merge

{
  "commit_title": "Autopilot: <PR title>",
  "commit_message": "Merged by Autopilot after Jules completed and required checks passed.",
  "sha": "<current PR head SHA>",
  "merge_method": "squash"
}
```

Close abandoned PR:

```ts
PATCH /repos/{owner}/{repo}/pulls/{pull_number}

{
  "state": "closed"
}
```

---

## 15. Pull request safety module

Create `worker/github/prSafety.ts`.

Expose:

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
```

Return shape:

```ts
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
if PR closed -> cannot merge
if draft -> cannot merge
if base branch differs from repo config -> cannot merge
if label missing -> cannot merge
if mergeable === null -> wait and poll again
if mergeable === false -> cannot merge; maybe update branch if enabled later
if any required/observed checks pending -> wait
if any check failed -> wait, because Jules may try to fix it
if failed checks remain past timeout -> mark manual_review_required or close PR based on config
if combined status failure -> wait/fail
if all checks success/skipped/neutral and mergeable true -> can merge
```

MVP simplification:

- If branch protection exists, GitHub merge endpoint will still enforce it.
- Still inspect checks before calling merge to avoid noisy merge attempts.
- Do not implement branch update automatically in MVP. Add later if needed.

---

## 16. Workflow implementation

Create `worker/workflows/AutopilotRunnerWorkflow.ts`.

Workflow input:

```ts
type AutopilotWorkflowParams = {
  runId: string;
  repoConfigId: string;
  triggerKind: 'manual' | 'cron' | 'retry';
};
```

Workflow steps:

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
        await step.do('mark timed out', async () => {
          await markRunTimedOut(this.env.DB, runId);
        });
        return;
      }

      if (!prUrl) {
        await step.do('mark no PR created', async () => {
          await markRunNoPrCreated(this.env.DB, runId);
        });
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

Important Workflows rules:

- Put external API calls inside `step.do`.
- Make every `step.do` idempotent.
- Persist important intermediate state to D1.
- Use deterministic step names in loops.
- Handle retryable vs non-retryable errors explicitly.
- Treat auth failures as non-retryable.

---

## 17. Scheduler tick

The cron trigger should call `scheduled()` in `worker/index.ts` every 5 minutes.

Scheduler responsibilities:

```text
1. Load enabled repo configs.
2. Skip repo if already locked.
3. Skip repo if daily quota exhausted.
4. Skip repo if outside configured run window.
5. Skip repo if min_delay_minutes has not elapsed since last terminal run.
6. Create a run row with state queued.
7. Trigger AUTOPILOT_RUNNER workflow with deterministic ID.
```

Example deterministic workflow ID:

```text
autopilot:<repo_config_id>:<run_id>
```

If Workflow creation returns “already exists”, do not create a duplicate run.

Pseudo-code:

```ts
export async function schedulerTick(env: Env) {
  const configs = await listEnabledRepoConfigs(env.DB);

  for (const config of configs) {
    const decision = await shouldStartRun(env.DB, config, new Date());
    if (!decision.start) continue;

    const run = await createQueuedRun(env.DB, config, 'cron');

    await env.AUTOPILOT_RUNNER.create({
      id: `autopilot:${config.id}:${run.id}`.slice(0, 100),
      params: {
        runId: run.id,
        repoConfigId: config.id,
        triggerKind: 'cron'
      }
    });
  }
}
```

---

## 18. Worker API routes

All `/api/*` routes require validated Cloudflare Access identity.

Suggested routes:

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

Do **not** expose endpoints for arbitrary GitHub operations. Every action should map to a configured repo and tracked run.

---

## 19. UI requirements

Build a simple private dashboard.

### 19.1 Home / dashboard

Show:

- Enabled repositories
- Active runs
- Runs today
- Daily quota used
- Failed/stuck runs
- Recent merges
- Emergency global pause toggle

### 19.2 Repository list

Show:

- GitHub owner/repo
- branch
- agent kind
- enabled/disabled
- daily limit
- active run state
- last run result
- auto-merge enabled/disabled

### 19.3 Add repository

Flow:

1. Load Jules sources.
2. Select source/repository.
3. Select branch.
4. Enter prompt template.
5. Set daily quota.
6. Set max runtime.
7. Set run window.
8. Choose auto-merge mode.
9. Save disabled by default.
10. Require explicit “Enable automation”.

### 19.4 Repository detail

Show:

- Config form
- Enable/disable button
- Run now button
- Active run panel
- Run history
- Latest PR
- Quota usage
- Last errors

### 19.5 Run detail

Show:

- Run state
- Jules session URL
- GitHub PR URL
- Event timeline
- Current checks
- Mergeability
- Failure reason
- Manual actions:
  - cancel local tracking
  - retry run
  - close tracked PR
  - mark manual review

---

## 20. Prompt template design

Each repo config has a `prompt_template`.

Support variables:

```text
{{owner}}
{{repo}}
{{branch}}
{{date}}
{{sequence}}
{{daily_limit}}
{{previous_run_summary}}
{{last_failure_reason}}
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

Add later: per-repo backlog items, labels, issue picking, documentation-only mode, test-fix mode.

---

## 21. Failure handling

### Jules session fails

If Jules state is `FAILED`:

- Mark run `agent_failed`.
- Store failure reason if activity includes `sessionFailed.reason`.
- Count attempt against quota.
- If PR already exists, do not close immediately unless `close_failed_prs = true` and PR is clearly tracked.

### Jules session hangs

If max runtime exceeded:

- Mark run `timed_out`.
- Release repo lock.
- If a tracked PR exists:
  - Add comment explaining Autopilot timed out.
  - If `close_failed_prs = true`, close PR.
  - Otherwise mark `manual_review_required`.
- Do not try to delete Jules session because delete/cancel is not documented.

### CI fails

If checks fail:

- Keep polling for a while, because Jules may continue fixing the PR.
- If failure remains until PR wait timeout:
  - Mark `manual_review_required` by default.
  - Do not merge.
  - Optionally close the PR only if configured.

### Merge conflict

If `mergeable === false`:

- Mark `manual_review_required` in MVP.
- Do not auto-update branch in MVP.
- Later add optional “update branch” support via GitHub API.

### GitHub API rate limits / secondary rate limits

- Use conservative polling intervals, e.g. 2 minutes.
- Back off on 403/429 with rate-limit headers.
- Do not spam merge attempts.

---

## 22. Quota model

Failed Jules sessions count against the user’s Jules quota, so Autopilot must count **attempts**, not only successful merges.

Definitions:

```text
daily_attempt_limit: max Jules sessions Autopilot may attempt per repo per local day
sessions_created: number of actual Jules sessions created
successful_merges: number of PRs merged
failures: number of failed/timed-out/manual-review runs
```

Quota date should use the repo config timezone, default `Europe/Brussels`.

Reserve quota before calling Jules:

```text
1. Ensure daily counter row exists.
2. Atomically increment attempts only if attempts < daily_attempt_limit.
3. If no row changed, stop.
4. Only then create Jules session.
```

If session creation fails before Jules accepts it, decide whether to decrement `attempts`. Conservative choice: do not decrement unless the API clearly returned no session and no quota-consuming work happened.

---

## 23. Locking model

Use `repo_locks` to prevent concurrent runs per repository.

Rules:

```text
- repo_config_id is primary key.
- acquire lock before creating Jules session.
- lock has expires_at to recover from broken workflows.
- scheduler must skip locked repos unless lock expired.
- workflow must release lock in finally step.
```

Acquire behavior:

```text
if no lock -> insert lock
if lock expired -> replace lock and add warning event
if active lock exists -> stop run as skipped/duplicate
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

For local development, add a dev-only auth bypass:

```text
if APP_ENV === 'development' and request has header x-dev-user-email matching ALLOWED_ADMIN_EMAIL -> allow
```

Never enable that bypass in production.

Run locally:

```bash
npm run dev
```

Test scheduled handler locally:

```bash
npx wrangler dev --test-scheduled
curl "http://localhost:8787/__scheduled?cron=*/5+*+*+*+*"
```

---

## 25. Testing requirements

Use Vitest.

Test at least:

### Security

- Access JWT missing -> API returns 403.
- Wrong email -> API returns 403.
- Secrets are never included in error responses.
- API key redaction works.

### Scheduler

- Disabled repo does not start.
- Locked repo does not start.
- Quota-exhausted repo does not start.
- Repo outside run window does not start.
- Repo with quota and no lock starts exactly one Workflow.

### Workflow

- Creates Jules session once.
- Polls until completed.
- Handles `FAILED` session.
- Handles timeout.
- Extracts PR URL from session outputs.
- Releases lock on success and failure.

### GitHub safety

- Human PR without label is never merged.
- Tracked PR with failing checks is not merged.
- Tracked PR with pending checks is not merged.
- Tracked PR with `mergeable === null` is not merged yet.
- Tracked PR with passing checks and `mergeable === true` is merged with expected SHA.
- SHA mismatch prevents merge.

### Database

- Quota reservation is atomic enough for duplicate scheduler ticks.
- Expired lock can be replaced.
- Active lock cannot be replaced.

---

## 26. Implementation phases

### Phase 1 — private dashboard shell

Deliver:

- React UI on `autopilot.bysander.net`.
- Cloudflare Access protection.
- Worker validates Access JWT.
- D1 connected.
- `/api/me`, `/api/health`.
- Basic dashboard layout.

### Phase 2 — Jules source browser

Deliver:

- Jules API client.
- `/api/jules/sources`.
- UI shows connected Jules repositories and branches.
- Redacted error handling.

### Phase 3 — repo config

Deliver:

- Add repo config from Jules source.
- Edit prompt, branch, quota, runtime, run window.
- Enable/disable automation.
- Store config in D1.

### Phase 4 — manual run

Deliver:

- “Run now” button.
- Create run row.
- Trigger Workflow.
- Workflow creates Jules session.
- Polls until terminal state.
- Stores run events.
- Shows Jules session URL.

No merging yet.

### Phase 5 — PR tracking

Deliver:

- Extract PR URL from Jules session output.
- Parse GitHub owner/repo/pull number from URL.
- Add `autopilot` and `autopilot:jules` labels.
- Add PR comment.
- Show PR in UI.

No merging yet.

### Phase 6 — CI-aware merge

Deliver:

- GitHub App auth.
- Check PR mergeability.
- Check status/check runs.
- Merge only if safe.
- Pass PR head SHA when merging.
- Store result.

Start with auto-merge disabled by default. Enable manually per repo.

### Phase 7 — scheduler

Deliver:

- Cron tick every 5 minutes.
- Per-repo lock.
- Daily quota.
- Min delay.
- Run window.
- Automatic run creation.

### Phase 8 — cleanup and manual controls

Deliver:

- Mark run cancelled.
- Retry failed run.
- Close tracked PR.
- Global pause.
- Manual review state.

### Phase 9 — multi-agent architecture

Deliver later:

- Abstract agent provider registry.
- Add another agent provider behind the same `AgentProvider` interface.

---

## 27. Acceptance criteria for MVP

The MVP is complete when:

1. `autopilot.bysander.net` is protected by Cloudflare Access.
2. The Worker rejects unauthenticated API requests.
3. Secrets are stored as Cloudflare secrets, not in code or D1.
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
17. Tests cover the scheduler, lock, quota, auth, and merge safety logic.

---

## 28. Non-goals for the first version

Do not implement these in MVP:

- Public user accounts.
- Multi-tenant SaaS behavior.
- Billing.
- Browser-stored API keys.
- Undocumented Jules schedule endpoints.
- Scraping the Jules web UI.
- Auto-updating PR branches.
- GitHub webhooks.
- Automatic issue selection.
- Autonomous large refactors.
- Automatic deletion/cancellation of Jules sessions.

---

## 29. Important cautions

This tool will have authority to create AI coding sessions and merge code. Treat it like production infrastructure.

Start with:

```text
one repo
manual runs only
auto-merge disabled
observe behavior
then enable merge
then enable scheduler
then add more repos
```

The danger is not creating Jules sessions. The danger is giving a bot merge authority without enough guardrails.

