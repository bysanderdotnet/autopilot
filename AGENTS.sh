#!/usr/bin/env sh
# Don't read or change this file for normal project work. Use this wrapper's
# help command instead: ./AGENTS.sh help

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AGENTS_PY="$SCRIPT_DIR/.agents/agents.py"

is_python3() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1
}

PYTHON=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && is_python3 "$candidate"; then
    PYTHON=$candidate
    break
  fi
done

if [ -z "$PYTHON" ]; then
  echo "AGENTS.sh: Python 3.8+ is required but no suitable python3/python command was found" >&2
  exit 127
fi

exec "$PYTHON" "$AGENTS_PY" "$@"
