#!/bin/bash
set -euo pipefail

# macOS 用户级 LaunchAgent 的 SSH 隧道管理脚本。
# 远端地址、账号和端口必须由调用方提供，不写入仓库或生成的固定模板。

LABEL="com.codex-proxy.vps-tunnel"
HOME_DIR="${HOME:-}"
if [[ -z "$HOME_DIR" || "$HOME_DIR" != /* ]]; then
    echo "HOME must be an absolute path." >&2
    exit 1
fi

PLIST="$HOME_DIR/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$HOME_DIR/.codex-proxy/logs"
STDOUT_LOG="$LOG_DIR/vps-tunnel.out.log"
STDERR_LOG="$LOG_DIR/vps-tunnel.err.log"
DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$LABEL"
TEMP_PLIST=""

cleanup_temp_plist() {
    if [[ -n "$TEMP_PLIST" ]]; then
        rm -f "$TEMP_PLIST"
    fi
}

trap cleanup_temp_plist EXIT HUP INT TERM

usage() {
    cat <<'EOF'
Usage:
  vps-tunnel-launchd.sh install [options]
  vps-tunnel-launchd.sh status
  vps-tunnel-launchd.sh health
  vps-tunnel-launchd.sh start
  vps-tunnel-launchd.sh restart
  vps-tunnel-launchd.sh stop
  vps-tunnel-launchd.sh uninstall

The install command accepts these options, or the matching environment variables:
  --host HOST                 CPR_VPS_HOST
  --user USER                 CPR_VPS_USER
  --local-port PORT           CPR_TUNNEL_LOCAL_PORT
  --remote-host HOST          CPR_TUNNEL_REMOTE_HOST
  --remote-port PORT          CPR_TUNNEL_REMOTE_PORT

Example:
  CPR_VPS_HOST=vps.example.com \
  CPR_VPS_USER=root \
  CPR_TUNNEL_LOCAL_PORT=18080 \
  CPR_TUNNEL_REMOTE_HOST=127.0.0.1 \
  CPR_TUNNEL_REMOTE_PORT=8080 \
  vps-tunnel-launchd.sh install
EOF
}

die() {
    echo "Error: $*" >&2
    exit 1
}

require_platform() {
    [[ "$(uname -s)" == "Darwin" ]] || die "this command is supported on macOS only."
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

require_install_commands() {
    require_command ssh
    require_command launchctl
    require_command lsof
    require_command plutil
    require_command curl
    [[ -x /usr/libexec/PlistBuddy ]] || die "required command not found: /usr/libexec/PlistBuddy"
}

require_lifecycle_commands() {
    require_command launchctl
}

require_status_commands() {
    require_command launchctl
    require_command lsof
    [[ -x /usr/libexec/PlistBuddy ]] || die "required command not found: /usr/libexec/PlistBuddy"
}

require_health_commands() {
    require_command curl
    [[ -x /usr/libexec/PlistBuddy ]] || die "required command not found: /usr/libexec/PlistBuddy"
}

validate_token() {
    local value="$1"
    local label="$2"

    case "$value" in
        ''|*[![:print:]]*|*[[:space:]]*)
            die "$label must be a non-empty value without whitespace or control characters."
            ;;
    esac
}

validate_port() {
    local value="$1"
    local label="$2"
    local numeric_value

    case "$value" in
        ''|*[!0-9]*)
            die "$label must be an integer in range 1-65535."
            ;;
    esac

    numeric_value=$((10#$value))
    if ((numeric_value < 1 || numeric_value > 65535)); then
        die "$label must be an integer in range 1-65535."
    fi

    printf '%s\n' "$numeric_value"
}

parse_install_options() {
    vps_host="${CPR_VPS_HOST:-}"
    vps_user="${CPR_VPS_USER:-}"
    local_port="${CPR_TUNNEL_LOCAL_PORT:-}"
    remote_host="${CPR_TUNNEL_REMOTE_HOST:-}"
    remote_port="${CPR_TUNNEL_REMOTE_PORT:-}"

    while (($# > 0)); do
        case "$1" in
            --host)
                (($# >= 2)) || die "--host requires a value."
                vps_host="$2"
                shift 2
                ;;
            --host=*)
                vps_host="${1#*=}"
                shift
                ;;
            --user)
                (($# >= 2)) || die "--user requires a value."
                vps_user="$2"
                shift 2
                ;;
            --user=*)
                vps_user="${1#*=}"
                shift
                ;;
            --local-port)
                (($# >= 2)) || die "--local-port requires a value."
                local_port="$2"
                shift 2
                ;;
            --local-port=*)
                local_port="${1#*=}"
                shift
                ;;
            --remote-host)
                (($# >= 2)) || die "--remote-host requires a value."
                remote_host="$2"
                shift 2
                ;;
            --remote-host=*)
                remote_host="${1#*=}"
                shift
                ;;
            --remote-port)
                (($# >= 2)) || die "--remote-port requires a value."
                remote_port="$2"
                shift 2
                ;;
            --remote-port=*)
                remote_port="${1#*=}"
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                die "unknown install option: $1"
                ;;
        esac
    done

    validate_token "$vps_host" "VPS host"
    validate_token "$vps_user" "VPS user"
    validate_token "$remote_host" "remote forward host"
    local_port="$(validate_port "$local_port" "local forward port")"
    remote_port="$(validate_port "$remote_port" "remote forward port")"

    ssh_target="$vps_user@$vps_host"
    forward_spec="127.0.0.1:$local_port:$remote_host:$remote_port"
    health_url="http://127.0.0.1:$local_port/healthz"
}

xml_escape() {
    local value="$1"
    value=${value//&/\&amp;}
    value=${value//</\&lt;}
    value=${value//>/\&gt;}
    printf '%s' "$value"
}

plist_string() {
    printf '      <string>'
    xml_escape "$1"
    printf '</string>\n'
}

ssh_options() {
    printf '%s\n' \
        '-o' 'BatchMode=yes' \
        '-o' 'ConnectTimeout=10' \
        '-o' 'StrictHostKeyChecking=yes' \
        '-o' 'ExitOnForwardFailure=yes' \
        '-o' 'ServerAliveInterval=30' \
        '-o' 'ServerAliveCountMax=3'
}

resolve_install_commands() {
    SSH_BIN="$(command -v ssh)"
    LAUNCHCTL_BIN="$(command -v launchctl)"
    LSOF_BIN="$(command -v lsof)"
    PLUTIL_BIN="$(command -v plutil)"
    CURL_BIN="$(command -v curl)"
    PLIST_BUDDY="/usr/libexec/PlistBuddy"
}

resolve_status_commands() {
    LAUNCHCTL_BIN="$(command -v launchctl)"
    LSOF_BIN="$(command -v lsof)"
    PLIST_BUDDY="/usr/libexec/PlistBuddy"
}

resolve_health_commands() {
    CURL_BIN="$(command -v curl)"
    PLIST_BUDDY="/usr/libexec/PlistBuddy"
}

resolve_lifecycle_commands() {
    LAUNCHCTL_BIN="$(command -v launchctl)"
}

service_output() {
    "$LAUNCHCTL_BIN" print "$SERVICE" 2>/dev/null
}

service_pid() {
    local output

    output="$(service_output || true)"
    printf '%s\n' "$output" | sed -n 's/^[[:space:]]*pid = //p' | head -n 1
}

listener_pids() {
    { "$LSOF_BIN" -nP -iTCP:"$1" -sTCP:LISTEN -Fp 2>/dev/null || true; } \
        | sed -n 's/^p//p' \
        | sort -u
}

all_listeners_owned_by() {
    local expected_pid="$1"
    local pids
    local pid

    [[ -n "$expected_pid" ]] || return 1
    pids="$(listener_pids "$2")"
    [[ -n "$pids" ]] || return 1

    while IFS= read -r pid; do
        [[ -z "$pid" ]] && continue
        [[ "$pid" == "$expected_pid" ]] || return 1
    done <<EOF
$pids
EOF
}

ensure_local_port_available() {
    local port="$1"
    local tunnel_pid="$2"
    local pids

    pids="$(listener_pids "$port")"
    if [[ -n "$pids" ]] && ! all_listeners_owned_by "$tunnel_pid" "$port"; then
        die "local port $port is already in use by another process (PID(s): $(printf '%s' "$pids" | tr '\n' ' '))."
    fi
}

write_plist() {
    local argument
    local ssh_arguments=()

    while IFS= read -r argument; do
        ssh_arguments+=("$argument")
    done < <(ssh_options)

    ssh_arguments+=("-N" "-L" "$forward_spec" "$ssh_target")

    TEMP_PLIST="$(mktemp "$PLIST.tmp.XXXXXX")" || die "could not create a temporary plist."
    {
        printf '%s\n' \
            '<?xml version="1.0" encoding="UTF-8"?>' \
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">' \
            '<plist version="1.0">' \
            '  <dict>' \
            '    <key>Label</key>'
        plist_string "$LABEL"
        printf '%s\n' \
            '    <key>RunAtLoad</key>' \
            '    <true/>' \
            '    <key>KeepAlive</key>' \
            '    <true/>' \
            '    <key>ThrottleInterval</key>' \
            '    <integer>10</integer>' \
            '    <key>WorkingDirectory</key>'
        plist_string "$HOME_DIR"
        printf '%s\n' \
            '    <key>ProgramArguments</key>' \
            '    <array>'
        plist_string "$SSH_BIN"
        for argument in "${ssh_arguments[@]}"; do
            plist_string "$argument"
        done
        printf '%s\n' \
            '    </array>' \
            '    <key>StandardOutPath</key>'
        plist_string "$STDOUT_LOG"
        printf '%s\n' \
            '    <key>StandardErrorPath</key>'
        plist_string "$STDERR_LOG"
        printf '%s\n' \
            '  </dict>' \
            '</plist>'
    } > "$TEMP_PLIST"

    "$PLUTIL_BIN" -lint "$TEMP_PLIST" >/dev/null \
        || die "generated plist failed validation: $TEMP_PLIST"
    mv -f "$TEMP_PLIST" "$PLIST"
    TEMP_PLIST=""
}

preflight_ssh() {
    local options=()
    local option

    while IFS= read -r option; do
        options+=("$option")
    done < <(ssh_options)

    printf '[vps-tunnel] checking SSH authentication for %s\n' "$ssh_target" >&2
    "$SSH_BIN" "${options[@]}" -T "$ssh_target" true \
        || die "SSH preflight failed for $ssh_target; check the key, agent, and known_hosts entry."
}

bootout_service() {
    if "$LAUNCHCTL_BIN" print "$SERVICE" >/dev/null 2>&1; then
        "$LAUNCHCTL_BIN" bootout "$SERVICE" >/dev/null 2>&1 \
            || "$LAUNCHCTL_BIN" bootout "$DOMAIN" "$PLIST" >/dev/null 2>&1 \
            || die "could not unload $SERVICE"
    fi
}

bootstrap_service() {
    "$LAUNCHCTL_BIN" bootstrap "$DOMAIN" "$PLIST"
}

kickstart_service() {
    "$LAUNCHCTL_BIN" kickstart -k "$SERVICE"
}

listener_summary() {
    local port="$1"
    local summary

    summary="$({ "$LSOF_BIN" -nP -iTCP:"$port" -sTCP:LISTEN -F cpn 2>/dev/null || true; } | awk '
        /^p/ { pid=substr($0, 2) }
        /^c/ { cmd=substr($0, 2) }
        /^n/ { name=substr($0, 2); print cmd " pid=" pid " " name }
    ' | paste -sd '; ' -)"
    if [[ -n "$summary" ]]; then
        printf '%s\n' "$summary"
    else
        printf 'not listening\n'
    fi
}

plist_arguments() {
    "$PLIST_BUDDY" -c 'Print :ProgramArguments' "$PLIST" 2>/dev/null | awk '
        NF && $1 != "Array" && $1 != "}" {
            value=$0
            sub(/^[[:space:]]+/, "", value)
            values[++count]=value
        }
        END {
            for (i = 1; i <= count; i++) {
                print values[i]
            }
        }
    '
}

configured_forward_specs() {
    local arguments

    [[ -f "$PLIST" ]] || return 0
    arguments="$(plist_arguments || true)"
    [[ -n "$arguments" ]] || return 0
    printf '%s\n' "$arguments" | awk '
        { values[++count]=$0 }
        END {
            for (i = 1; i < count; i++) {
                if (values[i] == "-L") {
                    print values[i + 1]
                }
            }
        }
    '
}

configured_target() {
    local arguments

    [[ -f "$PLIST" ]] || return 0
    arguments="$(plist_arguments || true)"
    [[ -n "$arguments" ]] || return 0
    printf '%s\n' "$arguments" | tail -n 1
}

local_port_from_forward_spec() {
    local spec="$1"
    local first_field
    local remainder

    first_field="${spec%%:*}"
    if [[ "$first_field" == "127.0.0.1" ]]; then
        remainder="${spec#*:}"
        printf '%s\n' "${remainder%%:*}"
    else
        printf '%s\n' "$first_field"
    fi
}

configured_local_port() {
    local spec
    local port

    spec="$(configured_forward_specs | head -n 1)"
    [[ -n "$spec" ]] || die "could not read a local forward from $PLIST; run install first."
    port="$(local_port_from_forward_spec "$spec")"
    validate_port "$port" "configured local forward port"
}

show_status() {
    local output
    local state
    local pid
    local last_exit_code
    local target
    local spec
    local configured_ports=()

    resolve_status_commands

    if output="$(service_output 2>&1)"; then
        state="$(printf '%s\n' "$output" | sed -n 's/^[[:space:]]*state = //p' | head -n 1)"
        pid="$(printf '%s\n' "$output" | sed -n 's/^[[:space:]]*pid = //p' | head -n 1)"
        last_exit_code="$(printf '%s\n' "$output" | sed -n 's/^[[:space:]]*last exit code = //p' | head -n 1)"
        printf 'label: %s\n' "$LABEL"
        printf 'loaded: yes\n'
        printf 'state: %s\n' "${state:-unknown}"
        [[ -n "$pid" ]] && printf 'pid: %s\n' "$pid"
        printf 'last exit code: %s\n' "${last_exit_code:-unknown}"
    else
        printf 'label: %s\n' "$LABEL"
        printf 'loaded: no\n'
    fi

    printf 'plist: %s\n' "$PLIST"
    target="$(configured_target || true)"
    [[ -n "$target" ]] && printf 'target: %s\n' "$target"
    printf 'stdout: %s\n' "$STDOUT_LOG"
    printf 'stderr: %s\n' "$STDERR_LOG"

    while IFS= read -r spec; do
        [[ -n "$spec" ]] || continue
        configured_ports+=("$(local_port_from_forward_spec "$spec")")
    done < <(configured_forward_specs)

    if ((${#configured_ports[@]} == 0)); then
        printf 'configured forwards: none\n'
    else
        for spec in "${configured_ports[@]}"; do
            printf 'port %s: %s\n' "$spec" "$(listener_summary "$spec")"
        done
    fi
}

health_check() {
    local deadline
    local status_code

    deadline=$(( $(date +%s) + 30 ))
    while (( $(date +%s) < deadline )); do
        status_code="$("$CURL_BIN" --silent --show-error --fail --max-time 5 \
            --output /dev/null --write-out '%{http_code}' "$health_url" 2>/dev/null || true)"
        case "$status_code" in
            2??|3??)
                printf '[vps-tunnel] health check: HTTP %s (%s)\n' "$status_code" "$health_url"
                return 0
                ;;
        esac
        sleep 1
    done

    return 1
}

health_service() {
    local local_port
    local health_url

    local_port="$(configured_local_port)"
    health_url="http://127.0.0.1:$local_port/healthz"
    "$CURL_BIN" --fail --silent --show-error --output /dev/null \
        --write-out 'health: HTTP %{http_code}\n' --max-time 10 "$health_url"
}

install_service() {
    local tunnel_pid

    require_platform
    require_install_commands
    resolve_install_commands
    parse_install_options "$@"

    mkdir -p "$LOG_DIR" "$HOME_DIR/Library/LaunchAgents"
    tunnel_pid="$(service_pid)"
    ensure_local_port_available "$local_port" "$tunnel_pid"
    preflight_ssh
    write_plist

    bootout_service
    bootstrap_service
    kickstart_service

    if ! health_check; then
        echo "Error: tunnel started but $health_url did not return a successful status within 30 seconds." >&2
        echo "Inspect: $STDERR_LOG" >&2
        exit 1
    fi

    printf 'OK: installed %s\n' "$PLIST"
    printf 'OK: forwarding 127.0.0.1:%s -> %s:%s\n' "$local_port" "$remote_host" "$remote_port"
    printf 'OK: logs %s and %s\n' "$STDOUT_LOG" "$STDERR_LOG"
}

start_service() {
    require_platform
    require_lifecycle_commands
    resolve_lifecycle_commands
    [[ -f "$PLIST" ]] || die "plist not found: $PLIST; run install first."

    if ! "$LAUNCHCTL_BIN" print "$SERVICE" >/dev/null 2>&1; then
        bootstrap_service
    fi
    "$LAUNCHCTL_BIN" kickstart "$SERVICE"
    printf 'OK: started %s\n' "$SERVICE"
}

restart_service() {
    require_platform
    require_lifecycle_commands
    resolve_lifecycle_commands
    [[ -f "$PLIST" ]] || die "plist not found: $PLIST; run install first."

    bootout_service
    bootstrap_service
    kickstart_service
    printf 'OK: restarted %s\n' "$SERVICE"
}

stop_service() {
    require_platform
    require_lifecycle_commands
    resolve_lifecycle_commands
    bootout_service
    printf 'OK: stopped %s\n' "$SERVICE"
}

uninstall_service() {
    require_platform
    require_lifecycle_commands
    resolve_lifecycle_commands
    bootout_service
    rm -f "$PLIST"
    printf 'OK: removed %s\n' "$PLIST"
    printf 'Note: kept logs under %s\n' "$LOG_DIR"
}

command_name="${1:-}"
if [[ -z "$command_name" ]]; then
    usage >&2
    exit 2
fi
shift

case "$command_name" in
    install)
        install_service "$@"
        ;;
    status)
        (($# == 0)) || die "status does not accept options."
        require_platform
        require_status_commands
        show_status
        ;;
    health)
        (($# == 0)) || die "health does not accept options."
        require_platform
        require_health_commands
        resolve_health_commands
        health_service
        ;;
    start)
        (($# == 0)) || die "start does not accept options."
        start_service
        ;;
    restart)
        (($# == 0)) || die "restart does not accept options."
        restart_service
        ;;
    stop)
        (($# == 0)) || die "stop does not accept options."
        stop_service
        ;;
    uninstall)
        (($# == 0)) || die "uninstall does not accept options."
        uninstall_service
        ;;
    -h|--help|help)
        (($# == 0)) || die "help does not accept options."
        usage
        ;;
    *)
        die "unknown command: $command_name"
        ;;
esac
