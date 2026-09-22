# Show the macOS LaunchAgent status and verify the local VPS tunnel health endpoint.
vps-tunnel-app-launchd-status:
    bash "{{ justfile_directory() }}/deploy/macos/vps-tunnel-launchd.sh" status
    bash "{{ justfile_directory() }}/deploy/macos/vps-tunnel-launchd.sh" health
