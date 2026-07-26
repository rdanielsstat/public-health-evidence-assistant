#!/usr/bin/env bash
# check_repro.sh — static pre-flight checks for a fresh checkout.
#
# Run this immediately after cloning and BEFORE bringing up the stack. It
# validates that the repository is self-contained: that the corpus snapshot and
# lockfile are committed, that every environment variable the application reads
# is declared in .env.example, that compose bind-mount sources exist, and that
# the setup steps in the README are internally consistent. These are the checks
# that pass on a populated development machine but fail on a fresh checkout.
#
# Exit code 0 = all checks passed. Non-zero = at least one hard failure.
# Usage:  bash scripts/check_repro.sh   (run from the repository root)

set -uo pipefail

fail=0
warn=0
pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fail=$((fail+1)); }
soft() { printf '  \033[33mWARN\033[0m  %s\n' "$1"; warn=$((warn+1)); }
head() { printf '\n\033[1m%s\033[0m\n' "$1"; }

if [ ! -f docker-compose.yaml ] || [ ! -f pyproject.toml ]; then
  echo "Run this from the repository root (docker-compose.yaml not found here)."
  exit 2
fi

head "1. Required files present and non-trivial"
# The corpus snapshot must be real committed JSON, not an LFS pointer or stub.
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
  bad "data/pubmed.jsonl MISSING — the corpus snapshot is not committed"
fi

for f in uv.lock pyproject.toml .env.example docker-compose.yaml; do
  if [ -s "$f" ]; then pass "$f present"; else bad "$f MISSING or empty"; fi
done

head "2. Lockfile in sync with pyproject (dependency versions pinned)"
if command -v uv >/dev/null 2>&1; then
  if uv lock --check >/dev/null 2>&1; then
    pass "uv.lock is in sync with pyproject.toml"
  else
    bad "uv.lock is OUT OF SYNC with pyproject.toml — 'uv sync --frozen' will fail on a fresh checkout"
  fi
else
  soft "uv not installed; skipped lock-sync check (install uv to verify)"
fi

head "3. Every environment variable the application reads is in .env.example"
# Vars the application actually needs at runtime. Deliberately excludes vars
# that only appear in commented-out compose services or have code-level defaults.
declared=$(grep -oE '^[A-Z_][A-Z0-9_]*' .env.example | sort -u)
# Referenced in application code via os.environ / os.getenv. Scoped to the
# project's own source directories so installed dependencies are not scanned.
py_refs=$(grep -rhoE 'os\.(environ\.get|getenv)\(\s*["'"'"'][A-Z_][A-Z0-9_]*["'"'"']|os\.environ\[\s*["'"'"'][A-Z_][A-Z0-9_]*["'"'"']' --include=*.py \
  --exclude-dir=.venv --exclude-dir=venv --exclude-dir=.git \
  --exclude-dir=__pycache__ --exclude-dir=.mypy_cache --exclude-dir=.pytest_cache \
  --exclude-dir=node_modules --exclude-dir=site-packages \
  app agents retrieval monitoring ingestion evaluation 2>/dev/null | grep -oE '[A-Z_][A-Z0-9_]{2,}' | sort -u)
# Referenced in compose as ${VAR}; skip commented lines and ${VAR:-default} (has a fallback).
compose_refs=$(grep -vE '^\s*#' docker-compose.yaml | grep -oE '\$\{[A-Z_][A-Z0-9_]*(:-[^}]*)?\}' | grep -vE ':-' | grep -oE '[A-Z_][A-Z0-9_]*' | sort -u)
# Known-optional vars (code default, or only in a commented-out service):
optional_re='^(MODE|NCBI_API_KEY)$'
missing=0
for v in $py_refs $compose_refs; do
  echo "$v" | grep -qE "$optional_re" && continue
  if echo "$declared" | grep -qx "$v"; then :; else
    bad "$v is referenced but NOT in .env.example"
    missing=1
  fi
done
[ "$missing" -eq 0 ] && pass "all required environment variables are declared in .env.example"

head "4. Compose bind-mount source files exist on disk"
# A missing bind-mount source is silently created as an empty directory and breaks the container.
mounts=$(grep -oE '\./[A-Za-z0-9_./-]+:' docker-compose.yaml | sed 's/:$//' | sort -u)
for m in $mounts; do
  case "$m" in
    *.*) if [ -e "$m" ]; then pass "mount source $m exists"; else bad "compose mounts $m but it does not exist"; fi ;;
    *)   [ -e "$m" ] && pass "mount source $m exists" || soft "mount source $m not found (dir mount)" ;;
  esac
done

head "5. Compose image tags resolve in the registry"
# Pinned image tags can be removed upstream, which breaks 'docker compose up' on
# a fresh pull. This checks that each pinned tag still exists. Requires network.
if command -v docker >/dev/null 2>&1; then
  imgs=$(grep -oE '^\s*image:\s*\S+' docker-compose.yaml | awk '{print $2}' | sort -u)
  for img in $imgs; do
    case "$img" in
      *'${'*) continue ;;  # skip images built from a variable
    esac
    if docker manifest inspect "$img" >/dev/null 2>&1; then
      pass "image tag resolves: $img"
    else
      bad "image tag does NOT resolve in the registry: $img (was it removed upstream?)"
    fi
  done
else
  soft "docker not available; skipped image-tag resolution check"
fi

head "6. SQL and module files referenced by the setup steps exist"
for f in ingestion/schema.sql ingestion/feedback_schema.sql ingestion/indexes.sql \
         ingestion/pipeline.py ingestion/embed.py \
         app/main.py docker/init-db.sql docker/Dockerfile.app; do
  [ -f "$f" ] && pass "$f exists" || bad "$f MISSING (referenced by the setup steps)"
done

head "7. README documents every schema file the setup applies"
for sqlf in schema.sql feedback_schema.sql indexes.sql; do
  if grep -q "$sqlf" README.md; then
    pass "README documents $sqlf"
  else
    bad "README does not mention $sqlf — the setup steps are incomplete"
  fi
done
# The application writes to query_log/feedback; those tables must be created by a documented file.
if grep -rqE 'INSERT INTO (query_log|feedback)' --include=*.py \
     --exclude-dir=.venv --exclude-dir=venv --exclude-dir=site-packages \
     app agents retrieval monitoring ingestion evaluation 2>/dev/null; then
  if grep -q feedback_schema.sql README.md; then
    pass "application writes query_log/feedback and README documents feedback_schema.sql"
  else
    bad "application writes query_log/feedback but README omits feedback_schema.sql — the dashboard will fail on a fresh checkout"
  fi
fi

head "Summary"
printf "  %s failure(s), %s warning(s)\n" "$fail" "$warn"
if [ "$fail" -eq 0 ]; then
  printf "  \033[32mStatic checks passed.\033[0m\n"
  exit 0
else
  printf "  \033[31mFix the failures above before bringing up the stack.\033[0m\n"
  exit 1
fi
