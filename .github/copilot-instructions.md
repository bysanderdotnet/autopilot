# Instructions for GitHub Copilot

Read `AGENTS.md` in the repo root and follow it — it is the agent operating
manual. `AGENTS.md` is authoritative; the points below mirror its core for
Copilot surfaces that cannot open repository files.

- One tool guides the workflow: `./AGENTS.sh init` at session start. On a
  fresh project it walks guided setup; otherwise it reports state and the
  next step. Follow its `next:` hints. Stuck → `./AGENTS.sh help`.
- Done means `./AGENTS.sh verify` exits 0. No green run, no "done".
- Session end: `./AGENTS.sh handoff` — clear every open item.
- One feature per session/commit. No drive-by refactors.
- State (session log, feature list, rules) is managed through the CLI (`log`,
  `feature ...`, `docs ...`, `cmd ...`) — never hand-edit `.agents/agents.json`.
- CI (`.github/workflows/`) is human-owned — never edit it; tell the user
  when it needs changes.
- Treat `.agents/agents.py` and `AGENTS.sh` as harness internals; use
  `./AGENTS.sh help` instead of reading script internals for usage.
- Style: caveman — all agent output (chat, commits, comments, logs, docs) max
  terse. Only exception: the product itself (copy, UI strings, end-user docs).
