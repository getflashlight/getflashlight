#!/usr/bin/env bash
# Fail if git-tracked operator config is present.
#
# `.gitignore` already excludes these paths for normal workflows. This check
# catches force-adds (`git add -f`) so CI/pre-commit reject the leak.
set -euo pipefail

FORBIDDEN=(
  .env
  config/connections.yml
)

tracked="$(git ls-files -- "${FORBIDDEN[@]}")"
if [[ -n "${tracked}" ]]; then
  echo "Operator config must not be committed." >&2
  echo "These paths are gitignored; commit the *.example.* templates instead:" >&2
  echo "${tracked}" >&2
  exit 1
fi
