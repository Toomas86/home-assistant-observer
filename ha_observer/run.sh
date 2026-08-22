#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -Eeuo pipefail

# Read app options from the file mounted by Supervisor. Recent Bashio releases
# fetch bashio::config values from the Supervisor API even when CONFIG_PATH is
# set, so use jq directly to preserve this app's least-privilege permissions.
options_file="/data/options.json"
if [[ ! -r "${options_file}" ]]; then
    bashio::log.fatal "The Home Assistant app options file is not readable."
    exit 1
fi

tunnel_id="$(jq --raw-output '.tunnel_id // empty' "${options_file}")"
runtime_key="$(jq --raw-output '.openai_runtime_api_key // empty' "${options_file}")"
retention_days="$(jq --raw-output '.retention_days // 30' "${options_file}")"
allow_sensitive="$(jq --raw-output '.allow_sensitive_entities // false' "${options_file}")"
config_management="$(jq --raw-output '.config_management_enabled // false' "${options_file}")"
log_level="$(jq --raw-output '.log_level // "info"' "${options_file}")"

if [[ -z "${tunnel_id}" || "${tunnel_id}" == "null" ]]; then
    bashio::log.fatal "Configure tunnel_id before starting the app."
    exit 1
fi
if [[ -z "${runtime_key}" || "${runtime_key}" == "null" ]]; then
    bashio::log.fatal "Configure openai_runtime_api_key before starting the app."
    exit 1
fi

umask 077
runtime_key_file="/data/openai_runtime_api_key"
printf '%s' "${runtime_key}" > "${runtime_key_file}"
unset runtime_key

export OBSERVER_DB_PATH="/data/observer.db"
export OBSERVER_RETENTION_DAYS="${retention_days}"
export OBSERVER_ALLOW_SENSITIVE_ENTITIES="${allow_sensitive}"
export OBSERVER_LOG_LEVEL="${log_level}"
export CONFIG_MANAGEMENT_ENABLED="${config_management}"
export HA_CONFIG_ROOT="/homeassistant"
export CONFIG_MANAGER_DATA_PATH="/data/config_manager"

export CONTROL_PLANE_TUNNEL_ID="${tunnel_id}"
export MCP_SERVER_URL="http://127.0.0.1:3000/mcp"
export HEALTH_LISTEN_ADDR="127.0.0.1:8080"
export LOG_LEVEL="${log_level}"
export LOG_FORMAT="json"

bashio::log.info "Starting the Home Assistant MCP observer and opt-in YAML manager."
/opt/venv/bin/uvicorn observer.server:app \
    --host 127.0.0.1 \
    --port 3000 \
    --log-level "${log_level}" \
    --no-access-log &
server_pid=$!

cleanup() {
    kill "${server_pid:-}" "${tunnel_pid:-}" 2>/dev/null || true
    wait "${server_pid:-}" "${tunnel_pid:-}" 2>/dev/null || true
}
trap 'cleanup; exit 0' TERM INT

ready=false
for _ in $(seq 1 30); do
    if /opt/venv/bin/python -c \
        'import urllib.request; urllib.request.urlopen("http://127.0.0.1:3000/healthz", timeout=1).read()' \
        >/dev/null 2>&1; then
        ready=true
        break
    fi
    if ! kill -0 "${server_pid}" 2>/dev/null; then
        bashio::log.fatal "The MCP observer stopped during startup."
        wait "${server_pid}" || true
        exit 1
    fi
    sleep 1
done

if [[ "${ready}" != "true" ]]; then
    bashio::log.fatal "The MCP observer did not become ready within 30 seconds."
    cleanup
    exit 1
fi

bashio::log.info "Starting the outbound-only OpenAI Secure MCP Tunnel."
/usr/bin/tunnel-client run \
    --control-plane.api-key="file:${runtime_key_file}" &
tunnel_pid=$!

set +e
wait -n "${server_pid}" "${tunnel_pid}"
exit_code=$?
set -e
bashio::log.error "A required process stopped with exit code ${exit_code}; stopping the app."
cleanup
exit "${exit_code}"
