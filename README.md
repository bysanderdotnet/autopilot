# autopilot

Private AI coding control plane. Lets one trusted user run automated
[Google Jules](https://jules.google) coding sessions on selected GitHub
repositories: create a Jules session, wait for it to finish, detect the PR it
opens, wait for CI + mergeability, merge only when safe, then start the next run
if daily quota remains.

Not a public SaaS — a private automation dashboard for repos controlled by the
owner, served at `autopilot.bysander.net` behind Cloudflare Access.

> **Status: pre-implementation.** This repo currently holds the spec
> (`autopilot_implementation_instructions.md`) and the agent harness. Application
> code is not built yet. The spec is the source of truth for design.

## Core design

Per-repository **sequential** runner — at most one active AI coding run per repo
at a time:

```text
repo enabled -> no active run? -> daily quota left?
  -> create one Jules session -> wait for finish -> detect PR
  -> wait for CI + mergeability -> merge or fail safely
  -> release repo lock -> maybe start next run later
```

A 5-minute cron is only a wake-up tick; the scheduler checks locks, quota,
cooldown, and run windows before starting anything.

## Planned stack

| Layer | Tech |
|---|---|
| UI | React + Vite (Cloudflare Workers Assets, SPA) |
| API + orchestration | Cloudflare Worker (TypeScript, Hono optional) |
| Durable runs | Cloudflare Workflows |
| Storage | Cloudflare D1 (repo configs, runs, locks, quotas, events) |
| Scheduling | Cloudflare Cron Trigger |
| Auth | Cloudflare Access + Access JWT validation (`jose`) |
| Agent | Google Jules API (`x-goog-api-key`), `AgentProvider` abstraction |
| Git | GitHub App installation token (`@octokit/auth-app`) |
| Tests | Vitest |

## Security model (non-negotiable)

- Cloudflare Access protects the hostname; Worker still validates the Access JWT
  on every `/api/*` route and checks email against `ALLOWED_ADMIN_EMAIL`.
- Secrets live as Cloudflare Worker secrets only — never in `vars`, D1, or code.
  Never log or return secrets; redact api keys + auth headers in all errors.
- Merge/close only Autopilot-owned PRs: tracked run `pr_url` **and** `autopilot`
  (or `autopilot:jules`) label **and** matching repo. Human PRs are never
  touched. Merge passes the PR head `sha` and only when CI + mergeability pass.

Full design, DB schema, API surface, and phased plan:
[`autopilot_implementation_instructions.md`](./autopilot_implementation_instructions.md).

## Working in this repo

The agent harness drives the workflow. Use the wrapper, not the `.agents/`
internals:

```sh
./AGENTS.sh init       # session start: status + next step
./AGENTS.sh verify     # definition of done
./AGENTS.sh handoff    # end-of-session checklist
./AGENTS.sh help       # all commands
```

State lives in `.agents/agents.json` (CLI-owned, never hand-edit). Agent rules:
`AGENTS.md`. One feature per session/commit; done means `./AGENTS.sh verify` is
green.
