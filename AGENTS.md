# Agent Operating Manual

One tool runs the whole workflow, prints next step every turn:

    ./AGENTS.sh init       # start here; follow output
    ./AGENTS.sh help       # stuck, or unsure which command fits

Trust script over memory: walks setup, scope, verification, progress,
handoff. All state in `.agents/agents.json`, CLI-owned — never hand-edit.

## Project

- Name: autopilot
- Stack: TypeScript, React, Vite, Cloudflare Workers/Workflows/D1, Vitest, Hono. Pre-impl: only harness + spec exist now.
- Purpose: Private AI coding control plane behind Cloudflare Access. Runs Google Jules sessions on selected GitHub repos, one active run per repo, detects the PR, waits for CI + mergeability, merges only when safe. Full build spec: README.md.

## Rules

- One feature per session/commit. No drive-by refactors.
- Done = `./AGENTS.sh verify` green. Anything else = "unverified" — say so.
- `AGENTS.sh` / `.agents/agents.py` = harness internals. Usage = `help`,
  not reading or editing source.
- Never commit secrets. Secrets = Cloudflare Worker secrets only, never `vars`/D1/code. Redact api keys + auth headers in all logs/errors.
- Never hand-edit applied `migrations/*.sql`; add a new migration.
- Merge/close only Autopilot-owned PRs (tracked run pr_url + autopilot label + matching repo). Never touch human PRs.

## Skills

Skills = stored playbooks in `.agents/skills/<name>/SKILL.md`; `init` lists them.

- Task matches a skill → follow playbook, don't improvise.
- Just did recurring multi-step task (deploy, release, migration, codegen)?
  Capture as skill NOW, unprompted — scaffold: `./AGENTS.sh skill new <name>`;
  how-to: `.agents/skills/new-skill/SKILL.md`. Next session replays it,
  no re-deriving.

## Style: caveman

All agent output — chat, commits, code comments, logs, docs — max terse.
Drop filler; fragments fine: "Tests green. Lint: 2 unused imports." Exact
paths, commands, numbers; never paraphrase a name. Say once.

Only exception: the product itself (website copy, UI strings, end-user docs).
