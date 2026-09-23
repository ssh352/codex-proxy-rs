# Show the macOS LaunchAgent status and verify the local VPS tunnel health endpoint.
vps-tunnel-app-launchd-status:
    bash "{{ justfile_directory() }}/deploy/macos/vps-tunnel-launchd.sh" status
    bash "{{ justfile_directory() }}/deploy/macos/vps-tunnel-launchd.sh" health

# codex-proxy-rs 原生 systemd VPS 升级入口。
#
# 保留数据库、配置、PostgreSQL 和 Redis。升级必须显式确认目标版本，并采用
# 只向前的迁移策略；目标迁移已写入数据库后不会再启动旧二进制。
#
# 示例：
#

# just vps-upgrade host=179.255.105.21 version=v3.13.1
vps-upgrade *args:
    bash "{{ justfile_directory() }}/deploy/native-vps-upgrade.sh" upgrade {{ args }}
