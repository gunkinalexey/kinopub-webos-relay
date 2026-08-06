#!/usr/bin/env bash
# Runs on the Stop hook: once per assistant turn, not once per edit.
#
# Why not PostToolUse: a single turn here routinely makes a dozen edits, and a
# rebuild takes tens of seconds. Per-edit would mean a dozen commits and minutes
# of rebuilds for one logical change.
#
# Why not rebuid_containers.bat: it ends with `pause`, which blocks forever with
# no console, and it uses `build --no-cache`, which reinstalls ffmpeg (~480 MB)
# every time. That script stays for manual full rebuilds.
#
# The frontend is a read-only bind mount (./frontend -> nginx html), so its
# changes are live immediately and only backend/ changes need an image rebuild.
set -uo pipefail

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}" || exit 0
command -v git >/dev/null 2>&1 || exit 0
git rev-parse --git-dir >/dev/null 2>&1 || exit 0

# Nothing staged, unstaged, or untracked -> nothing to do.
if git diff --quiet && git diff --cached --quiet && [ -z "$(git status --porcelain)" ]; then
  exit 0
fi

backend_changed="$(git status --porcelain -- backend | head -1)"

# stderr is silenced: on Windows every file trips a CRLF advisory that would
# otherwise bury the hook's own output.
git add -A 2>/dev/null || exit 0
if git diff --cached --quiet; then
  exit 0
fi

files="$(git diff --cached --name-only | wc -l | tr -d ' ')"
summary="$(git diff --cached --name-only | sed 's#/.*##' | sort -u | paste -sd, -)"
git commit -q -m "checkpoint: ${summary} ($(date '+%Y-%m-%d %H:%M'))" 2>/dev/null || exit 0
short="$(git rev-parse --short HEAD)"
message="commit ${short} — ${files} file(s)"

if [ -n "$backend_changed" ]; then
  if command -v docker >/dev/null 2>&1 &&
     docker compose up -d --build backend >/dev/null 2>&1; then
    message="${message}; backend rebuilt"
  else
    message="${message}; backend rebuild FAILED — run rebuid_containers.bat"
  fi
else
  message="${message}; frontend is a bind mount, no rebuild needed"
fi

printf '{"systemMessage":"%s"}\n' "$message"
