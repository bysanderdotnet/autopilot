# Agent Operating Manual

One tool runs the whole workflow, prints next step every turn:

    ./AGENTS.sh init       # start here; follow output
    ./AGENTS.sh help       # stuck, or unsure which command fits

Trust script over memory: walks setup, scope, verification, progress,
handoff. All state in `.agents/agents.json`, CLI-owned — never hand-edit.

## Project

<!-- TODO(setup): fill in, then remove this comment -->
- Name:
- Stack:
- Purpose:

## Rules

- One feature per session/commit. No drive-by refactors.
- Done = `./AGENTS.sh verify` green. Anything else = "unverified" — say so.
- `AGENTS.sh` / `.agents/agents.py` = harness internals. Usage = `help`,
  not reading or editing source.
<!-- TODO(setup): add project no-go zones (e.g. "never edit /migrations"), then remove this comment -->

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
