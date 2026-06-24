#!/usr/bin/env bash
# Register the self-hosted runner, run it, and deregister cleanly on stop.
set -euo pipefail

: "${GITHUB_URL:?set GITHUB_URL, e.g. https://github.com/ychaparala/auralake}"

RUNNER_NAME="${RUNNER_NAME:-auralake-docker-$(hostname)}"
RUNNER_LABELS="${RUNNER_LABELS:-self-hosted,docker,linux,x64}"
RUNNER_WORKDIR="${RUNNER_WORKDIR:-_work}"

cd /home/runner

# A registration token is short-lived. Prefer a PAT (repo admin scope) so the
# container mints a fresh token on every (re)start; fall back to a token you paste.
get_token() {
  if [ -n "${RUNNER_TOKEN:-}" ]; then
    echo "${RUNNER_TOKEN}"
    return
  fi
  : "${GITHUB_PAT:?set RUNNER_TOKEN or GITHUB_PAT}"
  local repo="${GITHUB_URL#https://github.com/}"
  curl -fsSL -X POST \
    -H "Authorization: Bearer ${GITHUB_PAT}" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/${repo}/actions/runners/registration-token" \
    | jq -r .token
}

TOKEN="$(get_token)"

cleanup() {
  echo "Deregistering runner ${RUNNER_NAME}..."
  ./config.sh remove --token "$(get_token)" || true
  exit 0
}
trap cleanup INT TERM

EPHEMERAL_FLAG=()
[ "${RUNNER_EPHEMERAL:-false}" = "true" ] && EPHEMERAL_FLAG=(--ephemeral)

./config.sh \
  --unattended \
  --replace \
  --url "${GITHUB_URL}" \
  --token "${TOKEN}" \
  --name "${RUNNER_NAME}" \
  --labels "${RUNNER_LABELS}" \
  --work "${RUNNER_WORKDIR}" \
  "${EPHEMERAL_FLAG[@]}"

# run.sh in the background so the trap can fire on SIGTERM (docker stop).
./run.sh &
wait $!
