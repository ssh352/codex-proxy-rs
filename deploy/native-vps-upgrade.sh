#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# 现有 VPS 原生 systemd 安装的升级脚本。
#
# 该脚本沿用项目内置更新器的 Release 归档与校验和合同，但保留当前原生
# 服务边界。PostgreSQL、Redis、config.yaml 和运行数据不会被复制或重置。

readonly APP_REPOSITORY="${CPR_UPDATE_REPOSITORY:-zyycn/codex-proxy-rs}"
readonly SERVICE_NAME_DEFAULT="${CPR_VPS_SERVICE:-codex-proxy-rs.service}"
readonly BINARY_PATH_DEFAULT="${CPR_VPS_BINARY:-/opt/codex-proxy-rs/codex-proxy-rs}"
readonly WEB_DIST_PATH_DEFAULT="${CPR_VPS_WEB_DIST:-/opt/codex-proxy-rs/web/dist}"
readonly DATABASE_NAME_DEFAULT="${CPR_VPS_DATABASE:-codex_proxy}"
readonly LISTEN_PORT_DEFAULT="${CPR_VPS_PORT:-8080}"
readonly BACKUP_ROOT_DEFAULT="${CPR_VPS_BACKUP_ROOT:-/var/lib/codex-proxy-rs/upgrade-backups}"
readonly PREVIOUS_MIGRATION_DEFAULT="${CPR_PREVIOUS_MIGRATION:-16}"
readonly TARGET_MIGRATION_DEFAULT="${CPR_TARGET_MIGRATION:-17}"
readonly APP_BINARY_NAME="codex-proxy-rs"

mode=""
host_arg=""
user_arg=""
version_arg=""

service_name="$SERVICE_NAME_DEFAULT"
binary_path="$BINARY_PATH_DEFAULT"
web_dist_path="$WEB_DIST_PATH_DEFAULT"
database_name="$DATABASE_NAME_DEFAULT"
listen_port="$LISTEN_PORT_DEFAULT"
backup_root="$BACKUP_ROOT_DEFAULT"
previous_migration="$PREVIOUS_MIGRATION_DEFAULT"
target_migration="$TARGET_MIGRATION_DEFAULT"

work_dir=""
remote_stage=""
release_checksum=""
staged_binary_hash=""

log() {
    printf '[native-vps-upgrade] %s\n' "$*" >&2
}

die() {
    printf '[native-vps-upgrade] error: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat >&2 <<'EOF'
Usage:
  native-vps-upgrade.sh upgrade [options]

Options:
  --host HOST                 VPS host; otherwise CPR_VPS_HOST is required
  --user USER                 SSH user; default root
  --version VERSION           Release tag; required

Just arguments:
  host=HOST version=VERSION user=USER

Environment overrides:
  CPR_VPS_SERVICE, CPR_VPS_BINARY, CPR_VPS_WEB_DIST, CPR_VPS_DATABASE,
  CPR_VPS_PORT, CPR_VPS_BACKUP_ROOT, CPR_PREVIOUS_MIGRATION,
  CPR_TARGET_MIGRATION
EOF
    exit 2
}

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "missing local command: $1"
}

download() {
    curl \
        --fail \
        --location \
        --silent \
        --show-error \
        --retry 3 \
        --connect-timeout 15 \
        --max-time 300 \
        "$1" \
        --output "$2"
}

validate_token() {
    local label="$1"
    local value="$2"
    local pattern="$3"
    [[ "$value" =~ $pattern ]] || die "invalid $label"
}

validate_args() {
    validate_token 'repository' "$APP_REPOSITORY" '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'
    validate_token 'host' "$host" '^[A-Za-z0-9._:-]+$'
    validate_token 'SSH user' "$ssh_user" '^[A-Za-z0-9._-]+$'
    validate_token 'release version' "$release_version" '^v[0-9]+\.[0-9]+\.[0-9]+([-.+][0-9A-Za-z.-]+)?$'
    validate_token 'service name' "$service_name" '^[A-Za-z0-9_.@-]+$'
    validate_token 'database name' "$database_name" '^[A-Za-z0-9_]+$'
    validate_token 'listen port' "$listen_port" '^[0-9]+$'
    validate_token 'previous migration' "$previous_migration" '^[0-9]+$'
    validate_token 'target migration' "$target_migration" '^[0-9]+$'
    validate_token 'binary path' "$binary_path" '^/[A-Za-z0-9._/-]+$'
    validate_token 'web assets path' "$web_dist_path" '^/[A-Za-z0-9._/-]+$'
    validate_token 'backup root' "$backup_root" '^/[A-Za-z0-9._/-]+$'
    [[ "$target_migration" -gt "$previous_migration" ]] \
        || die 'target migration must be greater than previous migration'
}

parse_args() {
    (($# > 0)) || usage
    mode="$1"
    shift
    [[ "$mode" == upgrade ]] || usage

    while (($# > 0)); do
        case "$1" in
            host=*)
                host_arg="${1#host=}"
                shift
                ;;
            user=*)
                user_arg="${1#user=}"
                shift
                ;;
            version=*)
                version_arg="${1#version=}"
                shift
                ;;
            --host)
                (($# >= 2)) || usage
                host_arg="$2"
                shift 2
                ;;
            --user)
                (($# >= 2)) || usage
                user_arg="$2"
                shift 2
                ;;
            --version)
                (($# >= 2)) || usage
                version_arg="$2"
                shift 2
                ;;
            --help|-h)
                usage
                ;;
            *)
                usage
                ;;
        esac
    done

    host="${host_arg:-${CPR_VPS_HOST:-}}"
    ssh_user="${user_arg:-${CPR_VPS_USER:-root}}"
    [[ -n "$version_arg" ]] || die 'version is required: pass version=... or --version ...'
    release_version="$version_arg"
    ssh_target="${ssh_user}@${host}"
    asset_version="${release_version#v}"
    release_asset="${APP_BINARY_NAME}_${asset_version}_linux_amd64.tar.gz"
    release_base_url="https://github.com/${APP_REPOSITORY}/releases/download/${release_version}"
    release_archive_url="${release_base_url}/${release_asset}"
    checksums_url="${release_base_url}/checksums.txt"

    [[ -n "$host" ]] || die 'host is required: pass host=... or set CPR_VPS_HOST'
    validate_args
}

cleanup() {
    if [[ -n "$remote_stage" ]]; then
        ssh "${ssh_options[@]}" "$ssh_target" "rm -rf -- '$remote_stage'" >/dev/null 2>&1 || true
    fi
    if [[ -n "$work_dir" ]]; then
        rm -rf -- "$work_dir"
    fi
}

release_preflight() {
    work_dir="$(mktemp -d "${TMPDIR:-/tmp}/codex-proxy-rs-upgrade.XXXXXX")"
    local checksums_file="$work_dir/checksums.txt"
    local expected_checksum

    log "checking release ${release_version}"
    download "$checksums_url" "$checksums_file"
    expected_checksum="$(awk -v target="$release_asset" '$2 == target {print $1}' "$checksums_file")"
    [[ "$expected_checksum" =~ ^[0-9a-fA-F]{64}$ ]] \
        || die "checksums.txt has no valid entry for ${release_asset}"
    release_checksum="$expected_checksum"
    printf 'release_asset=%s\nrelease_checksum=%s\n' "$release_asset" "$release_checksum"
}

remote_preflight() {
    ssh "${ssh_options[@]}" "$ssh_target" bash -s -- \
        "$service_name" \
        "$binary_path" \
        "$web_dist_path" \
        "$database_name" \
        "$previous_migration" \
        "$target_migration" \
        "$listen_port" <<'REMOTE_PREFLIGHT'
set -Eeuo pipefail

service="$1"
binary="$2"
web_dist="$3"
database="$4"
previous_migration="$5"
target_migration="$6"
listen_port="$7"

die() {
    printf '[remote-preflight] error: %s\n' "$*" >&2
    exit 1
}

command -v curl >/dev/null 2>&1 || die 'missing curl'
command -v systemctl >/dev/null 2>&1 || die 'missing systemctl'
command -v flock >/dev/null 2>&1 || die 'missing flock'
command -v psql >/dev/null 2>&1 || die 'missing psql'
command -v runuser >/dev/null 2>&1 || die 'missing runuser'
[[ -x "$binary" ]] || die "binary is not executable: $binary"
[[ -d "$web_dist" ]] || die "web assets directory is missing: $web_dist"

exec_start="$(systemctl show -p ExecStart --value "$service" 2>/dev/null || true)"
[[ "$exec_start" == *"path=$binary"* ]] || die "${service} does not execute ${binary}"
systemctl is-active --quiet "$service" || die "${service} is not active"

health_status="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 10 "http://127.0.0.1:${listen_port}/healthz" || true)"
[[ "$health_status" == 204 ]] || die "health check returned HTTP ${health_status:-no-response}"

migration_row="$(runuser -u postgres -- psql -X -qAt -d "$database" -v ON_ERROR_STOP=1 -c \
    "select coalesce(min(version) filter (where success), 0)::text || '|' || coalesce(max(version) filter (where success), 0)::text || '|' || (count(*) filter (where success))::text || '|' || (count(*) filter (where not success))::text from _sqlx_migrations;")"
IFS='|' read -r min_success max_success success_count failed_count <<< "$migration_row"
[[ "$min_success" =~ ^[0-9]+$ && "$max_success" =~ ^[0-9]+$ && "$success_count" =~ ^[0-9]+$ && "$failed_count" =~ ^[0-9]+$ ]] \
    || die 'could not parse _sqlx_migrations state'
[[ "$failed_count" == 0 ]] || die "database has ${failed_count} failed migration record(s)"
[[ "$min_success" == 1 && "$max_success" == "$previous_migration" && "$success_count" == "$previous_migration" ]] \
    || die "expected contiguous migrations 1..${previous_migration}, found ${min_success}..${max_success} (${success_count} successful)"

printf 'remote_preflight=ok\nservice=%s\nhealth=204\nmigration=%s\ndatabase=unchanged\n' \
    "$service" "$max_success"
REMOTE_PREFLIGHT
}

stage_release() {
    remote_stage="$(ssh "${ssh_options[@]}" "$ssh_target" 'mktemp -d -p /var/tmp codex-proxy-rs-upgrade.XXXXXXXX')"
    [[ "$remote_stage" =~ ^/var/tmp/codex-proxy-rs-upgrade\.[A-Za-z0-9]+$ ]] \
        || die 'remote staging directory has an unexpected path'

    log "downloading verified release on ${ssh_target}"
    staged_binary_hash="$(ssh "${ssh_options[@]}" "$ssh_target" bash -s -- \
        "$remote_stage" \
        "$release_archive_url" \
        "$release_checksum" \
        "$release_asset" <<'REMOTE_STAGE'
set -Eeuo pipefail

stage="$1"
archive_url="$2"
expected_archive_checksum="$3"
release_asset="$4"
app_binary_name="codex-proxy-rs"

die() {
    printf '[remote-stage] error: %s\n' "$*" >&2
    exit 1
}

command -v curl >/dev/null 2>&1 || die 'missing curl'
command -v find >/dev/null 2>&1 || die 'missing find'
command -v sha256sum >/dev/null 2>&1 || die 'missing sha256sum'
command -v tar >/dev/null 2>&1 || die 'missing tar'
command -v tr >/dev/null 2>&1 || die 'missing tr'
command -v wc >/dev/null 2>&1 || die 'missing wc'

archive="$stage/$release_asset"
archive_listing="$stage/archive.list"
extract_dir="$stage/extracted"

curl \
    --fail \
    --location \
    --silent \
    --show-error \
    --retry 3 \
    --connect-timeout 15 \
    --max-time 300 \
    "$archive_url" \
    --output "$archive"

actual_archive_checksum="$(sha256sum "$archive")"
actual_archive_checksum="${actual_archive_checksum%% *}"
[[ "$actual_archive_checksum" == "$expected_archive_checksum" ]] \
    || die 'release archive checksum mismatch'

tar -tzf "$archive" > "$archive_listing"
while IFS= read -r path; do
    case "$path" in
        /*|..|../*|*/../*|*/..)
            die "unsafe path in release archive: $path"
            ;;
    esac
done < "$archive_listing"

mkdir -p "$extract_dir"
tar -xzf "$archive" -C "$extract_dir"

binary_count="$(find "$extract_dir" -type f -name "$app_binary_name" -print | wc -l | tr -d '[:space:]')"
[[ "$binary_count" == 1 ]] \
    || die "release archive must contain exactly one ${app_binary_name} binary"
binary_source="$(find "$extract_dir" -type f -name "$app_binary_name" -print -quit)"

web_count="$(find "$extract_dir" -type d -path '*/web/dist' -print | wc -l | tr -d '[:space:]')"
[[ "$web_count" == 1 ]] || die 'release archive must contain exactly one web/dist directory'
web_source="$(find "$extract_dir" -type d -path '*/web/dist' -print -quit)"

# 官方二进制可能经过 strip，不保证保留构建版本字符串；Release 文件名与校验和已核对。
chmod 0755 "$binary_source"
mv -- "$binary_source" "$stage/$app_binary_name"
mkdir -p "$stage/web-dist"
cp -a -- "$web_source/." "$stage/web-dist/"

staged_binary_hash="$(sha256sum "$stage/$app_binary_name")"
staged_binary_hash="${staged_binary_hash%% *}"
printf '%s\n' "$staged_binary_hash"
REMOTE_STAGE
    )"
    [[ "$staged_binary_hash" =~ ^[0-9a-fA-F]{64}$ ]] \
        || die 'remote staged binary checksum is invalid'
    printf 'staged_binary_checksum=%s\n' "$staged_binary_hash"
}

activate_release() {
    local expected_binary_hash

    expected_binary_hash="$staged_binary_hash"

    ssh "${ssh_options[@]}" "$ssh_target" bash -s -- \
        "$service_name" \
        "$binary_path" \
        "$web_dist_path" \
        "$database_name" \
        "$previous_migration" \
        "$target_migration" \
        "$listen_port" \
        "$remote_stage" \
        "$backup_root" \
        "$release_version" \
        "$expected_binary_hash" <<'REMOTE_ACTIVATE'
set -Eeuo pipefail

service="$1"
binary="$2"
web_dist="$3"
database="$4"
previous_migration="$5"
target_migration="$6"
listen_port="$7"
stage="$8"
backup_root="$9"
release_version="${10}"
expected_binary_hash="${11}"
APP_BINARY_NAME="codex-proxy-rs"

die() {
    printf '[remote-upgrade] error: %s\n' "$*" >&2
    exit 1
}

migration_state() {
    local migration_row
    local min_success
    local max_success
    local success_count
    local failed_count

    if ! migration_row="$(runuser -u postgres -- psql -X -qAt -d "$database" -v ON_ERROR_STOP=1 -c \
        "select coalesce(min(version) filter (where success), 0)::text || '|' || coalesce(max(version) filter (where success), 0)::text || '|' || (count(*) filter (where success))::text || '|' || (count(*) filter (where not success))::text from _sqlx_migrations;" 2>/dev/null)"; then
        printf 'ambiguous\n'
        return 0
    fi
    IFS='|' read -r min_success max_success success_count failed_count <<< "$migration_row"
    if [[ ! "$min_success" =~ ^[0-9]+$ || ! "$max_success" =~ ^[0-9]+$ || ! "$success_count" =~ ^[0-9]+$ || ! "$failed_count" =~ ^[0-9]+$ ]]; then
        printf 'ambiguous\n'
        return 0
    fi
    if [[ "$failed_count" != 0 ]]; then
        printf 'ambiguous\n'
    elif [[ "$min_success" == 1 && "$max_success" == "$target_migration" && "$success_count" == "$target_migration" ]]; then
        printf 'target\n'
    elif [[ "$min_success" == 1 && "$max_success" == "$previous_migration" && "$success_count" == "$previous_migration" ]]; then
        printf 'previous\n'
    else
        printf 'unexpected\n'
    fi
}

health_ok() {
    local status
    status="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 10 "http://127.0.0.1:${listen_port}/healthz" || true)"
    [[ "$status" == 204 ]] && systemctl is-active --quiet "$service"
}

wait_for_health() {
    local attempt=0
    while ((attempt < 60)); do
        if health_ok; then
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 2
    done
    return 1
}

restore_old_files() {
    local backup_dir="$1"
    local failed_dir="$backup_dir/failed-new"
    local restore_error=0

    install -d -m 0700 -- "$failed_dir" || return 1
    if [[ -e "$binary" ]]; then
        mv -- "$binary" "$failed_dir/$APP_BINARY_NAME" || restore_error=1
    fi
    if [[ -e "$web_dist" ]]; then
        mv -- "$web_dist" "$failed_dir/web-dist" || restore_error=1
    fi
    if [[ ! -e "$binary" ]]; then
        cp -a -- "$backup_dir/$APP_BINARY_NAME" "$binary" || restore_error=1
    fi
    if [[ ! -e "$web_dist" ]]; then
        cp -a -- "$backup_dir/web-dist" "$web_dist" || restore_error=1
    fi
    return "$restore_error"
}

fail_after_replacement() {
    local reason="$1"
    local state

    trap - ERR
    systemctl stop "$service" >/dev/null 2>&1 || true
    state="$(migration_state)"
    if [[ "$state" == previous ]]; then
        if restore_old_files "$backup_dir" \
            && { systemctl reset-failed "$service" >/dev/null 2>&1 || true; } \
            && systemctl start "$service" \
            && wait_for_health; then
            printf 'upgrade=failed\nrollback=pre-migration-files-only\nreason=%s\nmigration=%s\nbackup_dir=%s\ndatabase=preserved\n' \
                "$reason" "$previous_migration" "$backup_dir"
            exit 1
        fi
        die "${reason}; pre-migration restore or old service restart failed; backup kept at ${backup_dir}"
    fi

    printf 'upgrade=blocked\nrollback=none\nreason=%s\nmigration_state=%s\nrelease=%s\nbackup_dir=%s\ndatabase=left-at-current-state\n' \
        "$reason" "$state" "$release_version" "$backup_dir" >&2
    exit 1
}

on_replacement_error() {
    local exit_code=$?
    fail_after_replacement "command failed after file replacement (exit ${exit_code})"
}

exec 9>/var/lock/codex-proxy-rs-upgrade.lock
flock -n 9 || die 'another native upgrade is already running'

command -v curl >/dev/null 2>&1 || die 'missing curl'
command -v systemctl >/dev/null 2>&1 || die 'missing systemctl'
command -v psql >/dev/null 2>&1 || die 'missing psql'
command -v runuser >/dev/null 2>&1 || die 'missing runuser'
command -v sha256sum >/dev/null 2>&1 || die 'missing sha256sum'
[[ -x "$binary" ]] || die "binary is not executable: $binary"
[[ -d "$web_dist" ]] || die "web assets directory is missing: $web_dist"
[[ -x "$stage/$APP_BINARY_NAME" ]] || die 'staged binary is missing'
[[ -d "$stage/web-dist" ]] || die 'staged web assets are missing'

state="$(migration_state)"
[[ "$state" == previous ]] || die "database changed during preflight: state=${state}"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0700 -- "$backup_root"
backup_dir="${backup_root%/}/${release_version}-${timestamp}"
[[ ! -e "$backup_dir" ]] || die "backup directory already exists: ${backup_dir}"
install -d -m 0700 -- "$backup_dir"
old_binary_checksum="$(sha256sum "$binary" | cut -d ' ' -f1)"

systemctl stop "$service" || die "failed to stop ${service}"
systemctl is-active --quiet "$service" && die "${service} remained active after stop"

new_binary_moved=0
new_web_moved=0
swap_error=0

if ! mv -- "$binary" "$backup_dir/$APP_BINARY_NAME"; then
    swap_error=1
fi
if ((swap_error == 0)) && ! mv -- "$web_dist" "$backup_dir/web-dist"; then
    swap_error=1
fi
if ((swap_error == 0)) && mv -- "$stage/$APP_BINARY_NAME" "$binary"; then
    new_binary_moved=1
fi
if ((swap_error == 0)) && mv -- "$stage/web-dist" "$web_dist"; then
    new_web_moved=1
fi
if ((swap_error == 0)) && ((new_binary_moved == 0 || new_web_moved == 0)); then
    swap_error=1
fi

if ((swap_error != 0)); then
    recovery_error=0
    failed_dir="$backup_dir/failed-new"
    if ! install -d -m 0700 -- "$failed_dir"; then
        recovery_error=1
    fi
    if ((new_binary_moved == 1)) && [[ -e "$binary" ]]; then
        mv -- "$binary" "$failed_dir/$APP_BINARY_NAME" || recovery_error=1
    fi
    if ((new_web_moved == 1)) && [[ -e "$web_dist" ]]; then
        mv -- "$web_dist" "$failed_dir/web-dist" || recovery_error=1
    fi
    if [[ ! -e "$binary" && -e "$backup_dir/$APP_BINARY_NAME" ]]; then
        cp -a -- "$backup_dir/$APP_BINARY_NAME" "$binary" || recovery_error=1
    fi
    if [[ ! -e "$web_dist" && -e "$backup_dir/web-dist" ]]; then
        cp -a -- "$backup_dir/web-dist" "$web_dist" || recovery_error=1
    fi
    if ((recovery_error != 0)); then
        die "file replacement failed and pre-migration recovery failed; backup kept at ${backup_dir}"
    fi
    systemctl start "$service" || die "file replacement failed and old service failed to restart; backup kept at ${backup_dir}"
    die "file replacement failed before migration; backup kept at ${backup_dir}"
fi

printf 'backup_dir=%s\nold_binary_checksum=%s\n' "$backup_dir" "$old_binary_checksum"
trap on_replacement_error ERR

chmod 0755 -- "$binary"
find "$web_dist" -type d -exec chmod 0755 {} +
find "$web_dist" -type f -exec chmod 0644 {} +
remote_binary_hash="$(sha256sum "$binary" | cut -d ' ' -f1)"
[[ "$remote_binary_hash" == "$expected_binary_hash" ]] \
    || fail_after_replacement 'installed binary checksum mismatch'

systemctl reset-failed "$service" || true
start_ok=1
if ! systemctl start "$service"; then
    start_ok=0
fi
if ((start_ok == 1)) && wait_for_health; then
    state="$(migration_state)"
    if [[ "$state" == target ]]; then
        printf 'upgrade=success\nrelease=%s\nmigration=%s\nhealth=204\nbinary_checksum=%s\nbackup_dir=%s\ndatabase=preserved\n' \
            "$release_version" "$target_migration" "$remote_binary_hash" "$backup_dir"
        exit 0
    fi
fi

fail_after_replacement 'new service failed health or migration verification'
REMOTE_ACTIVATE
}

main() {
    parse_args "$@"
    need_cmd curl
    need_cmd awk
    need_cmd mktemp
    need_cmd ssh

    ssh_options=(-o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=15)

    release_preflight
    remote_preflight
    stage_release
    activate_release
}

trap cleanup EXIT
main "$@"
