#!/usr/bin/env bash
set -Eeuo pipefail

# Operator helpers for dario's headless admin API. Runbook: docs/dario.md
#
#   dario.sh status                    /health, plus each Claude seat's grant age
#   dario.sh login [alias]             start a re-grant, then paste the code the
#                                      approval page shows
#   dario.sh complete <code> [alias]   finish a re-grant started without a terminal

source "$(dirname "${0}")/lib/common.sh"

ROOT_DIR="$(git -C "$(dirname "${0}")" rev-parse --show-toplevel)"
DARIO_URL="${DARIO_URL:-https://dario.turner.haus}"
export SOPS_AGE_KEY_FILE="${SOPS_AGE_KEY_FILE:-${ROOT_DIR}/age.key}"

# The /admin/* bearer, from the same SOPS secret the pod reads
function admin_token() {
    sops --decrypt --extract '["stringData"]["DARIO_ADMIN_TOKEN"]' \
        "${ROOT_DIR}/kubernetes/apps/default/dario/app/secret.sops.yaml"
}

# POST a JSON body to the admin API; prints the response, fails unless 200
function admin_post() {
    local out code
    out=$(curl -sS -m 30 -X POST -H "authorization: Bearer ${TOKEN}" \
        -H 'content-type: application/json' -d "${2}" -w '\n%{http_code}' "${DARIO_URL}${1}")
    code="${out##*$'\n'}"
    printf '%s\n' "${out%$'\n'*}"
    [[ "${code}" == 200 ]]
}

function status() {
    local health accounts
    health=$(curl -sS -m 15 -o /dev/null -w '%{http_code}' "${DARIO_URL}/health" || true)
    accounts=$(curl -sS -m 15 --fail-with-body -H "authorization: Bearer ${TOKEN}" "${DARIO_URL}/admin/accounts") \
        || log error "GET /admin/accounts failed" response="${accounts}"

    printf '\n/health: %s\n\n' "${health}"
    jq -r '
        ["SEAT", "STATUS", "GRANT", "AGE", "WALL (~28d after grant)", "ACCESS TOKEN", "AUTH FAILS"],
        (.accounts[] | [
            .alias,
            (if .status == "rejected" and .reset_at then "rate-limited until \(.reset_at / 1000 | strflocaltime("%H:%M"))" else .status end),
            .grant_level,
            (if .grant_age_days then "\(.grant_age_days)d" else "?" end),
            (if .refresh_wall_at then (.refresh_wall_at / 1000 | strflocaltime("%a %b %d %H:%M")) else "-" end),
            (if .expires_in_ms > 0 then "\(.expires_in_ms / 3600000 | floor)h left" else "expired" end),
            (.consecutive_auth_failures | tostring)
        ]) | @tsv' <<<"${accounts}" | column -t -s $'\t'
    echo

    if jq -e '.accounts | any(.grant_level != "ok")' <<<"${accounts}" >/dev/null; then
        log warn "Re-grant needed: task dario:login [SEAT=<seat>]"
    elif [[ "${health}" != 200 ]] && jq -e '.accounts | length > 0 and all(.status == "rejected")' <<<"${accounts}" >/dev/null; then
        log warn "Rate-limited: dario resumes by itself at the reset time (docs/dario.md, \"When it's rate-limited\")"
    elif [[ "${health}" != 200 ]]; then
        log warn "dario isn't serving; see docs/dario.md"
    fi
}

function login() {
    local alias="${1}" started pasted
    started=$(admin_post /admin/login/start "$(jq -nc --arg alias "${alias}" '{alias: $alias}')") \
        || log error "Could not start a login" response="${started}"

    printf '\nOpen this in a browser signed in to the Claude account for seat "%s", and approve:\n\n  %s\n\n' \
        "${alias}" "$(jq -r .authorize_url <<<"${started}")"
    if [[ ! -t 0 ]]; then
        log info "No terminal to paste into; within 10 minutes run" cmd="task dario:complete SEAT=${alias} CODE='<code#state>'"
        return
    fi
    read -rp "Paste the code the page shows (all of it, code#state): " pasted
    complete "${pasted}" "${alias}"
}

function complete() {
    local pasted="${1//[[:space:]]/}" alias="${2}" result
    [[ -n "${pasted}" ]] || log error "No code given"
    result=$(admin_post /admin/login/complete \
        "$(jq -nc --arg alias "${alias}" --arg code "${pasted}" '{alias: $alias, code: $code}')") \
        || log error "Not re-granted; a pending login is single-use, so start over with 'task dario:login'" response="${result}"
    log info "Seat re-granted; the proxy serves it without a restart" alias="${alias}"
    status
}

function main() {
    TOKEN=$(admin_token)
    case "${1:-}" in
        status) status ;;
        login) login "${2:-main}" ;;
        complete) complete "${2:-}" "${3:-main}" ;;
        *) log error "Usage: dario.sh status | login [alias] | complete <code> [alias]" ;;
    esac
}

main "$@"
