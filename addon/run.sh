#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -euo pipefail

bashio::log.info "Starting VoltWise"

# Validate the user configuration before any optimisation or write can occur.
if ! python3 -m voltwise --config /data/options.json --dry-run --validate-only; then
    bashio::log.fatal "Configuration is invalid. VoltWise will not start."
    exit 1
fi

export VOLTWISE_PLAN_PATH=/data/voltwise/plan.json
export VOLTWISE_WEB_PORT=8099

# --apply is ONLY passed when the operator explicitly enabled actuation in the
# add-on options. The CLI still requires the flag, so a config-only mistake
# cannot write to Home Assistant.
if bashio::config.true 'actuation.enabled'; then
    bashio::log.warning "Actuation is ENABLED: VoltWise will write power targets to Home Assistant."
    exec python3 -m voltwise.service --config /data/options.json \
        --output "${VOLTWISE_PLAN_PATH}" --web-port "${VOLTWISE_WEB_PORT}" --apply --confirm
else
    bashio::log.info "Actuation is disabled (monitoring/dry-run mode)."
    exec python3 -m voltwise.service --config /data/options.json \
        --output "${VOLTWISE_PLAN_PATH}" --web-port "${VOLTWISE_WEB_PORT}"
fi
