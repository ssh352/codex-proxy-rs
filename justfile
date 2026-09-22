# Show the macOS LaunchAgent status, configured forwards, and log paths for the VPS SSH tunnel.
vps-tunnel-app-launchd-status:
    bash "{{ justfile_directory() }}/deploy/macos/vps-tunnel-launchd.sh" status
