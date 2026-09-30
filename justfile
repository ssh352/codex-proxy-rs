# Show the macOS LaunchAgent status and verify the local VPS tunnel health endpoint.
vps-tunnel-app-launchd-status:
    bash "{{ justfile_directory() }}/deploy/macos/vps-tunnel-launchd.sh" status
    bash "{{ justfile_directory() }}/deploy/macos/vps-tunnel-launchd.sh" health

# 每次显式给出 VPS 主机。默认预览并确认最新正式版；yes=true 用于无人值守。
#
# just vps-upgrade host=<vps-host>
# just vps-upgrade check host=<vps-host>
# just vps-upgrade host=<vps-host> yes=true
# Upgrade to latest stable with confirmation; host=<vps-host> required, check previews only, yes=true skips confirmation.
vps-upgrade *args:
    bash "{{ justfile_directory() }}/deploy/native-vps-upgrade.sh" upgrade {{ args }}
