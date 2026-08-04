#!/usr/bin/env bash
# Linux/macOS supervisor for the AgentSociety Hub worker (mirror of run-agent-worker.ps1).
# Keeps `node agent-host/dist/src/cli.js worker` running: restarts on crash,
# logs stdout/stderr per attempt under worker-logs/.
#
# Usage:
#   bash scripts/run-agent-worker.sh            # foreground supervisor
#   setsid bash scripts/run-agent-worker.sh &    # detached resident worker
#
# Env overrides:
#   AGENT_WORKER_SESSION_MODE  per_task (default for the platform) or continuous
#   AGENT_WORKER_CONCURRENCY   number of parallel worker slots (default 1)
#   RESTART_DELAY_SECONDS      pause between restarts (default 5)

set -u

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RESTART_DELAY_SECONDS="${RESTART_DELAY_SECONDS:-5}"

# A pi TUI session exports these; a worker must never inherit them.
export AGENT_WORKER_SUPERVISED="1"
unset PI_PROVIDER PI_MODEL AGENT_HUB_RUNTIME_DISABLED

# 持续形 worker:连续 session,按 scope 跨任务复用、跨重启恢复。
export AGENT_WORKER_SESSION_MODE="${AGENT_WORKER_SESSION_MODE:-continuous}"

NODE="$(command -v node || true)"
if [ -z "${NODE}" ]; then
  echo "node not found in PATH" >&2
  exit 1
fi
ENTRYPOINT="${PROJECT_ROOT}/agent-host/dist/src/cli.js"
if [ ! -f "${ENTRYPOINT}" ]; then
  echo "Agent Host entrypoint not found: ${ENTRYPOINT}" >&2
  exit 1
fi

LOG_DIR="${PROJECT_ROOT}/worker-logs"
SUPERVISOR_LOG="${LOG_DIR}/worker-supervisor.log"
SUPERVISOR_PID="${LOG_DIR}/worker-supervisor.pid"
mkdir -p "${LOG_DIR}"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "${SUPERVISOR_LOG}"
}

log "agent worker supervisor started project=${PROJECT_ROOT} mode=${AGENT_WORKER_SESSION_MODE} concurrency=${AGENT_WORKER_CONCURRENCY:-1}"
echo "$$" > "${SUPERVISOR_PID}"

while true; do
  STAMP="$(date '+%Y%m%d-%H%M%S')"
  STDOUT_LOG="${LOG_DIR}/worker-${STAMP}.out.log"
  STDERR_LOG="${LOG_DIR}/worker-${STAMP}.err.log"
  log "starting worker stdout=${STDOUT_LOG} stderr=${STDERR_LOG}"

  # shellcheck disable=SC2086
  "${NODE}" "${ENTRYPOINT}" worker >> "${STDOUT_LOG}" 2>> "${STDERR_LOG}"
  CODE=$?
  log "worker exited code=${CODE}"

  sleep "${RESTART_DELAY_SECONDS}"
done
