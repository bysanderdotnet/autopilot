# .agents/ — harness internals

Harness implementation and state live here. Humans and agents normally should
not inspect or edit this directory directly; use the root wrapper instead:

```sh
./AGENTS.sh help
```

| Path | What | Lifecycle |
|---|---|---|
| `agents.py` | Harness CLI implementation — guided setup, sessions, verification, project docs, state | Called through `../AGENTS.sh`; edit rarely (register commands instead) |
| `agents.json` | All durable state: setup progress, registered commands, features, progress log, rules | Via the CLI (`init`, `cmd`, `feature`, `log`, `docs`), never hand-edit |
| `agents.scratch.json` | Transient scratch (last verify result) | Gitignored; written by `./AGENTS.sh verify` |
| `agents.json.lock` | Cross-session advisory lock (POSIX only) | Gitignored; held while the CLI runs |
| `skills/` | Task playbooks (`<name>/SKILL.md`) | Scaffold via `../AGENTS.sh skill new <name>`; how-to in `skills/new-skill/SKILL.md` |

`AGENTS.sh` is the stable public interface. It finds Python and forwards to
`agents.py`, which is deliberately a guide, not just a runner: `init` walks
first-time configuration step by step on a fresh project, then each session
snapshots state and suggests the next action, `verify` runs the registered
definition of done, `docs` shows a
generated repo map plus curated rules (architecture / conventions / testing),
`maintenance` flags what to combine, prune, or re-check, and `handoff` checks
the session is safely closeable. Subcommand details live in
`./AGENTS.sh help`, nowhere else.

`AGENTS.md` (repo root) is the single manual; per-agent entrypoints map to it:
`CLAUDE.md` and `GEMINI.md` are symlinks, Codex reads `AGENTS.md` natively,
and `.github/copilot-instructions.md` points Copilot at it.
`.claude/skills` symlinks to `skills/` so Claude Code auto-discovers them.

Known limitation — Windows: those entrypoints are git symlinks. On a Windows
checkout without symlink support they materialize as plain text files and
`./AGENTS.sh check` FAILs on them. Enable Developer Mode (or run git
elevated) and clone with `git clone -c core.symlinks=true <url>`; an existing
checkout can be repaired with `git config core.symlinks true` +
`git checkout -- CLAUDE.md GEMINI.md .claude/skills`. The cross-session state
lock (`agents.json.lock`) is also POSIX-only and skipped on Windows.
`.claude/settings.json` wires a SessionStart hook that auto-runs
`./AGENTS.sh init`. `.github/workflows/agents.yml` runs `./AGENTS.sh ci` —
same gates, enforced remotely.

Design principles:

- Instructions: short AGENTS.md; details in `./AGENTS.sh help`, loaded on demand.
- State: one JSON file behind a CLI → sessions resume, never cold-start, and
  agents can't corrupt state by hand-editing.
- Docs: the repo map is generated (never drifts); only rules are curated, and
  `maintenance` keeps them small and current.
- Verification: machine-checkable done; registered commands, not vibes.
- Scope: one feature at a time, enforced by the CLI.
- Lifecycle: guided setup once, init at start, handoff checklist at end.
