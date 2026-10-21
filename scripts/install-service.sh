#!/bin/sh
# Compatibility entrypoint; all lifecycle rules live in service_control.py.
set -eu
mode=status
if [ "$#" -gt 0 ]; then
    case "$1" in
        --install)
            echo 'Prepare a verified artifact with install-cli.sh, then runtime activate RELEASE. Installation never starts a service.' >&2
            exit 2 ;;
        --start|--stop|--restart|--status|--uninstall) mode=${1#--}; shift ;;
        --help|-h)
            echo 'Usage: install-service.sh --start|--stop|--restart|--status|--uninstall [service options]'
            echo 'ORACULO_CLI selects the installed launcher; ORACULO_CONFIG selects config.'
            exit 0 ;;
    esac
fi
exec "${ORACULO_CLI:-$HOME/.local/bin/oraculo}" --config "${ORACULO_CONFIG:-$HOME/.config/oraculo/config.toml}" service "$mode" "$@"
