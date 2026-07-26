#!/usr/bin/env bash
# check_repro.sh — static reproducibility checks for a fresh clone.
#
# Run this immediately after `git clone` and BEFORE standing up the stack.
# It catches the clean-clone failures that a populated dev machine hides:
# missing committed data, an out-of-sync lockfile, env vars referenced by code
# but absent from .env.example, compose bind-mount targets that don't exist,
# and setup files the README forgets to mention.
#
# Exit code 0 = all checks passed. Non-zero = at least one hard failure.
# Usage:  bash scripts/check_repro.sh   (run from repo root)

set -uo pipefail

fail=0
warn=0
pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fail=$((fail+1)); }
soft() { printf '  \033[33mWARN\033[0m  %s\n' "$1"; warn=$((warn+1)); }
head() { printf '\n\033[1m%s\033[0m\n' "$1"; }

if [ ! -f docker-compose.yaml ] || [ ! -f pyproject.toml ]; then
  echo "Run this from the repo root (docker-compose.yaml not found here)."
  exit 2
fi

head "1. Clone-critical files present and non-trivial"
# data corpus: must be real committed JSON, not an LFS pointer or empty file.
if [ -f data/pubmed.jsonl ]; then
  lines=$(wc -l < data/pubmed.jsonl)
  bytes=$(wc -c < data/pubmed.jsonl)
  if [ "$bytes" -lt 100000 ]; then
    bad "data/pubmed.jsonl is only $bytes bytes — likely an LFS pointer or stub, not the corpus"
  elif head -1 data/pubmed.jsonl | grep -q '^version https://git-lfs'; then
    bad "data/pubmed.jsonl is a Git LFS pointer, not the actual data"
  else
    pass "data/pubmed.jsonl present ($lines lines, $bytes bytes)"
  fi
else
  bad "data/pubmed.jsonl MISSING — reproducibility caps at 1 point (data not accessible)"
fi

for f in uv.lock pyproject.toml .env.example docker-compose.yaml; do
  if [ -s "$f" ]; then pass "$f present"; else bad "$f MISSING or empty"; fi
done

head "2. Lockfile in sync with pyproject (versions pinned)"
if command -v uv >/dev/null 2>&1; then
  if uv lock --check >/dev/null 2>&1; then
    pass "uv.lock is in sync with pyproject.toml"
  else
    bad "uv.lock is OUT OF SYNC with pyproject.toml — 'uv sync --frozen' will fail on clone"
  fi
else
  soft "uv not installed; skipped lock-sync check (install uv to verify)"
fi

head "3. Every env var referenced by code/compose is in .env.example"
# Vars the app actually needs at runtime. Deliberately excludes vars that only
# appear in commented-out compose services or that have code-level defaults.
declared=$(grep -oE '^[A-Z_][A-Z0-9_]*' .env.example | sort -u)
# Referenced in python via os.environ / os.getenv:
py_refs=$(grep -rhoE 'os\.(environ\.get|getenv)\(\s*["'"'"'][A-Z_][A-Z0-9_]*["'"'"']|os\.environ\[\s*["'"'"'][A-Z_][A-Z0-9_]*["'"'"']' --include=*.py \
  --exclude-dir=.venv --exclude-dir=venv --exclude-dir=.git \
  --exclude-dir=__pycache__ --exclude-dir=.mypy_cache --exclude-dir=.pytest_cache \
  --exclude-dir=node_modules --exclude-dir=site-packages \
  app agents retrieval monitoring ingestion evaluation 2>/dev/null | grep -oE '[A-Z_][A-Z0-9_]{2,}' | sort -u)
# Referenced in compose as ${VAR}, but skip commented lines and skip ${VAR:-default} (has fallback):
compose_refs=$(grep -vE '^\s*#' docker-compose.yaml | grep -oE '\$\{[A-Z_][A-Z0-9_]*(:-[^}]*)?\}' | grep -vE ':-' | grep -oE '[A-Z_][A-Z0-9_]*' | sort -u)
# Known-optional vars (code default or commented service):
optional_re='^(MODE|NCBI_API_KEY)$'
missing=0
for v in $py_refs $compose_refs; do
  echo "$v" | grep -qE "$optional_re" && continue
  if echo "$declared" | grep -qx "$v"; then :; else
    bad "$v is referenced but NOT in .env.example"
    missing=1
  fi
done
[ "$missing" -eq 0 ] && pass "all required env vars are declared in .env.example"

head "4. Compose bind-mount source files exist on disk"
# A missing bind-mount source silently becomes an empty dir and breaks the container.
mounts=$(grep -oE '\./[A-Za-z0-9_./-]+:' docker-compose.yaml | sed 's/:$//' | sort -u)
for m in $mounts; do
  # only check file mounts (those with an extension), not directory mounts
  case "$m" in
    *.*) if [ -e "$m" ]; then pass "mount source $m exists"; else bad "compose mounts $m but it does not exist"; fi ;;
    *)   [ -e "$m" ] && pass "mount source $m exists" || soft "mount source $m not found (dir mount)" ;;
  esac
done

head "5. SQL / module files the setup sequence uses all exist"
for f in ingestion/schema.sql ingestion/feedback_schema.sql ingestion/indexes.sql \
         ingestion/pipeline.py ingestion/embed.py \
         app/main.py docker/init-db.sql docker/Dockerfile.app; do
  [ -f "$f" ] && pass "$f exists" || bad "$f MISSING (setup sequence references it)"
done

head "6. README documents every psql setup file"
# feedback_schema.sql is easy to leave out of the README while the app depends on it.
for sqlf in schema.sql feedback_schema.sql indexes.sql; do
  if grep -q "$sqlf" README.md; then
    pass "README mentions $sqlf"
  else
    bad "README never mentions $sqlf — a reviewer following it will skip that step"
  fi
done

# The app writes to query_log/feedback; those tables must come from a documented file.
if grep -rqE 'INSERT INTO (query_log|feedback)' --include=*.py .; then
  if grep -q feedback_schema.sql README.md; then
    pass "app writes query_log/feedback and README documents feedback_schema.sql"
  else
    bad "app writes query_log/feedback but README omits feedback_schema.sql — dashboard will break on clean clone"
  fi
fi

head "Summary"
printf "  %s failure(s), %s warning(s)\n" "$fail" "$warn"
if [ "$fail" -eq 0 ]; then
  printf "  \033[32mStatic checks passed. Proceed to the live clean-clone run.\033[0m\n"
  exit 0
else
  printf "  \033[31mFix the failures above before the live run.\033[0m\n"
  exit 1
fi
