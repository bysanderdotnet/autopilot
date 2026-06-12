#!/usr/bin/env python3
"""agents.py — agent harness. One CLI guides whole workflow.

Don't read or change this file for project work. Usage: ./AGENTS.sh help

Subcommands (details + examples: ./AGENTS.sh help <command>):

    init         session start: health check + state snapshot; fresh project →
                 guided setup until complete (hook/CI run this)
    verify       run registered definition of done; records result
    handoff      end-of-session checklist, live status
    feature      scope: list / add / start / done / block / note
    log          record progress entry (auto-stamps date, commit, verify result)
    progress     show recent progress entries (display bounded, never compact by hand)
    docs         live project docs: generated repo map + curated rules
    skill        scaffold a new skill playbook / list discovered skills
    maintenance  health sweep: update, combine, prune, re-check
    cmd          register project commands: set / rm / list
    run          run one registered command by name
    check        structure/state validation only
    ci           what CI runs: check, then init + verify once setup complete

Stdlib only; Python 3.8+. Durable state: .agents/agents.json; scratch:
.agents/agents.scratch.json (gitignored). Both owned by this script — never
hand-edit. New build/test/lint commands → `cmd set`, not edits here.
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

SCRIPT = "./AGENTS.sh"


def find_root():
    # This file lives at <root>/.agents/agents.py. Anchor on the script, not
    # the caller's cwd: invoked via absolute path from inside another repo,
    # a cwd-based root would read/write that repo's files.
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


ROOT = find_root()
CONFIG_PATH = os.path.join(ROOT, ".agents", "agents.json")
SCRATCH_PATH = os.path.join(ROOT, ".agents", "agents.scratch.json")
SKILLS_DIR = os.path.join(ROOT, ".agents", "skills")

PROGRESS_DEFAULT_SHOWN = 5   # entries shown by `progress` / referenced by `init`
RULE_CATEGORIES = ("architecture", "conventions", "testing")
FEATURE_STATUSES = ("in_progress", "todo", "blocked", "done")
RULES_SOFT_CAP = 12          # per category; above this, maintenance says combine/prune
RULE_STALE_DAYS = 90         # rules older than this get flagged for a re-check
TREE_MAX_DEPTH = 3           # repo map: directories deeper than this are collapsed
TREE_MAX_ENTRIES = 12        # repo map: entries shown per directory
VERIFY_SLOW_SECS = 60        # verify steps slower than this get flagged by maintenance
SKILL_BODY_MAX_LINES = 80    # quality bar from skills/new-skill/SKILL.md, checked by `skill lint`
LOCK_WAIT_SECS = 5           # how long to wait for another session's state lock


# ---------- small helpers ----------

def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def git(*args):
    out = subprocess.run(["git", *args], capture_output=True, text=True, cwd=ROOT)
    return out.stdout.strip() if out.returncode == 0 else ""


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def tip(msg):
    print(f"next: {msg}")


# ---------- state (single file: agents.json; scratch: agents.scratch.json) ----------

_LOCK_FH = None  # kept open for the process lifetime; OS releases on exit


def acquire_state_lock():
    """agents.json updates are read-modify-write: two parallel sessions would
    silently lose each other's writes. Exclusive advisory lock, taken on first
    state load and held until the process exits. POSIX only (like the exe
    checks); elsewhere this is a no-op."""
    global _LOCK_FH
    if _LOCK_FH is not None or os.name != "posix":
        return
    import fcntl
    lock_path = CONFIG_PATH + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fh = open(lock_path, "w")
    deadline = time.monotonic() + LOCK_WAIT_SECS
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _LOCK_FH = fh
            return
        except OSError:
            if time.monotonic() >= deadline:
                fh.close()
                die(f"another {SCRIPT} process holds {lock_path} — "
                    "wait for it to finish (stale after a crash: remove the file)")
            time.sleep(0.2)

def section_error(cfg):
    """Wrong-typed sections (hand-edit damage); None when the shape is sane."""
    for key, typ in (("setup", dict), ("commands", dict), ("features", list),
                     ("progress", list), ("rules", list)):
        if key in cfg and not isinstance(cfg[key], typ):
            kind = "object" if typ is dict else "array"
            return f"'{key}' must be a JSON {kind}"
    for key in ("features", "progress", "rules"):
        if any(not isinstance(x, dict) for x in cfg.get(key, []) or []):
            return f"'{key}' entries must be JSON objects"
    if any(not isinstance(v, dict) for v in (cfg.get("commands") or {}).values()):
        return "'commands' entries must be JSON objects"
    setup = cfg.get("setup")
    if isinstance(setup, dict) and "done" in setup and not isinstance(setup["done"], list):
        return "'setup.done' must be a JSON array"
    return None


def load_config():
    """All durable harness state. Top-level keys are independent sections so
    future harness versions can add more without migrations."""
    acquire_state_lock()
    if not os.path.isfile(CONFIG_PATH):
        die(".agents/agents.json missing. "
            "Restore from git history or re-copy from the template — never hand-edit.")
    try:
        cfg = load_json(CONFIG_PATH, default=None)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        die(f".agents/agents.json not valid JSON: {e}. "
            "Restore from git history — never hand-edit.")
    if not isinstance(cfg, dict):
        die(".agents/agents.json is not a JSON object. "
            "Restore from git history — never hand-edit.")
    err = section_error(cfg)
    if err:
        die(f".agents/agents.json invalid: {err}. "
            "Restore from git history — never hand-edit.")
    for key, default in (("commands", {}), ("features", []),
                         ("progress", []), ("rules", [])):
        cfg.setdefault(key, default)
    return cfg


def save_config(cfg):
    save_json(CONFIG_PATH, cfg)


def load_scratch():
    try:
        data = load_json(SCRATCH_PATH, default=None)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}  # scratch is disposable; a corrupt one is treated as absent
    return data if isinstance(data, dict) else {}


def save_scratch(data):
    save_json(SCRATCH_PATH, data)


def setup_pending(cfg=None):
    """Setup state dict while setup is incomplete, else None."""
    return (cfg or load_config()).get("setup")


def find_feature(feats, fid):
    for f in feats:
        if f.get("id") == fid:
            return f
    return None


def fmt_feature(f):
    s = f"{f.get('id', '?')} — {f.get('title', '?')}"
    if f.get("notes"):
        s += f"  ({f['notes']})"
    return s


# ---------- structure / state checks ----------

SHELL_BUILTINS = {"cd", "export", "set", "source", ".", "exec", "eval",
                  "if", "for", "while", "until", "case", "test", "["}


def missing_command_exes(cmds):
    """(name, exe) pairs whose executable doesn't resolve. Catches dead
    absolute paths — e.g. a toolchain registered from /tmp in an ephemeral
    session — before verify fails with a cryptic shell error."""
    if os.name != "posix":
        return []  # registered commands run through POSIX sh; skip elsewhere
    missing = []
    for name, c in cmds.items():
        try:
            tokens = shlex.split(c.get("run", ""))
        except ValueError:
            continue
        exe = next((t for t in tokens if "=" not in t), None)  # skip FOO=1 prefixes
        if not exe or exe in SHELL_BUILTINS:
            continue
        if os.path.sep in exe:
            ok = os.path.exists(exe if os.path.isabs(exe)
                                else os.path.join(ROOT, exe))
        else:
            ok = shutil.which(exe) is not None
        if not ok:
            missing.append((name, exe))
    return missing


def collect_problems():
    """Return (fails, warns) about harness structure and state."""
    fails, warns = [], []

    def need_file(rel):
        if not os.path.isfile(os.path.join(ROOT, rel)):
            fails.append(f"{rel} missing")

    def need_link(rel):
        if not os.path.islink(os.path.join(ROOT, rel)):
            fails.append(f"{rel} symlink missing (Windows checkout? re-clone with "
                         "-c core.symlinks=true — see .agents/README.md)")

    need_file("AGENTS.md")
    need_link("CLAUDE.md")
    need_link("GEMINI.md")
    need_link(".claude/skills")
    need_file(".github/copilot-instructions.md")
    need_file(".agents/agents.json")

    cfg = None
    if os.path.isfile(CONFIG_PATH):
        try:
            cfg = load_json(CONFIG_PATH)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            fails.append(f".agents/agents.json is not valid JSON: {e}")
    if isinstance(cfg, dict):
        err = section_error(cfg)
        if err:
            fails.append(f".agents/agents.json invalid: {err} — "
                         "restore from git history, never hand-edit")
            cfg = None
    if isinstance(cfg, dict):
        wip = [f for f in cfg.get("features", []) if f.get("status") == "in_progress"]
        if len(wip) > 1:
            warns.append("%d features in_progress (policy: max 1): %s"
                         % (len(wip), ", ".join(f.get("id", "?") for f in wip)))
        unknown = [f"{f.get('id', '?')} ('{f.get('status')}')"
                   for f in cfg.get("features", [])
                   if f.get("status", "todo") not in FEATURE_STATUSES]
        if unknown:
            warns.append("feature(s) with unknown status (hand-edit damage?): "
                         + ", ".join(unknown)
                         + f" — repair via {SCRIPT} feature start/done/block")
        for cat in RULE_CATEGORIES:
            n = sum(1 for r in cfg.get("rules", []) if r.get("category") == cat)
            if n > RULES_SOFT_CAP:
                warns.append(f"{n} {cat} rules (soft cap {RULES_SOFT_CAP}) — "
                             f"combine/prune: {SCRIPT} maintenance")
        for name, exe in missing_command_exes(cfg.get("commands") or {}):
            warns.append(f"command '{name}': executable '{exe}' not found — "
                         f"install the toolchain or re-register: {SCRIPT} cmd set {name} \"...\"")

    if os.path.isdir(SKILLS_DIR):
        for name in sorted(os.listdir(SKILLS_DIR)):
            d = os.path.join(SKILLS_DIR, name)
            if not os.path.isdir(d):
                continue
            md = os.path.join(d, "SKILL.md")
            if not os.path.isfile(md):
                warns.append(f".agents/skills/{name}/ has no SKILL.md")
            elif not skill_description(md):
                warns.append(f".agents/skills/{name}/SKILL.md missing 'description:' frontmatter")
            elif skill_description(md).startswith("TODO"):
                warns.append(f".agents/skills/{name}/SKILL.md description still the "
                             "scaffold TODO — fill it in")

    if not git("rev-parse", "--is-inside-work-tree"):
        warns.append("not a git checkout — repo map, verify staleness tracking, "
                     "and the setup marker scan are degraded")
    return fails, warns


def skill_description(md_path):
    """First 'description:' line in the frontmatter block."""
    try:
        with open(md_path, encoding="utf-8") as fh:
            in_frontmatter = False
            for i, line in enumerate(fh):
                if line.strip() == "---":
                    if in_frontmatter:
                        break
                    in_frontmatter = i == 0
                elif in_frontmatter and line.startswith("description:"):
                    return line.partition(":")[2].strip()
    except OSError:
        pass
    return ""


def list_skills():
    rows = []
    if os.path.isdir(SKILLS_DIR):
        for name in sorted(os.listdir(SKILLS_DIR)):
            md = os.path.join(SKILLS_DIR, name, "SKILL.md")
            if os.path.isfile(md):
                rows.append((name, skill_description(md) or "(no description)"))
    return rows


# ---------- guided setup ----------
# Each step: (name, summary, instructions, check). check() returns
# (ok, detail); check=None means the step is confirmed manually.

SETUP_MARKER = "TODO" + "(setup)"  # built dynamically so the finalize scan skips this file cleanly


def _check_project():
    path = os.path.join(ROOT, "AGENTS.md")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return False, "AGENTS.md unreadable"
    # \r?\n: CRLF checkouts (e.g. Windows autocrlf) must not fail the check
    m = re.search(r"^## Project[ \t]*\r?\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not m:
        return False, "AGENTS.md has no '## Project' section"
    section = m.group(1)
    if SETUP_MARKER in section:
        return False, f"AGENTS.md '## Project' still contains {SETUP_MARKER}"
    empty = [field for field in ("Name", "Stack", "Purpose")
             if not re.search(rf"^- {field}:[ \t]*\S", section, re.M)]
    if empty:
        return False, "AGENTS.md '## Project' fields empty: " + ", ".join(empty)
    readme = os.path.join(ROOT, "README.md")
    if not os.path.isfile(readme):
        return False, "README.md missing — write one for the actual project"
    try:
        with open(readme, encoding="utf-8") as fh:
            rtext = fh.read()
    except OSError:
        return False, "README.md unreadable"
    if "Template repository for setting up projects with an AI harness" in rtext:
        return False, "README.md still template text — rewrite for the actual project"
    return True, "AGENTS.md project section filled, README.md rewritten"


def _check_commands():
    cmds = load_config()["commands"]
    n = sum(1 for c in cmds.values() if c.get("verify"))
    if n:
        return True, f"{n} --verify command(s) registered"
    return False, "no --verify commands registered"


def _check_rules():
    rules = load_config()["rules"]
    missing = [c for c in RULE_CATEGORIES
               if not any(r.get("category") == c for r in rules)]
    if missing:
        return False, "no rules yet for: " + ", ".join(missing)
    return True, "rules cover " + ", ".join(RULE_CATEGORIES)


def _check_scope():
    feats = load_config()["features"]
    rest = [f for f in feats if f.get("id") != "F-000"]
    if rest:
        return True, f"{len(rest)} feature(s) seeded"
    return False, "no features besides F-000"


SETUP_STEPS = [
    ("project", "Project identity (AGENTS.md + README.md)", f"""\
1. Fill '## Project' in AGENTS.md: name, stack, purpose (2-4 lines).
   Remove its {SETUP_MARKER} comment.
2. Rewrite README.md for actual project (template text = placeholder).
Infer from codebase first (code, lockfiles, configs, CI); ask user only
what you can't infer (purpose, planned stack on empty repo).""",
     _check_project),

    ("commands", "Register project commands", f"""\
Find real commands (package.json scripts, Makefile, pyproject, CI) and
register — never edit .agents/agents.py itself:
  {SCRIPT} cmd set lint "npm run lint" --verify
  {SCRIPT} cmd set test "npm test" --verify        # --verify = definition of done, cheap/fast first
  {SCRIPT} cmd set deps "npm ci" --init            # --init = session-start smoke check
  {SCRIPT} cmd set dev "npm run dev"               # no flag = on-demand helper
Don't invent commands. CI runs these via {SCRIPT} ci; CI needs toolchain
steps (e.g. node install) → tell user — CI human-owned, never edit.
No code yet? Add feature "set up toolchain + verify commands" in scope
step, mark this one: {SCRIPT} init done commands --force""",
     _check_commands),

    ("rules", "Record project rules (architecture / conventions / testing)", f"""\
Record what agent must know — one terse rule per call:
  {SCRIPT} docs add architecture "<modules, data flow, key dirs>"
  {SCRIPT} docs add conventions "<naming, style, commit format>"
  {SCRIPT} docs add testing "<how to run tests, expectations>"
Infer from codebase. Nothing to record yet (e.g. no tests)? Record that
fact as the rule. Min one rule per category. Repo map generated live by
`{SCRIPT} docs` — don't describe the file tree.""",
     _check_rules),

    ("scope", "Seed the feature list", f"""\
Agree initial features with user, then:
  {SCRIPT} feature add "<title>" [--notes "..."]
One entry per feature, smallest shippable units first.""",
     _check_scope),

    ("guardrails", "Project rules + .gitignore", f"""\
1. Add project no-go zones to '## Rules' in AGENTS.md
   (e.g. "never edit /migrations"). Remove its {SETUP_MARKER} comment.
2. Add stack-specific ignores to .gitignore; replace its {SETUP_MARKER} line.""",
     None),
]


def scan_setup_markers():
    """Tracked files still containing the setup marker (this script excluded)."""
    out = subprocess.run(
        ["git", "grep", "-l", "-F", SETUP_MARKER, "--", ".", ":!.agents/agents.py"],
        capture_output=True, text=True, cwd=ROOT,
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def setup_finalize(cfg):
    """All steps recorded: run the final gates, then lift setup mode."""
    print("-- finalize: all steps recorded, running final gates --")
    blockers = []
    fails, _warns = collect_problems()
    blockers += [f"structure: {f}" for f in fails]
    leftover = scan_setup_markers()
    if leftover:
        blockers.append(f"{SETUP_MARKER} still in: {', '.join(leftover)}")
    steps = [(n, c) for n, c in cfg["commands"].items() if c.get("verify")]
    if steps and not run_verify_steps(steps):
        blockers.append("verify red — fix before completing setup")
    if blockers:
        print("Setup NOT complete:")
        for b in blockers:
            print(f"  BLOCKED: {b}")
        tip(f"fix blockers, rerun: {SCRIPT} init")
        sys.exit(1)

    f000 = find_feature(cfg["features"], "F-000")
    if f000:
        f000["status"] = "done"
    del cfg["setup"]
    cfg["progress"].append({
        "date": now_utc(),
        "title": "project setup",
        "done": "setup complete: %d command(s) registered, %d feature(s) seeded, %d rule(s) recorded"
                % (len(cfg["commands"]),
                   len([f for f in cfg["features"] if f.get("id") != "F-000"]),
                   len(cfg["rules"])),
        "verified": verified_note(),
        "blockers": "none",
        "feature": "F-000",
    })
    save_config(cfg)
    print("== setup COMPLETE (progress entry written, F-000 closed) ==")
    tip("commit everything: git commit -m 'chore: complete project setup'; push if expected")


def setup_flow(cfg, mark_step=None, force=False, unmark_step=None):
    """Guided setup, driven by init while the project is unconfigured.
    Exits 1 while steps remain; returns once setup finalizes so init can
    continue into a normal session."""
    state = cfg["setup"]
    state.setdefault("done", [])
    names = [n for n, *_ in SETUP_STEPS]

    if unmark_step:
        if unmark_step not in names:
            die(f"unknown setup step '{unmark_step}'. Steps: {', '.join(names)}")
        if unmark_step in state["done"]:
            state["done"].remove(unmark_step)
            save_config(cfg)
            print(f"step '{unmark_step}' unmarked."
                  + (" (auto-checked step: re-completes once its check passes)"
                     if SETUP_STEPS[names.index(unmark_step)][3] else ""))
        else:
            print(f"step '{unmark_step}' was not marked done — nothing to undo.")

    if mark_step:
        if mark_step not in names:
            die(f"unknown setup step '{mark_step}'. Steps: {', '.join(names)}")
        _, _, _, check = SETUP_STEPS[names.index(mark_step)]
        if check and not force:
            ok, detail = check()
            if not ok:
                die(f"step '{mark_step}' not done: {detail}. Fix, or override with --force.")
        if mark_step not in state["done"]:
            state["done"].append(mark_step)
            save_config(cfg)
        print(f"step '{mark_step}' recorded.")

    # Status, plus full instructions for the first pending step only —
    # the right information at the right time.
    print("-- SETUP MODE: project not configured yet; finish setup before feature work --")
    pending = []
    for name, summary, _instr, check in SETUP_STEPS:
        if name in state["done"]:
            print(f"  [ok] {name}: {summary}")
            continue
        if check:
            ok, detail = check()
            if ok:
                state["done"].append(name)
                save_config(cfg)
                print(f"  [ok] {name}: {summary} — auto-detected ({detail})")
                continue
            print(f"  [..] {name}: {summary} — {detail}")
        else:
            print(f"  [..] {name}: {summary} — manual confirm")
        pending.append(name)

    if not pending:
        setup_finalize(cfg)
        return
    name = pending[0]
    _n, summary, instructions, check = SETUP_STEPS[names.index(name)]
    print(f"-- current step: {name} — {summary} --")
    print(instructions)
    if check:
        tip(f"step auto-completes once check passes — rerun: {SCRIPT} init")
    else:
        tip(f"manual step — when finished, record: {SCRIPT} init done {name}")
    sys.exit(1)


# ---------- progress rendering ----------

def render_entry(e, indent="  "):
    head = f"{e.get('date', '?')} — {e.get('title', '?')}"
    if e.get("feature"):
        head += f"  [{e['feature']}]"
    lines = [head]
    for key in ("done", "verified", "issues", "next", "blockers"):
        if e.get(key):
            lines.append(f"{indent}{key}: {e[key]}")
    if e.get("commit"):
        lines.append(f"{indent}commit: {e['commit']}")
    return "\n".join(lines)


# ---------- init / verify / check / ci ----------

def cmd_init(args):
    print("== init: session start ==")
    cfg = load_config()

    fails, warns = collect_problems()
    print("-- structure --")
    for w in warns:
        print(f"WARN: {w}")
    for f in fails:
        print(f"FAIL: {f}")
    if not fails:
        print("structure OK")

    in_setup = setup_pending(cfg) is not None
    mark_step = unmark_step = None
    action = getattr(args, "action", None)
    if action in ("done", "undo"):
        if not in_setup:
            print(f"note: setup already complete — 'init {action}' only applies during setup.")
        elif not getattr(args, "step", None):
            die(f"init {action} needs a step name: "
                + ", ".join(n for n, *_ in SETUP_STEPS))
        elif action == "done":
            mark_step = args.step
        else:
            unmark_step = args.step
    if in_setup:
        setup_flow(cfg, mark_step=mark_step, force=getattr(args, "force", False),
                   unmark_step=unmark_step)
        cfg = load_config()  # setup just finalized; continue into a normal session

    print("-- skills (playbooks; follow when task matches) --")
    skills = list_skills()
    for name, desc in skills:
        print(f"  {name}: {desc}")
    if not skills:
        print("  (none)")

    print("-- project docs --")
    rules = cfg["rules"]
    if rules:
        counts = ", ".join(
            f"{sum(1 for r in rules if r.get('category') == c)} {c}"
            for c in RULE_CATEGORIES)
        print(f"  rules: {counts}  (read before coding: {SCRIPT} docs)")
    else:
        print(f"  no rules recorded — {SCRIPT} docs add <category> \"<rule>\"")

    print("-- git --")
    print(git("status", "--short", "--branch") or "(not a git checkout)")
    print(git("log", "--oneline", "-5") or "(no commits yet)")

    print("-- scope --")
    feats = cfg["features"]
    wip = [f for f in feats if f.get("status") == "in_progress"]
    todo = [f for f in feats if f.get("status") == "todo"]
    blocked = [f for f in feats if f.get("status") == "blocked"]
    nxt = todo[0] if todo else None
    if wip:
        print(f"in_progress: {fmt_feature(wip[0])}")
    elif nxt:
        print(f"nothing in_progress. Next todo: {fmt_feature(nxt)}")
    else:
        print("nothing in_progress, no todos left.")
    if blocked:
        print(f"blocked: {len(blocked)} "
              f"({', '.join(f.get('id', '?') for f in blocked)})")

    print("-- progress --")
    entries = cfg["progress"]
    open_blocker = None
    if entries:
        latest = entries[-1]
        print(f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'}. Latest:")
        print(render_entry(latest))
        blockers = latest.get("blockers", "")
        if blockers and blockers.lower() not in ("none", "none.", "no", "-"):
            open_blocker = blockers
        if len(entries) > 1:
            print(f"(older entries: {SCRIPT} progress)")
    else:
        print(f"no entries yet. Record work with: {SCRIPT} log")

    print("-- registered commands --")
    cmds = cfg["commands"]
    if cmds:
        for name, c in cmds.items():
            flags = "".join(f" [{f}]" for f in ("verify", "init") if c.get(f))
            print(f"  {name}: {c.get('run', '?')}{flags}")
    else:
        print(f"  none. Register with: {SCRIPT} cmd set <name> \"<cmd>\" [--verify] [--init]")
    if not any(c.get("verify") for c in cmds.values()):
        print("WARN: no --verify commands registered — `verify` has nothing to run.")

    init_cmds = [(n, c) for n, c in cmds.items() if c.get("init")]
    if init_cmds:
        print("-- init checks --")
        for name, c in init_cmds:
            print(f"-> {name}: {c['run']}")
            rc = subprocess.run(c["run"], shell=True, cwd=ROOT).returncode
            if rc != 0:
                fails.append(f"init check '{name}' failed (exit {rc})")
                print(f"FAIL: init check '{name}' exited {rc}")

    if fails:
        print("== init FAILED: fix FAILs above before feature work. ==")
        sys.exit(1)
    print("== init OK ==")
    if open_blocker:
        tip(f"resolve or re-confirm open blocker first: {open_blocker}")
    if wip:
        tip(f"continue {wip[0].get('id')}; when done: {SCRIPT} verify, then {SCRIPT} handoff")
    elif nxt:
        tip(f"pick ONE item: user request, or {SCRIPT} feature start {nxt.get('id')}")
    else:
        tip(f"no open scope — agree next features with user: {SCRIPT} feature add \"<title>\"")


def run_verify_steps(steps, keep_going=False):
    """Run verify-flagged commands in order; record + return overall result."""
    failures, timings = [], []
    for name, c in steps:
        print(f"-- {name}: {c['run']} --")
        t0 = time.monotonic()
        rc = subprocess.run(c["run"], shell=True, cwd=ROOT).returncode
        timings.append({"name": name, "secs": round(time.monotonic() - t0, 1),
                        "ok": rc == 0})
        if rc != 0:
            failures.append(f"{name} (exit {rc})")
            if not keep_going:
                print(f"FAIL: step '{name}' exited {rc}; remaining steps skipped "
                      "(run them all anyway: verify --keep-going).")
                break
            print(f"FAIL: step '{name}' exited {rc}; continuing (--keep-going).")
    record_verify("fail" if failures else "pass",
                  failed=", ".join(failures) or None, steps=timings)
    return not failures


def cmd_verify(args):
    print("== verify: definition of done ==")
    cfg = load_config()
    if setup_pending(cfg) is not None:
        print(f"Project setup incomplete — finish first: {SCRIPT} init")
        sys.exit(1)
    steps = [(n, c) for n, c in cfg["commands"].items() if c.get("verify")]
    if not steps:
        print("No verify commands registered — nothing gates completion.")
        print("Register (cheap/fast first), e.g.:")
        print(f'  {SCRIPT} cmd set lint "npm run lint" --verify')
        print(f'  {SCRIPT} cmd set test "npm test" --verify')
        record_verify("fail", failed="(no verify commands registered)")
        sys.exit(1)
    if run_verify_steps(steps, keep_going=getattr(args, "keep_going", False)):
        print(f"== verify OK: all {len(steps)} step(s) green ==")
        tip(f"{SCRIPT} handoff — log work, close feature, commit")
    else:
        print("== verify FAILED. Not done — fix and rerun. ==")
        sys.exit(1)


def tree_state():
    """Content hash of the working tree — tracked AND untracked non-ignored
    files — commit-independent and excluding .agents/ (harness state:
    log/feature updates after a verify run must not mark it stale). Built
    through a throwaway index, not `git stash create`: stash misses untracked
    files, so a verify would stay "fresh" after new code appeared, and go
    stale after committing files that were untracked when it ran. With the
    full snapshot a verify stays fresh exactly while the content it checked
    is unchanged, including across the commit."""
    real_index = git("rev-parse", "--git-path", "index")
    if not real_index:
        return None
    if not os.path.isabs(real_index):
        real_index = os.path.join(ROOT, real_index)
    tmp_dir = tempfile.mkdtemp(prefix="agents-tree-")
    tmp_index = os.path.join(tmp_dir, "index")
    try:
        if os.path.isfile(real_index):
            shutil.copy(real_index, tmp_index)  # keep stat cache: add -A stays fast
        env = dict(os.environ, GIT_INDEX_FILE=tmp_index)
        subprocess.run(["git", "add", "-A", "."],
                       capture_output=True, cwd=ROOT, env=env)
        out = subprocess.run(["git", "write-tree"],
                             capture_output=True, text=True, cwd=ROOT, env=env)
        tree = out.stdout.strip() if out.returncode == 0 else ""
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    if not tree:
        return None
    lines = [l for l in git("ls-tree", tree).splitlines()
             if not l.endswith("\t.agents")]
    return hashlib.sha1("\n".join(lines).encode("utf-8")).hexdigest()


def record_verify(result, failed=None, steps=None):
    scratch = load_scratch()
    scratch["last_verify"] = {
        "result": result,
        "failed_step": failed,
        "date": now_utc(),
        "head": git("rev-parse", "--short", "HEAD") or None,
        "tree": tree_state(),
        "steps": steps or [],
    }
    save_scratch(scratch)


def cmd_check(_args):
    fails, warns = collect_problems()
    for w in warns:
        print(f"WARN: {w}")
    for f in fails:
        print(f"FAIL: {f}")
    if fails:
        print("== check FAILED ==")
        sys.exit(1)
    print("== check OK: harness structure and state valid ==")


def cmd_ci(args):
    cmd_check(args)
    if setup_pending() is not None:
        print("Project setup incomplete — structure gate only; init/verify skipped.")
        return
    cmd_init(args)   # exits non-zero on failure
    cmd_verify(args)


# ---------- log / progress / handoff ----------

def verified_note():
    lv = load_scratch().get("last_verify")
    if not lv:
        return "unverified (no verify run recorded)"
    note = f"{lv.get('result', '?')} ({lv.get('date', '?')} @ {lv.get('head') or 'no-commit'})"
    if lv.get("result") == "fail" and lv.get("failed_step"):
        note += f" — failed at {lv['failed_step']}"
    if lv.get("tree") != tree_state():
        note += " — STALE: files changed since that run, re-verify"
    return note


def cmd_log(args):
    cfg = load_config()

    if args.amend:
        if not cfg["progress"]:
            die("log --amend: no progress entries yet")
        entry = cfg["progress"][-1]
        if args.title and args.title.strip():
            entry["title"] = args.title.strip()
        # only fields explicitly passed change; "" clears (render skips empties)
        for key, val in (("done", args.done), ("issues", args.issues),
                         ("next", args.next), ("blockers", args.blockers),
                         ("verified", args.verified)):
            if val is not None:
                entry[key] = val
        if args.feature:
            if find_feature(cfg["features"], args.feature) is None:
                print(f"WARN: feature '{args.feature}' not in feature list; amending anyway.")
            entry["feature"] = args.feature
        save_config(cfg)
        print("Amended last entry:")
        print(render_entry(entry))
        return

    title = (args.title or "").strip()
    if not title:
        die("log needs a non-empty title")
    if args.done is None:
        die('log needs --done "..." (what shipped)')
    entry = {
        "date": now_utc(),
        "title": title,
        "done": args.done,
        "verified": args.verified or verified_note(),
    }
    if args.issues:
        entry["issues"] = args.issues
    if args.next:
        entry["next"] = args.next
    entry["blockers"] = args.blockers or "none"
    if args.feature:
        if find_feature(cfg["features"], args.feature) is None:
            print(f"WARN: feature '{args.feature}' not in feature list; logging anyway.")
        entry["feature"] = args.feature
    head = git("rev-parse", "--short", "HEAD")
    if head:
        entry["commit"] = head + (" (+ uncommitted changes)" if git("status", "--porcelain") else "")
    cfg["progress"].append(entry)
    save_config(cfg)
    print("Logged:")
    print(render_entry(entry))
    if args.blockers:
        tip(f"if a feature is stuck on this, record it: {SCRIPT} feature block <id> --notes \"{args.blockers}\"")
    tip("commit .agents/agents.json together with the feature")


def cmd_progress(args):
    entries = load_config()["progress"]
    if not entries:
        print(f"No progress entries yet. Record work with: {SCRIPT} log")
        return
    if args.feature:
        entries = [e for e in entries if e.get("feature") == args.feature]
        if not entries:
            print(f"No entries for feature '{args.feature}'.")
            return
    n = len(entries) if args.all else max(1, args.n)
    shown = entries[-n:]
    for e in reversed(shown):  # newest first
        print(render_entry(e))
        print()
    hidden = len(entries) - len(shown)
    if hidden:
        print(f"({hidden} older entr{'y' if hidden == 1 else 'ies'} hidden — use --all or -n)")


def cmd_handoff(_args):
    """End-of-session checklist; state on disk beats memory in context."""
    print("== handoff: end-of-session checklist ==")
    cfg = load_config()
    if setup_pending(cfg) is not None:
        print(f"Project setup incomplete — finish first: {SCRIPT} init")
        sys.exit(1)
    todo = 0

    def item(ok, label, detail):
        nonlocal todo
        todo += 0 if ok else 1
        print(f"  [{'ok' if ok else '..'}] {label}: {detail}")

    lv = load_scratch().get("last_verify")
    if lv is None:
        item(False, "verify", f"no run recorded — run: {SCRIPT} verify")
    elif lv.get("result") != "pass":
        item(False, "verify", f"last run FAILED at {lv.get('failed_step')} — fix and rerun, "
                              "or hand off explicitly as unverified/broken in the log")
    elif lv.get("tree") != tree_state():
        item(False, "verify", f"files changed since last pass — rerun: {SCRIPT} verify")
    else:
        item(True, "verify", f"pass ({lv.get('date')})")

    entries = cfg["progress"]
    today = now_utc()[:10]
    latest = entries[-1] if entries else None
    latest_date = latest.get("date", "") if latest else ""
    if not latest_date.startswith(today):
        item(False, "log", f"no entry for this session — run: {SCRIPT} log \"<title>\" "
                           "--done \"...\" --next \"...\" (caveman style; cover shipped, "
                           "known issues, next step, blockers)")
    elif lv and lv.get("result") == "pass" and (lv.get("date") or "") > latest_date:
        # dates share one fixed-width format, so string compare is chronological;
        # a same-day entry from an earlier session must not pass for this one
        item(False, "log", f"latest entry (\"{latest.get('title')}\") predates the last "
                           f"verify pass — log this session's work: {SCRIPT} log \"<title>\" "
                           "--done \"...\"")
    else:
        item(True, "log", f"entry recorded today: \"{latest.get('title')}\"")

    wip = [f for f in cfg["features"] if f.get("status") == "in_progress"]
    if wip:
        fid = wip[0].get("id")
        item(False, "scope", f"{fid} still in_progress — {SCRIPT} feature done {fid}, "
                             f"or feature block {fid} --notes \"why\" (half-done = block, not done)")
    else:
        item(True, "scope", "no feature left in_progress")

    if git("status", "--porcelain"):
        item(False, "commit", "working tree dirty — commit (.agents/agents.json included); "
                              "half-done work → 'wip:' commit on a feature branch")
    else:
        item(True, "commit", "working tree clean")
    unpushed = git("rev-list", "--count", "@{u}..HEAD")
    if unpushed and unpushed != "0":
        item(False, "push", f"{unpushed} unpushed commit(s) — remote/ephemeral sessions "
                            "lose unpushed work")
    elif unpushed == "0":
        item(True, "push", "in sync with upstream")
    else:
        item(True, "push", "no upstream configured (skip)")

    print("also consider:")
    print(f"  - learned a durable fact → {SCRIPT} docs add <category> \"<rule>\"")
    print(f"  - repeated a multi-step procedure → capture a skill: {SCRIPT} skill new <name>")
    print(f"  - build/test commands changed → {SCRIPT} cmd set ...; CI needs toolchain "
          "changes → tell user (CI human-owned, never edit)")
    if todo:
        print(f"== handoff incomplete: {todo} item(s) open above ==")
        tip(f"close the open items, then rerun: {SCRIPT} handoff")
        sys.exit(1)
    print("== handoff clean: next session resumes from init output alone ==")


# ---------- feature ----------

def cmd_feature(args):
    cfg = load_config()
    feats = cfg["features"]

    if args.action == "list":
        by = {s: [] for s in FEATURE_STATUSES}
        for f in feats:
            by.setdefault(f.get("status", "todo"), []).append(f)
        for status in ("in_progress", "todo", "blocked"):
            if by[status]:
                print(f"{status}:")
                for f in by[status]:
                    print(f"  {fmt_feature(f)}")
        for status in by:  # unknown statuses (hand-edit damage) must stay visible
            if status in FEATURE_STATUSES:
                continue
            print(f"{status} (unknown status — repair: feature start/done/block):")
            for f in by[status]:
                print(f"  {fmt_feature(f)}")
        if args.all:
            if by["done"]:
                print("done:")
                for f in by["done"]:
                    print(f"  {fmt_feature(f)}")
        elif by["done"]:
            print(f"done: {len(by['done'])} (use --all to show)")
        if not feats:
            print(f"No features. Add one: {SCRIPT} feature add \"<title>\"")
        elif by["in_progress"]:
            tip(f"continue {by['in_progress'][0].get('id')}; when done: {SCRIPT} verify")
        elif by["todo"]:
            tip(f"start one: {SCRIPT} feature start <id>")
        return

    if args.action == "add":
        title = (args.title or "").strip()
        if not title:
            die("feature add needs a title: feature add \"<title>\"")
        fid = args.id
        if fid is None:
            nums = [int(m.group(1)) for f in feats
                    for m in [re.match(r"F-(\d+)$", f.get("id", ""))] if m]
            fid = f"F-{(max(nums) + 1 if nums else 1):05d}"
        elif not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]*$", fid):
            die("feature id must be letters/digits/dashes/underscores, e.g. F-00001")
        if find_feature(feats, fid):
            die(f"feature id '{fid}' already exists")
        f = {"id": fid, "title": title, "status": "todo"}
        if args.notes:
            f["notes"] = args.notes
        feats.append(f)
        save_config(cfg)
        print(f"Added: {fmt_feature(f)}")
        tip(f"start it when ready: {SCRIPT} feature start {fid}")
        return

    # remaining actions operate on an existing id
    if not args.title and args.action == "start":
        nxt = next((x for x in feats if x.get("status") == "todo"), None)
        if nxt is None:
            die("feature start: no id given and no todo features left "
                "(see: feature list --all)")
        print(f"no id given — starting first todo: {fmt_feature(nxt)}")
        args.title = nxt["id"]
    if not args.title:
        die(f"feature {args.action} needs an id, e.g.: feature {args.action} F-00001")
    f = find_feature(feats, args.title)
    if f is None:
        die(f"no feature with id '{args.title}' (see: feature list --all)")

    if args.action == "start":
        wip = [x for x in feats if x.get("status") == "in_progress" and x is not f]
        if wip:
            die(f"{wip[0].get('id')} already in_progress (policy: max 1). "
                f"Finish (feature done {wip[0].get('id')}) or block it first.")
        if f.get("status") == "done":
            print(f"WARN: {f['id']} was done — reopening.")
        elif f.get("status") == "blocked":
            if f.get("notes"):
                print(f"WARN: {f['id']} was blocked ({f['notes']}) — confirm resolved; "
                      f"note is stale: {SCRIPT} feature note {f['id']} "
                      "--notes \"...\" or --clear")
            else:
                print(f"WARN: {f['id']} was blocked (no reason recorded) — confirm resolved.")
        f["status"] = "in_progress"
    elif args.action == "done":
        if f.get("status") != "in_progress":
            print(f"WARN: {f['id']} was '{f.get('status')}', not in_progress — marking done anyway.")
        f["status"] = "done"
    elif args.action == "block":
        f["status"] = "blocked"
        if args.notes:
            f["notes"] = args.notes
        elif not f.get("notes"):
            print("WARN: blocked without a reason — pass --notes \"why\".")
    elif args.action == "note":
        if args.clear and args.notes:
            die("feature note: pass --notes or --clear, not both")
        if args.clear:
            f.pop("notes", None)
        elif args.notes:
            f["notes"] = args.notes
        else:
            die("feature note needs --notes \"text\" or --clear")
    elif args.action == "edit":
        new_title = (args.new_title or "").strip()
        if not new_title:
            die('feature edit needs --title "new title"')
        f["title"] = new_title
    save_config(cfg)
    if args.action == "edit":
        print(f"Retitled: {fmt_feature(f)}")
        return
    print(f"{f['id']} -> {f['status']}" + (f" ({f['notes']})" if f.get("notes") else ""))
    if args.action == "start":
        tip(f"implement {f['id']}; stay in scope. When finished: {SCRIPT} verify "
            f"(must be green), then {SCRIPT} handoff")
    elif args.action == "done":
        nxt = next((x for x in feats if x.get("status") == "todo"), None)
        tip(f"{SCRIPT} handoff — log + commit"
            + (f"; next todo after that: {nxt['id']} — {nxt['title']}" if nxt else ""))


# ---------- docs: generated repo map + curated rules ----------

def _tree_file_count(node):
    n = 0
    for child in node.values():
        n += 1 if child is None else _tree_file_count(child)
    return n


def repo_tree_lines():
    """Bounded file tree from git ls-files — always current, never hand-kept."""
    out = git("ls-files")
    if not out:
        return ["(no tracked files — not a git checkout?)"]
    tree = {}
    for path in out.splitlines():
        parts = path.split("/")
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part + "/", {})
        node[parts[-1]] = None

    lines = ["."]

    def render(node, prefix, depth):
        entries = sorted(node.items(),
                         key=lambda kv: (not kv[0].endswith("/"), kv[0]))
        shown = entries[:TREE_MAX_ENTRIES]
        hidden = len(entries) - len(shown)
        for i, (name, child) in enumerate(shown):
            last = i == len(shown) - 1 and hidden == 0
            branch = "└── " if last else "├── "
            cont = "    " if last else "│   "
            if child is None:
                lines.append(prefix + branch + name)
            elif depth + 1 >= TREE_MAX_DEPTH:
                count = _tree_file_count(child)
                lines.append(f"{prefix}{branch}{name} ({count} file{'s' if count != 1 else ''})")
            else:
                lines.append(prefix + branch + name)
                render(child, prefix + cont, depth + 1)
        if hidden:
            lines.append(prefix + f"└── … +{hidden} more")

    render(tree, "", 0)
    return lines


def cmd_docs(args):
    cfg = load_config()
    rules = cfg["rules"]

    if args.action == "add":
        if args.target not in RULE_CATEGORIES:
            die(f"docs add needs a category: {' | '.join(RULE_CATEGORIES)}")
        text = (args.text or "").strip()
        if not text:
            die('docs add needs the rule text: docs add <category> "<rule>"')
        if len(text) > 160:
            print("WARN: long rule — caveman style, split or trim.")
        nums = [int(m.group(1)) for r in rules
                for m in [re.match(r"R-(\d+)$", r.get("id", ""))] if m]
        rule = {
            "id": f"R-{(max(nums) + 1 if nums else 1):03d}",
            "category": args.target,
            "text": text,
            "added": now_utc()[:10],
        }
        rules.append(rule)
        save_config(cfg)
        print(f"Added {rule['id']} [{rule['category']}]: {rule['text']}")
        n = sum(1 for r in rules if r.get("category") == args.target)
        if n > RULES_SOFT_CAP:
            print(f"WARN: {n} {args.target} rules (soft cap {RULES_SOFT_CAP}) — "
                  f"combine overlapping ones, rm stale ones: {SCRIPT} maintenance")
        return

    if args.action == "rm":
        if not args.target:
            die("docs rm needs a rule id, e.g.: docs rm R-003")
        kept = [r for r in rules if r.get("id") != args.target]
        if len(kept) == len(rules):
            die(f"no rule with id '{args.target}' (see: {SCRIPT} docs)")
        cfg["rules"] = kept
        save_config(cfg)
        print(f"Removed {args.target}.")
        return

    # show
    print("== docs: live repo map + curated rules ==")
    print("-- repo map (generated from git ls-files; collapsed dirs show file counts) --")
    for line in repo_tree_lines():
        print(line)
    print("-- rules --")
    for cat in RULE_CATEGORIES:
        in_cat = [r for r in rules if r.get("category") == cat]
        print(f"{cat}:")
        for r in in_cat:
            print(f"  {r.get('id', '?')}: {r.get('text', '?')}")
        if not in_cat:
            print(f"  (none — add: {SCRIPT} docs add {cat} \"<rule>\")")
    tip(f"learned a durable fact → {SCRIPT} docs add <category> \"<rule>\" (terse, one fact per rule)")


# ---------- skill ----------

SKILL_TEMPLATE = """\
---
name: {name}
description: TODO what it does + when to use. Third person, specific trigger words — the only part loaded by default, make it self-explanatory.
---

# {title}

One line: goal of the playbook.

## Steps

1. TODO — numbered, concrete, commands copy-pasteable from repo root. Cheap checks first.

## Rules / gotchas

- TODO — constraints, failure modes, what NOT to do.
"""


SCAFFOLD_TODO_RE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+TODO\b")  # list item starting with TODO


def skill_body_lines(md_path):
    """SKILL.md lines after the frontmatter block (whole file if none)."""
    try:
        with open(md_path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return lines[i + 1:]
    return lines


def skill_lint():
    """Check every skill against the quality bar; True when all clean."""
    rows = []
    if os.path.isdir(SKILLS_DIR):
        for name in sorted(os.listdir(SKILLS_DIR)):
            if os.path.isdir(os.path.join(SKILLS_DIR, name)):
                rows.append((name, os.path.join(SKILLS_DIR, name, "SKILL.md")))
    if not rows:
        print(f"No skills to lint. Scaffold one: {SCRIPT} skill new <name>")
        return True
    clean = True
    for name, md in rows:
        problems = []
        if not os.path.isfile(md):
            problems.append("no SKILL.md")
        else:
            desc = skill_description(md)
            if not desc:
                problems.append("missing 'description:' frontmatter")
            elif desc.startswith("TODO"):
                problems.append("description still the scaffold TODO")
            body = skill_body_lines(md)
            todos = sum(1 for l in body if SCAFFOLD_TODO_RE.search(l))
            if todos:
                problems.append(f"{todos} scaffold TODO item(s) left in body")
            if len(body) > SKILL_BODY_MAX_LINES:
                problems.append(f"body {len(body)} lines (bar: <=~{SKILL_BODY_MAX_LINES}) — "
                                "split details into extra files in the skill dir")
        if problems:
            clean = False
            print(f"  [..] {name}: " + "; ".join(problems))
        else:
            print(f"  [ok] {name}")
    return clean


def cmd_skill(args):
    if args.action == "list":
        rows = list_skills()
        if not rows:
            print(f"No skills yet. Scaffold one: {SCRIPT} skill new <name>")
            return
        for name, desc in rows:
            print(f"  {name}: {desc}")
        return

    if args.action == "lint":
        if skill_lint():
            print("== skill lint OK ==")
        else:
            print("== skill lint: fix items above (quality bar: "
                  ".agents/skills/new-skill/SKILL.md) ==")
            sys.exit(1)
        return

    # new
    if not args.name:
        die("skill new needs a name, e.g.: skill new release-deploy")
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", args.name):
        die("skill name must be kebab-case: lowercase letters/digits/dashes")
    md = os.path.join(SKILLS_DIR, args.name, "SKILL.md")
    if os.path.exists(md):
        die(f".agents/skills/{args.name}/SKILL.md already exists")
    os.makedirs(os.path.dirname(md), exist_ok=True)
    title = args.name.replace("-", " ").capitalize()
    with open(md, "w", encoding="utf-8") as fh:
        fh.write(SKILL_TEMPLATE.format(name=args.name, title=title))
    print(f"Scaffolded .agents/skills/{args.name}/SKILL.md")
    print("Fill every TODO. Quality bar (.agents/skills/new-skill/SKILL.md): body <=~80 "
          "lines, caveman style, executable by a fresh agent with zero context.")
    tip(f"fill it in, then check discovery: {SCRIPT} init")


# ---------- maintenance ----------

def cmd_maintenance(_args):
    """Health sweep: suggest what to update, combine, prune, or re-check."""
    cfg = load_config()
    if setup_pending(cfg) is not None:
        print(f"Project setup incomplete — finish first: {SCRIPT} init")
        sys.exit(1)

    print("== maintenance: harness + knowledge health ==")
    flagged = 0

    def item(ok, label, detail):
        nonlocal flagged
        flagged += 0 if ok else 1
        print(f"  [{'ok' if ok else '..'}] {label}: {detail}")

    print("-- structure --")
    fails, warns = collect_problems()
    for f in fails:
        item(False, "structure", f)
    for w in warns:
        item(False, "structure", w)
    if not fails and not warns:
        item(True, "structure", "no FAILs or WARNs")

    print("-- project identity (setup checks re-run; catches later drift) --")
    ok, detail = _check_project()
    item(ok, "project", detail)

    print("-- rules (project docs) --")
    rules = cfg["rules"]
    for cat in RULE_CATEGORIES:
        in_cat = [r for r in rules if r.get("category") == cat]
        if not in_cat:
            item(False, cat, f"0 rules — record at least one: {SCRIPT} docs add {cat} \"<rule>\"")
        elif len(in_cat) > RULES_SOFT_CAP:
            item(False, cat, f"{len(in_cat)} rules (soft cap {RULES_SOFT_CAP}) — combine "
                             f"overlapping, rm stale: {SCRIPT} docs rm <id>")
        else:
            item(True, cat, f"{len(in_cat)} rule(s)")
    stale = []
    now = datetime.now(timezone.utc)
    for r in rules:
        try:
            added = datetime.strptime(r.get("added", ""), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):  # missing/hand-mangled date: skip, never crash
            continue
        if (now - added).days > RULE_STALE_DAYS:
            stale.append(r.get("id", "?"))
    if stale:
        item(False, "stale rules", f"{len(stale)} older than {RULE_STALE_DAYS} days "
                                   f"({', '.join(stale)}) — spot-check against code; "
                                   "still true → rm + re-add (refreshes date); drifted → fix or rm")
    else:
        item(True, "stale rules", f"none older than {RULE_STALE_DAYS} days")

    print("-- scope --")
    feats = cfg["features"]
    blocked = [f for f in feats if f.get("status") == "blocked"]
    if blocked:
        item(False, "blocked", f"{len(blocked)} feature(s) blocked "
                               f"({', '.join(f.get('id', '?') for f in blocked)}) — "
                               "unblock, re-scope, or close with the user")
    else:
        item(True, "blocked", "no blocked features")
    done = sum(1 for f in feats if f.get("status") == "done")
    print(f"  (info) features: {done} done / {len(feats)} total; "
          f"progress entries: {len(cfg['progress'])} (append-only, display bounded — leave as is)")

    print("-- skills --")
    skills = list_skills()
    if skills:
        item(False, "skills", f"{len(skills)} skill(s) — run {SCRIPT} skill lint, then reread "
                              "each SKILL.md: commands still exist? steps still match the "
                              "code? Fix or delete drifted ones")
    else:
        item(True, "skills", "none to review")

    print("-- commands / CI --")
    ok, detail = _check_commands()
    item(ok, "verify commands", detail)
    timings = (load_scratch().get("last_verify") or {}).get("steps") or []
    slow = [s for s in timings if (s.get("secs") or 0) > VERIFY_SLOW_SECS]
    if slow:
        item(False, "slow verify", ", ".join(f"{s.get('name')} {s.get('secs')}s" for s in slow)
                                   + f" (cap {VERIFY_SLOW_SECS}s) — keep cheap checks first "
                                   f"({SCRIPT} cmd move <name> --before <other>); split or cache slow ones")
    elif timings:
        item(True, "slow verify", f"slowest step {max(s.get('secs') or 0 for s in timings)}s "
                                  f"(cap {VERIFY_SLOW_SECS}s)")
    wf = os.path.join(ROOT, ".github", "workflows", "agents.yml")
    try:
        with open(wf, encoding="utf-8") as fh:
            ci_ok = "AGENTS.sh ci" in fh.read()
    except OSError:
        ci_ok = False
    if ci_ok:
        item(True, "ci", ".github/workflows/agents.yml runs ./AGENTS.sh ci")
    else:
        item(False, "ci", ".github/workflows/agents.yml missing or doesn't run "
                          "./AGENTS.sh ci — report to user; CI human-owned, don't edit")
    item(False, "commands", f"reread {SCRIPT} cmd list — every command still real? "
                            f"definition of done still complete? Then run: {SCRIPT} verify")

    print("-- manual sweep --")
    print("  - AGENTS.md '## Project' and '## Rules' still accurate?")
    print("  - README.md still describes the actual project?")
    print("  - .gitignore still matches the stack?")

    print(f"== maintenance: {flagged} item(s) to act on above ==")
    tip(f"fix small items now; bigger → {SCRIPT} feature add \"maintenance: <what>\"")
    if fails:
        sys.exit(1)


# ---------- cmd / run ----------

def cmd_cmd(args):
    cfg = load_config()
    cmds = cfg["commands"]

    if args.action == "list":
        if not cmds:
            print("No commands registered. Example:")
            print(f'  {SCRIPT} cmd set test "npm test" --verify')
            return
        for name, c in cmds.items():
            flags = "".join(f" [{f}]" for f in ("verify", "init") if c.get(f))
            desc = f"  # {c['desc']}" if c.get("desc") else ""
            print(f"  {name}: {c.get('run', '?')}{flags}{desc}")
        print(f"Run one: {SCRIPT} run <name>. [verify] steps run in listed order.")
        return

    if not args.name:
        die(f"cmd {args.action} needs a name")
    if args.action == "rm":
        if args.name not in cmds:
            die(f"no command named '{args.name}'")
        del cmds[args.name]
        save_config(cfg)
        print(f"Removed '{args.name}'.")
        return

    if args.action == "move":
        if args.name not in cmds:
            die(f"no command named '{args.name}'")
        if bool(args.before) == bool(args.after):
            die("cmd move: pass exactly one of --before <name> / --after <name>")
        anchor = args.before or args.after
        if anchor == args.name:
            die("cmd move: command and anchor are the same")
        if anchor not in cmds:
            die(f"no command named '{anchor}'")
        entry = cmds.pop(args.name)
        items = list(cmds.items())
        idx = list(cmds).index(anchor) + (0 if args.before else 1)
        items.insert(idx, (args.name, entry))
        cfg["commands"] = dict(items)
        save_config(cfg)
        print("New order: " + ", ".join(cfg["commands"]))
        print("[verify] steps run in this order — cheap/fast checks first.")
        return

    # set
    if not re.match(r"^[a-z0-9][a-z0-9_-]*$", args.name):
        die("command name must be lowercase letters/digits/dashes/underscores")
    if not args.command:
        die('cmd set needs the shell command: cmd set <name> "<shell command>"')
    if args.verify and args.no_verify:
        die("cmd set: pass --verify or --no-verify, not both")
    if args.init and args.no_init:
        die("cmd set: pass --init or --no-init, not both")
    entry = {"run": args.command}
    old = cmds.get(args.name, {})
    for key in ("verify", "init", "desc"):  # updates keep flags/desc — clear via --no-*/--desc ""
        if old.get(key):
            entry[key] = old[key]
    if args.verify:
        entry["verify"] = True
    if args.no_verify:
        entry.pop("verify", None)
    if args.init:
        entry["init"] = True
    if args.no_init:
        entry.pop("init", None)
    if args.desc is not None:
        if args.desc:
            entry["desc"] = args.desc
        else:
            entry.pop("desc", None)
    existed = args.name in cmds
    cmds[args.name] = entry  # keeps position if existing, appends if new
    save_config(cfg)
    flags = "".join(f" [{f}]" for f in ("verify", "init") if entry.get(f))
    print(f"{'Updated' if existed else 'Registered'} {args.name}: {args.command}{flags}")
    for _n, exe in missing_command_exes({args.name: entry}):
        print(f"WARN: executable '{exe}' not found from repo root — "
              "command may not run; check path/toolchain.")


def cmd_run(args):
    cfg = load_config()
    c = cfg["commands"].get(args.name)
    if c is None:
        die(f"no command named '{args.name}' (see: cmd list)")
    print(f"-- {args.name}: {c['run']} --")
    sys.exit(subprocess.run(c["run"], shell=True, cwd=ROOT).returncode)


# ---------- argument parsing ----------

def build_parser():
    p = argparse.ArgumentParser(
        prog="./AGENTS.sh",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Agent harness — one CLI guides the whole workflow.",
        epilog=f"""\
which command when:
  session start          {SCRIPT} init                 auto-run by hooks; fresh project → guided setup
  pick work              {SCRIPT} feature start <id>   ONE item at a time (see: feature list)
  finished implementing  {SCRIPT} verify               green = done, red = not done
  ending the session     {SCRIPT} handoff              checklist: log, close feature, commit, push
  learned a durable fact {SCRIPT} docs add <category> "<rule>"
  blocked                {SCRIPT} log "<title>" --done "..." --blockers "..."   then ask user
  asked to do upkeep     {SCRIPT} maintenance

Every command prints a `next:` hint — follow it. State lives in
.agents/agents.json, owned by this script: manage through these
subcommands, never hand-edit. Details per command: {SCRIPT} help <command>.""",
    )
    # dest must not be "command": the `cmd` subparser has a positional named
    # command (the shell command), which would clobber it in the namespace.
    sub = p.add_subparsers(dest="subcommand", required=True, metavar="<command>")

    def add(name, fn, help_, epilog=None):
        sp = sub.add_parser(name, help=help_, description=help_, epilog=epilog,
                            formatter_class=argparse.RawDescriptionHelpFormatter)
        sp.set_defaults(fn=fn)
        return sp

    ini = add("init", cmd_init,
              "session start: health check + state snapshot; on a fresh project it "
              "walks guided setup until complete",
              epilog=f"""\
First runs: init enters SETUP MODE, guides configuration one step at a time.
Steps with an automatic check complete themselves on rerun; manual steps are
recorded with: {SCRIPT} init done <step>. Rerun init after each step; once
setup completes, init reports state and the next action.""")
    ini.add_argument("action", nargs="?", choices=["done", "undo"],
                     help="'done' — record a manual setup step as finished; "
                          "'undo' — unrecord a step marked by mistake")
    ini.add_argument("step", nargs="?", help="setup step name (for 'done'/'undo')")
    ini.add_argument("--force", action="store_true",
                     help="record the step even if its automatic check fails")

    vf = add("verify", cmd_verify,
             "run the registered definition of done (commands flagged --verify, in order); "
             "records the result so `log` can report it")
    vf.add_argument("--keep-going", action="store_true",
                    help="don't stop at the first red step; report all failures")

    add("handoff", cmd_handoff,
        "end-of-session checklist with live status: verify, log, feature state, commit, push")

    lg = add("log", cmd_log,
             "record a progress entry; date, commit, and verify status are stamped automatically",
             epilog=f"""\
examples:
  {SCRIPT} log "auth feature" --done "JWT login in src/auth/" \\
      --issues "refresh tokens untested" --next "wire logout" --feature F-00002
  {SCRIPT} log --amend --done "JWT login + logout in src/auth/"
--amend fixes the LAST entry: only the fields you pass change ("" clears one).
Terse caveman style (AGENTS.md '## Style'). Storage/history handled for you;
nothing to compact or archive.""")
    lg.add_argument("title", nargs="?", help="short entry title (optional with --amend)")
    lg.add_argument("--done", help="what shipped (paths, behavior); required unless --amend")
    lg.add_argument("--amend", action="store_true",
                    help="update the last entry instead of appending (typo/forgot a field)")
    lg.add_argument("--issues", help="broken/known issues: facts, exact errors")
    lg.add_argument("--next", help="single most useful next step")
    lg.add_argument("--blockers", help="what stops progress (default: none)")
    lg.add_argument("--feature", help="related feature id, e.g. F-00001")
    lg.add_argument("--verified", help="override the auto-detected verify status")

    pr = add("progress", cmd_progress, "show recent progress entries, newest first")
    pr.add_argument("-n", type=int, default=PROGRESS_DEFAULT_SHOWN,
                    help="how many entries (default %(default)s)")
    pr.add_argument("--all", action="store_true", help="show every entry")
    pr.add_argument("--feature", help="only entries logged for this feature id, e.g. F-00001")

    ft = add("feature", cmd_feature,
             "manage scope; one feature in_progress at a time (enforced)",
             epilog=f"""\
examples:
  {SCRIPT} feature list
  {SCRIPT} feature add "rate limiting" --notes "per-IP, 100 req/min"
  {SCRIPT} feature start                    no id = first todo
  {SCRIPT} feature start F-00003
  {SCRIPT} feature done F-00003
  {SCRIPT} feature block F-00004 --notes "waiting on API key"
  {SCRIPT} feature note F-00004 --clear
  {SCRIPT} feature edit F-00003 --title "rate limiting (per-IP)"
""")
    ft.add_argument("action", choices=["list", "add", "start", "done", "block", "note", "edit"])
    ft.add_argument("title", nargs="?", help="title (for add) or feature id (for the rest)")
    ft.add_argument("--id", help="explicit id for add (default: next F-NNNNN)")
    ft.add_argument("--title", dest="new_title", help="edit: replacement title")
    ft.add_argument("--notes", help="notes text (add/block/note)")
    ft.add_argument("--clear", action="store_true", help="note: remove the feature's note")
    ft.add_argument("--all", action="store_true", help="list: include done features")

    dc = add("docs", cmd_docs,
             "live project docs: generated repo map + curated rules "
             "(architecture / conventions / testing)",
             epilog=f"""\
examples:
  {SCRIPT} docs                                     repo map + all rules
  {SCRIPT} docs add conventions "commits: imperative, <=72 chars"
  {SCRIPT} docs rm R-003
Repo map generated from git ls-files — never drifts. Rules are the curated
part: one terse fact each, added when learned, pruned when stale
(`{SCRIPT} maintenance` flags categories past {RULES_SOFT_CAP}).""")
    dc.add_argument("action", nargs="?", choices=["show", "add", "rm"], default="show")
    dc.add_argument("target", nargs="?",
                    help="category (for add: %s) or rule id (for rm)"
                         % " | ".join(RULE_CATEGORIES))
    dc.add_argument("text", nargs="?", help="rule text (for add)")

    sk = add("skill", cmd_skill,
             "scaffold a new skill playbook (.agents/skills/<name>/SKILL.md) or list skills",
             epilog=f"""\
examples:
  {SCRIPT} skill new release-deploy     scaffold, then fill every TODO
  {SCRIPT} skill list
  {SCRIPT} skill lint                   quality bar: TODOs gone, body <=~{SKILL_BODY_MAX_LINES} lines
Quality bar + format details: .agents/skills/new-skill/SKILL.md.""")
    sk.add_argument("action", choices=["new", "list", "lint"])
    sk.add_argument("name", nargs="?", help="kebab-case skill name (for new)")

    add("maintenance", cmd_maintenance,
        "health sweep for an upkeep session: flags rules to combine/prune, blocked "
        "features, skills and commands to re-check, docs to refresh")

    cm = add("cmd", cmd_cmd,
             "register project commands (build/test/lint/dev) — data, not script edits",
             epilog=f"""\
examples:
  {SCRIPT} cmd set lint "npm run lint" --verify     part of the definition of done
  {SCRIPT} cmd set deps "npm ci" --init             session-start smoke check
  {SCRIPT} cmd set dev "npm run dev"                on-demand helper (use: run dev)
  {SCRIPT} cmd move lint --before test              reorder without rm + re-add
  {SCRIPT} cmd rm lint
verify steps run in listed order — register cheap/fast checks first
(reorder later: cmd move <name> --before/--after <other>).
re-running set on an existing name keeps its flags/desc and its position in
the order; clear flags with --no-verify / --no-init, the desc with --desc "".""")
    cm.add_argument("action", choices=["set", "rm", "list", "move"])
    cm.add_argument("name", nargs="?", help="command name, e.g. test")
    cm.add_argument("command", nargs="?", help="shell command, e.g. \"npm test\"")
    cm.add_argument("--before", help="move: place before this command")
    cm.add_argument("--after", help="move: place after this command")
    cm.add_argument("--verify", action="store_true",
                    help="part of the definition of done (run by `verify`)")
    cm.add_argument("--no-verify", action="store_true",
                    help="remove the verify flag from an existing command")
    cm.add_argument("--init", action="store_true",
                    help="session-start smoke check (run by `init`)")
    cm.add_argument("--no-init", action="store_true",
                    help="remove the init flag from an existing command")
    cm.add_argument("--desc", help="one-line description (\"\" clears it)")

    rn = add("run", cmd_run, "run a registered command by name")
    rn.add_argument("name")

    add("check", cmd_check, "structure/state validation only (no setup gate)")
    add("ci", cmd_ci, "what CI runs: check, then init + verify once setup complete")

    hp = add("help", lambda a: p.parse_args(([a.topic] if a.topic else []) + ["--help"]),
             "show usage; `help <command>` for one command's details")
    hp.add_argument("topic", nargs="?", help="command name, e.g. feature")
    return p


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ["help"]  # bare invocation: show the guide, not a usage error
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except BrokenPipeError:
        # output piped into e.g. `head` that exited early — not an error
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(141)  # 128 + SIGPIPE


if __name__ == "__main__":
    main()
