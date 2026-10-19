#!/usr/bin/env bash
# =============================================================================
# install-service.sh — Prepare and manage the Oraculo user LaunchAgent
#
# Installs the locked Python environment and renders absolute runtime paths.
# Installation does not start the service, authenticate, or enable providers.
#
# Prerequisites:
#   macOS, uv, git; a checked-out repository with uv.lock and Python 3.12 available.
#   export ORACULO_CONFIG="/absolute/path/config.toml"  # optional
#
# Usage:
#   ./scripts/install-service.sh                 # installation dry-run
#   ./scripts/install-service.sh --install       # prepare without starting
#   ./scripts/install-service.sh --start         # doctor, then load LaunchAgent
#   ./scripts/install-service.sh --stop --dry-run # preview stopping
#   ./scripts/install-service.sh --help
# =============================================================================
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPOSITORY="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
CONFIG="${ORACULO_CONFIG:-${HOME}/.config/oraculo/config.toml}"
PLIST="${HOME}/Library/LaunchAgents/local.oraculo.plist"
LOG_DIR="${HOME}/Library/Logs/Oraculo"
PYTHON="${REPOSITORY}/.venv/bin/python"
SERVICE="gui/$(id -u)/local.oraculo"
DOMAIN="gui/$(id -u)"
MODE="install"
DRY_RUN=1
EXPLICIT_MODE=0
FORCE_DRY_RUN=0
TEMP_PLIST=""

cleanup() {
    if [[ -n "${TEMP_PLIST}" && -f "${TEMP_PLIST}" ]]; then
        rm -f -- "${TEMP_PLIST}"
    fi
}
trap cleanup EXIT

show_help() {
    cat <<'HELP'
Usage: install-service.sh [ACTION] [--dry-run]

  --install     Sync locked dependencies, initialize missing config/vault/state,
                render LaunchAgent; does not load or start it.
  --start       Require config and run doctor; load/start installed LaunchAgent.
  --stop        Unload LaunchAgent, including its KeepAlive restart policy.
  --status      Print launchd state (exit 1 when not loaded).
  --uninstall   Unload and remove LaunchAgent only; preserve vault, state, config,
                environment, credentials and logs.
  --dry-run     Describe action without modifying files or invoking launchctl.
  --help, -h    Show this help.

No action defaults to --install --dry-run. Explicit actions execute unless
--dry-run is present. Set ORACULO_CONFIG to an absolute config path if needed.
Stop before upgrading/reinstalling or running OAuth login. After installation:
  .venv/bin/python -m oraculo --config /absolute/config.toml auth login
Configure only verified existing subscription models, then use --start.
Changing config path requires reinstalling the LaunchAgent while stopped.
HELP
}

fail() {
    printf 'Error: %s\n' "$1" >&2
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --install|--start|--stop|--status|--uninstall)
            [[ "${EXPLICIT_MODE}" -eq 0 ]] || fail 'Choose exactly one action.'
            MODE="${1#--}"
            EXPLICIT_MODE=1
            DRY_RUN=0
            ;;
        --dry-run) FORCE_DRY_RUN=1 ;;
        --help|-h) show_help; exit 0 ;;
        *) fail "Unknown argument: $1" ;;
    esac
    shift
done
[[ "${FORCE_DRY_RUN}" -eq 0 ]] || DRY_RUN=1
[[ "${CONFIG}" == /* ]] || fail 'ORACULO_CONFIG must be an absolute path.'

if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf 'Dry-run: %s\nRepository: %s\nConfig: %s\nLaunchAgent: %s\nRuntime: %s\n' \
        "${MODE}" "${REPOSITORY}" "${CONFIG}" "${PLIST}" "${PYTHON}"
    case "${MODE}" in
        install) printf 'Would sync uv.lock with Python 3.12, initialize missing data, and render plist. No service start.\n' ;;
        start) printf 'Would require installed config/plist, run doctor, and bootstrap service if not loaded.\n' ;;
        stop) printf 'Would unload service and disable KeepAlive until the next start.\n' ;;
        status) printf 'Would inspect launchctl service %s.\n' "${SERVICE}" ;;
        uninstall) printf 'Would unload service and remove only its plist; preserve all data.\n' ;;
    esac
    exit 0
fi

[[ "$(uname -s)" == Darwin ]] || fail 'LaunchAgents require macOS.'
[[ -x /bin/launchctl ]] || fail 'launchctl unavailable.'

is_loaded() {
    /bin/launchctl print "${SERVICE}" >/dev/null 2>&1
}

stop_service() {
    if is_loaded; then
        /bin/launchctl bootout "${SERVICE}"
        printf 'Service unloaded. State and child-task receipts preserved.\n'
    else
        printf 'Service already unloaded.\n'
    fi
}

case "${MODE}" in
    install)
        if is_loaded; then
            fail 'Stop the service before reinstalling; its environment must not change while running.'
        fi
        UV_BIN="$(command -v uv)" || fail 'Install uv before preparing the service.'
        [[ -f "${REPOSITORY}/uv.lock" ]] || fail 'Missing uv.lock.'
        "${UV_BIN}" sync --locked --python 3.12 --project "${REPOSITORY}"
        "${PYTHON}" -m oraculo --config "${CONFIG}" init
        mkdir -p "${HOME}/Library/LaunchAgents" "${LOG_DIR}"
        TEMP_PLIST="$(mktemp "${HOME}/Library/LaunchAgents/.local.oraculo.XXXXXX")"
        "${PYTHON}" - "${REPOSITORY}" "${CONFIG}" "${LOG_DIR}" "${TEMP_PLIST}" "${HOME}" <<'PY'
import os
import plistlib
import sys
from pathlib import Path

repository, config, logs, destination, user_home = map(Path, sys.argv[1:])
with (repository / "deploy/local.oraculo.plist").open("rb") as stream:
    template = plistlib.load(stream)
values = {
    "__PYTHON__": str(repository / ".venv/bin/python"),
    "__CONFIG__": str(config),
    "__REPOSITORY__": str(repository),
    "__STDOUT__": str(logs / "stdout.log"),
    "__STDERR__": str(logs / "stderr.log"),
    "__PATH__": ":".join(map(str, [repository / ".venv/bin", user_home / ".local/bin",
                                    Path("/opt/homebrew/bin"), Path("/usr/local/bin"),
                                    Path("/usr/bin"), Path("/bin"), Path("/usr/sbin"), Path("/sbin")])),
}
def replace(value):
    if isinstance(value, dict):
        return {key: replace(item) for key, item in value.items()}
    if isinstance(value, list):
        return [replace(item) for item in value]
    return values.get(value, value) if isinstance(value, str) else value

with destination.open("wb") as stream:
    plistlib.dump(replace(template), stream)
    stream.flush()
    os.fsync(stream.fileno())
PY
        /usr/bin/plutil -lint "${TEMP_PLIST}"
        chmod 600 "${TEMP_PLIST}"
        mv -f -- "${TEMP_PLIST}" "${PLIST}"
        TEMP_PLIST=""
        printf 'Installed without starting: %s\nConfigure plans and log in while stopped; then use --start.\n' "${PLIST}"
        ;;
    start)
        [[ -f "${CONFIG}" ]] || fail 'Missing config; run --install first.'
        [[ -f "${PLIST}" && -x "${PYTHON}" ]] || fail 'Missing installation; run --install first.'
        "${PYTHON}" - "${PLIST}" "${CONFIG}" "${PYTHON}" <<'PY'
import plistlib
import sys
with open(sys.argv[1], "rb") as stream:
    args = plistlib.load(stream)["ProgramArguments"]
if args != [sys.argv[3], "-m", "oraculo", "--config", sys.argv[2], "serve"]:
    raise SystemExit("Installed paths differ; stop and reinstall first.")
PY
        "${PYTHON}" -m oraculo --config "${CONFIG}" doctor
        if is_loaded; then
            printf 'Service already loaded.\n'
        else
            /bin/launchctl enable "${SERVICE}"
            /bin/launchctl bootstrap "${DOMAIN}" "${PLIST}"
        fi
        /bin/launchctl print "${SERVICE}"
        ;;
    stop) stop_service ;;
    status) /bin/launchctl print "${SERVICE}" ;;
    uninstall)
        stop_service
        if [[ -f "${PLIST}" || -L "${PLIST}" ]]; then
            rm -f -- "${PLIST}"
        fi
        printf 'LaunchAgent removed; vault, state, config, credentials and logs retained.\n'
        ;;
esac
