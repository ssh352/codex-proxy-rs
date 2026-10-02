#!/usr/bin/env python3
"""原生 systemd VPS 升级。仅依赖 Python 标准库和目标机已有系统工具。"""

import concurrent.futures
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time

APP = "codex-proxy-rs"
UNIT = "codex-proxy-vps-upgrade"
GATE = "cpr_vps_upgrade"
PLUGIN_MANIFEST = "plugin-release-manifest.json"
TERMINAL = {"succeeded", "restored", "failed", "blocked", "already-current"}
STABLE_VERSION = re.compile(
    r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
USAGE = """Usage:
  just vps-upgrade host=HOST [user=root] [yes=true]
  just vps-upgrade check host=HOST

Uses the latest stable release, shows its preview, then asks once.
check is read-only. yes=true skips only the confirmation.
"""


class UpgradeError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise UpgradeError(message)


def run(args, **kwargs):
    # 子进程的 stderr 可能包含数据库细节；报告只保留命令与阶段。
    kwargs.setdefault("stdout", subprocess.PIPE)
    kwargs.setdefault("stderr", subprocess.PIPE)
    kwargs.setdefault("timeout", 300)
    try:
        return subprocess.run([str(arg) for arg in args], check=True, **kwargs).stdout
    except (subprocess.SubprocessError, OSError) as error:
        if Path(str(args[0])).name == "ssh":
            stderr = getattr(error, "stderr", None)
            if isinstance(stderr, bytes):
                lines = [
                    line.strip()
                    for line in stderr.decode(errors="replace").splitlines()
                ]
                remote_error = next(
                    (
                        line.removeprefix("[native-vps-upgrade] ")
                        for line in lines
                        if line.startswith("[native-vps-upgrade] ")
                    ),
                    None,
                )
                if remote_error:
                    raise UpgradeError(
                        f"Remote command failed: {remote_error[:240]}"
                    ) from error
                transport_error = next(
                    (
                        line
                        for line in lines
                        if line.startswith("ssh:")
                        or "Permission denied" in line
                        or "Host key verification failed" in line
                        or "Connection timed out" in line
                    ),
                    None,
                )
                if transport_error:
                    raise UpgradeError(
                        f"SSH transport failed: {transport_error[:240]}"
                    ) from error
        raise UpgradeError(
            f"{Path(str(args[0])).name} failed; operation stopped"
        ) from error


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_version(value):
    match = STABLE_VERSION.fullmatch(value)
    return tuple(map(int, match.groups())) if match else None


def fingerprint(path):
    path = Path(path)
    require(not path.is_symlink(), f"Symlink is unsupported: {path}")
    if not path.exists():
        return None
    if path.is_file():
        return sha(path)
    digest = hashlib.sha256()
    for child in sorted(path.rglob("*")):
        require(not child.is_symlink(), f"Symlink is unsupported: {child}")
        require(
            child.is_dir() or child.is_file(), f"Special file is unsupported: {child}"
        )
        record = [
            child.relative_to(path).as_posix(),
            sha(child) if child.is_file() else "dir",
        ]
        digest.update(json.dumps(record).encode())
    return digest.hexdigest()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def parse_args(arguments):
    if not arguments or "--help" in arguments or "-h" in arguments:
        print(USAGE)
        return None
    require(arguments[0] == "upgrade", "Expected upgrade; use --help for usage")
    options = {"user": "root", "check": False, "yes": False}
    seen = set()
    for argument in arguments[1:]:
        if argument == "check":
            key, value = "check", True
        else:
            key, separator, value = argument.partition("=")
            require(
                separator and key in {"host", "user", "yes"},
                f"Unknown argument: {argument}",
            )
            if key == "yes":
                require(value in {"true", "false"}, "yes= expects true or false")
                value = value == "true"
        require(key not in seen, f"Repeated argument: {key}")
        seen.add(key)
        options[key] = value
    require("host" in options, "host= is required (for example host=179.255.105.21)")
    for key, pattern in (
        ("host", r"[A-Za-z0-9][A-Za-z0-9.:-]*"),
        ("user", r"[A-Za-z_][A-Za-z0-9_-]*"),
    ):
        require(re.fullmatch(pattern, options[key]), f"Invalid {key}")
    return options


def configuration():
    defaults = {
        "service": ("CPR_VPS_SERVICE", "codex-proxy-rs.service"),
        "binary": ("CPR_VPS_BINARY", "/opt/codex-proxy-rs/codex-proxy-rs"),
        "web": ("CPR_VPS_WEB_DIST", "/opt/codex-proxy-rs/web/dist"),
        "database": ("CPR_VPS_DATABASE", "codex_proxy"),
        "port": ("CPR_VPS_PORT", "8080"),
        "backups": ("CPR_VPS_BACKUP_ROOT", "/var/lib/codex-proxy-rs/upgrade-backups"),
    }
    config = {
        key: os.environ.get(env, default) for key, (env, default) in defaults.items()
    }
    require(re.fullmatch(r"[A-Za-z0-9_]+", config["database"]), "Invalid database name")
    require(
        re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", config["service"]),
        "Invalid service name",
    )
    require(
        config["port"].isdigit() and 1 <= int(config["port"]) <= 65535,
        "Invalid listen port",
    )
    port = int(config.pop("port"))
    for key in ("binary", "web", "backups"):
        require(
            re.fullmatch(r"/[A-Za-z0-9_./-]+", config[key])
            and ".." not in Path(config[key]).parts,
            f"Invalid {key} path",
        )
    return config | {"port": port}


def download(url, destination=None):
    args = [
        "curl",
        "--fail",
        "--location",
        "--silent",
        "--show-error",
        "--retry",
        "2",
        "--connect-timeout",
        "15",
        "--max-time",
        "300",
        url,
    ]
    if destination:
        args.extend(["--output", str(destination)])
    return run(args, timeout=1000)


def extract_release(archive, destination):
    destination = Path(destination)
    directory_modes = {}
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        names = set()
        for item in members:
            path = PurePosixPath(item.name)
            require(
                not path.is_absolute()
                and ".." not in path.parts
                and path.parts
                and path.parts[0] == APP,
                "Unsafe release archive path",
            )
            require(
                item.isfile() or item.isdir(),
                "Release archive contains a link or special file",
            )
            require(path not in names, "Duplicate release archive path")
            names.add(path)
        require(
            sum(item.size for item in members) < 4 * 1024**3,
            "Release archive is too large",
        )
        for item in members:
            target = destination / item.name
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
                directory_modes[target] = (item.mode & 0o755) | 0o500
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(item)
                if source is None:
                    raise UpgradeError("Cannot read release archive member")
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod((item.mode & 0o755) | 0o400)
        for directory, mode in sorted(
            directory_modes.items(), key=lambda entry: len(entry[0].parts), reverse=True
        ):
            directory.chmod(mode)
    root = destination / APP
    require(
        (root / APP).is_file() and (root / "web/dist/index.html").is_file(),
        "Release archive is incomplete",
    )
    return root


def validate_release_plugins(root, release):
    directory = root / "plugins/official"
    expected = release["plugin_manifest_sha"]
    if expected is None:
        require(
            not directory.exists(),
            "Release has official plugins without a checksummed manifest",
        )
        return
    path = directory / PLUGIN_MANIFEST
    require(path.is_file(), "Release archive is missing the official plugin manifest")
    require(sha(path) == expected, "Official plugin manifest checksum mismatch")
    try:
        manifest = json.loads(path.read_bytes())
    except (ValueError, UnicodeDecodeError) as error:
        raise UpgradeError("Invalid official plugin release manifest") from error
    # 只核对发行身份；插件合同与包体仍由目标宿主在启动时校验。
    require(
        isinstance(manifest, dict)
        and manifest.get("schema_version") == 1
        and manifest.get("sealed") is True
        and manifest.get("gateway_version") == release["tag"].removeprefix("v")
        and manifest.get("gateway_git_sha") == release["commit"],
        "Official plugin manifest does not belong to the selected release",
    )


def migration_contract(repository, commit):
    base = f"https://raw.githubusercontent.com/{repository}/{commit}/backend/migrations"
    listing = json.loads(
        download(
            f"https://api.github.com/repos/{repository}/contents/backend/migrations?ref={commit}"
        )
    )
    names = {entry["name"] for entry in listing if entry["name"].endswith(".sql")}
    frozen = {}
    for line in download(f"{base}/.frozen-sha256").decode().splitlines():
        parts = line.split()
        require(
            len(parts) == 2 and re.fullmatch(r"[0-9a-f]{64}", parts[0]),
            "Invalid frozen migration manifest",
        )
        checksum, name = parts
        require(
            re.fullmatch(r"\d+_[A-Za-z0-9_]+\.sql", name) and name not in frozen,
            "Invalid migration filename",
        )
        frozen[name] = checksum
    require(
        names == set(frozen) and names,
        "Release migrations do not match the frozen manifest",
    )

    def resolve(name):
        source = download(f"{base}/{name}")
        require(
            hashlib.sha256(source).hexdigest() == frozen[name],
            f"Frozen migration checksum mismatch: {name}",
        )
        # SQLx 按原始迁移字节计算 SHA-384。
        return {
            "version": int(name.split("_", 1)[0]),
            "checksum": hashlib.sha384(source).hexdigest(),
            "success": True,
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        migrations = sorted(pool.map(resolve, names), key=lambda item: item["version"])
    require(
        [item["version"] for item in migrations] == list(range(1, len(migrations) + 1)),
        "Release migrations are not contiguous",
    )
    return migrations


def resolve_release():
    repository = os.environ.get("CPR_UPDATE_REPOSITORY", "zyycn/codex-proxy-rs")
    require(
        re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository),
        "Invalid repository",
    )
    release = json.loads(
        download(f"https://api.github.com/repos/{repository}/releases/latest")
    )
    tag = release["tag_name"]
    require(
        stable_version(tag) is not None
        and tag.startswith("v")
        and not release["draft"]
        and not release["prerelease"],
        "Latest release is not stable",
    )
    asset = f"{APP}_{tag[1:]}_linux_amd64.tar.gz"
    base = f"https://github.com/{repository}/releases/download/{tag}"
    checksums = {}
    for line in download(f"{base}/checksums.txt").decode().splitlines():
        checksum, name = line.split()
        require(
            re.fullmatch(r"[0-9a-f]{64}", checksum) and name not in checksums,
            "Invalid release checksums",
        )
        checksums[name] = checksum
    require(asset in checksums, "Release lacks a checksummed binary archive")
    # 标签解析为提交后，迁移读取始终使用该 SHA，不能使用可能移动的分支名。
    commit = json.loads(
        download(f"https://api.github.com/repos/{repository}/commits/{tag}")
    )["sha"]
    require(
        re.fullmatch(r"[0-9a-f]{40}", commit),
        "Invalid release commit",
    )
    print(f"Checking {tag} release metadata and migrations…", flush=True)
    return {
        "tag": tag,
        "commit": commit,
        "archive": asset,
        "base_url": base,
        "archive_sha": checksums[asset],
        "plugin_manifest_sha": checksums.get(PLUGIN_MANIFEST),
        "migrations": migration_contract(repository, commit),
    }


def migration_prefix(current, target):
    require(
        current and all(item.get("success") is True for item in current),
        "Database has missing or failed migrations",
    )
    require(
        len(current) <= len(target),
        "Database is newer than the selected release; downgrade refused",
    )
    require(
        current == target[: len(current)],
        "Database migration checksums differ from the selected release",
    )


def psql(config, query):
    return (
        run(
            [
                "runuser",
                "-u",
                "postgres",
                "--",
                "psql",
                "-X",
                "-qAt",
                "--no-password",
                "-d",
                config["database"],
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                query,
            ]
        )
        .decode()
        .strip()
    )


def migrations(config):
    query = (
        "select coalesce(json_agg(row_to_json(m) order by version), '[]'::json) "
        "from (select version, encode(checksum, 'hex') as checksum, success "
        "from _sqlx_migrations) m"
    )
    return json.loads(psql(config, query))


def properties(service):
    output = run(
        [
            "systemctl",
            "show",
            service,
            "-p",
            "MainPID",
            "-p",
            "WorkingDirectory",
            "-p",
            "User",
        ]
    ).decode()
    return dict(line.split("=", 1) for line in output.splitlines())


def file_paths(config, config_path):
    return {
        "binary": Path(config["binary"]),
        "web": Path(config["web"]),
        "official_plugins": Path(config["binary"]).parent / "plugins/official",
        "config": Path(config_path),
    }


def health(config, mark=0, path="/healthz"):
    connection = http.client.HTTPConnection("127.0.0.1", config["port"], timeout=3)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        connection.sock = sock
        sock.settimeout(3)
        if mark:
            sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_MARK"), mark)
        sock.connect(("127.0.0.1", config["port"]))
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status == (204 if path == "/healthz" else 200)
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def gate_present():
    tables = json.loads(run(["nft", "-j", "list", "tables"]))
    return any(
        item.get("table", {}).get("name") == GATE
        and item["table"].get("family") == "inet"
        for item in tables["nftables"]
    )


def validate_gate(port):
    rules = (
        f"add table inet {GATE}\n"
        f"add chain inet {GATE} input {{ type filter hook input priority -10; policy accept; }}\n"
        f'add rule inet {GATE} input iifname "lo" ip saddr 127.0.0.1 '
        f"meta mark 1 tcp dport {port} accept\n"
        f"add rule inet {GATE} input tcp dport {port} reject with tcp reset\n"
    )
    run(["nft", "--check", "-f", "-"], input=rules.encode())


def installed_version(binary, user, target):
    target_version = stable_version(target)
    if target_version is None:
        raise UpgradeError("Cannot parse target version; upgrade refused")
    try:
        output = (
            run(["runuser", "-u", user, "--", binary, "--version"], timeout=10)
            .decode()
            .strip()
        )
    except (UpgradeError, UnicodeDecodeError):
        return None
    installed = stable_version(output)
    if installed is None:
        return None
    require(
        installed <= target_version,
        "Installed version is newer than the selected release; downgrade refused",
    )
    return "v" + output.removeprefix("v")


def validate_version_state(version, current, target):
    if version is None:
        # 旧版没有版本命令；仅接受数据库严格落后于目标迁移清单的情况。
        require(
            len(current) < len(target),
            "Installed version is unknown and database has no pending target migrations; downgrade refused",
        )


def inspect_vps(config, release):
    require(
        sys.platform == "linux" and os.geteuid() == 0,
        "VPS inspection requires root on Linux",
    )
    require(os.uname().machine == "x86_64", "This VPS workflow requires Linux amd64")
    for command in (
        "curl",
        "systemctl",
        "systemd-run",
        "nft",
        "runuser",
        "psql",
        "pg_dump",
        "pg_restore",
        "cp",
    ):
        require(shutil.which(command), f"VPS is missing {command}")
    require(
        not gate_present(),
        f"Previous maintenance gate remains: inet {GATE}; inspect before retrying",
    )
    validate_gate(config["port"])
    props = properties(config["service"])
    pid = int(props["MainPID"])
    require(
        pid > 0
        and Path(f"/proc/{pid}/exe").resolve() == Path(config["binary"]).resolve(),
        "Service is not running the configured binary",
    )
    directory = Path(props["WorkingDirectory"])
    config_path = next(
        (
            parent / "deploy/config.yaml"
            for parent in (directory, *directory.parents)
            if (parent / "deploy/config.yaml").is_file()
        ),
        None,
    )
    require(
        config_path is not None,
        "Cannot locate the running service's deploy/config.yaml",
    )
    paths = file_paths(config, config_path)
    require(
        not paths["official_plugins"].parent.is_symlink(),
        "Official plugins parent cannot be a symlink",
    )
    files = {name: fingerprint(path) for name, path in paths.items()}
    require(files["binary"] and files["web"], "Application files are missing")
    require(
        sha(f"/proc/{pid}/exe") == files["binary"],
        "Running binary differs from the installed file",
    )
    current = migrations(config)
    migration_prefix(current, release["migrations"])
    require(
        health(config) and health(config, path="/"),
        "Current service or web assets are unhealthy",
    )
    version = installed_version(
        config["binary"], props.get("User") or "root", release["tag"]
    )
    validate_version_state(version, current, release["migrations"])
    database_bytes = int(psql(config, "select pg_database_size(current_database())"))
    application_bytes = sum(
        path.stat().st_size
        for root in paths.values()
        if root.exists()
        for path in ([root] if root.is_file() else root.rglob("*"))
        if path.is_file()
    )
    for location, required in (
        (Path(config["backups"]), database_bytes + application_bytes + 128 * 1024**2),
        (Path(config["binary"]).parent, application_bytes * 2 + 128 * 1024**2),
    ):
        while not location.exists():
            location = location.parent
        require(
            shutil.disk_usage(location).free > required,
            f"Insufficient free space at {location}",
        )
    return {
        "version": version,
        "files": files,
        "migrations": current,
        "config_path": str(config_path),
        "database_bytes": database_bytes,
    }


class Deployment:
    def __init__(self, plan, stage):
        self.plan = plan
        self.config = plan["config"]
        self.before = plan["before"]
        self.release = plan["release"]
        self.stage = Path(stage)
        self.backups = Path(self.config["backups"])
        self.snapshot = self.backups / (".pending-" + self.stage.name)
        self.paths = file_paths(self.config, self.before["config_path"])
        self.mark = secrets.randbelow(2**30) + 1

    def status(self, phase, **details):
        value = {"phase": phase, "release": self.release["tag"], **details}
        if (self.snapshot / "snapshot.json").is_file():
            value["backup"] = str(self.snapshot)
        elif (self.backups / "previous").is_dir():
            value["backup"] = str(self.backups / "previous")
            if self.snapshot.exists():
                value["incomplete_backup"] = str(self.snapshot)
        write_json(self.stage / "status.json", value)

    def gate_on(self):
        port = self.config["port"]
        rules = (
            f"add table inet {GATE}\n"
            f"add chain inet {GATE} input {{ type filter hook input priority -10; policy accept; }}\n"
            f'add rule inet {GATE} input iifname "lo" ip saddr 127.0.0.1 '
            f"meta mark {self.mark} tcp dport {port} accept\n"
            f"add rule inet {GATE} input tcp dport {port} reject with tcp reset\n"
        )
        run(["nft", "-f", "-"], input=rules.encode())

    def gate_off(self):
        run(["nft", "delete", "table", "inet", GATE])

    def stop(self):
        run(["systemctl", "stop", self.config["service"]], timeout=180)
        require(
            int(properties(self.config["service"])["MainPID"]) == 0,
            "Service did not stop",
        )

    def start(self):
        run(["systemctl", "reset-failed", self.config["service"]])
        run(["systemctl", "start", self.config["service"]], timeout=180)

    def verify(self, expected_files, expected_migrations):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if health(self.config, self.mark) and health(self.config, self.mark, "/"):
                pid = int(properties(self.config["service"])["MainPID"])
                require(
                    pid > 0 and sha(f"/proc/{pid}/exe") == expected_files["binary"],
                    "Running binary checksum mismatch",
                )
                require(
                    migrations(self.config) == expected_migrations,
                    "Database migration verification failed",
                )
                require(
                    all(
                        fingerprint(self.paths[key]) == value
                        for key, value in expected_files.items()
                    ),
                    "Installed files differ from verified files",
                )
                require(
                    gate_present() and not health(self.config),
                    "Maintenance gate is not holding incoming requests",
                )
                return
            time.sleep(2)
        raise UpgradeError("Service failed startup or health verification")

    def backup(self):
        self.snapshot.mkdir(mode=0o700)
        require(
            migrations(self.config) == self.before["migrations"],
            "Database changed after preview",
        )
        require(
            all(
                fingerprint(path) == self.before["files"][name]
                for name, path in self.paths.items()
            ),
            "Application changed after preview",
        )
        for name, path in self.paths.items():
            if path.exists():
                run(["cp", "-a", path, self.snapshot / name])
        dump = self.snapshot / "database.dump"
        with dump.open("wb") as output:
            run(
                [
                    "runuser",
                    "-u",
                    "postgres",
                    "--",
                    "pg_dump",
                    "--no-password",
                    "--format=custom",
                    "--create",
                    "--dbname",
                    self.config["database"],
                ],
                stdout=output,
                timeout=1800,
            )
            output.flush()
            os.fsync(output.fileno())
        with dump.open("rb") as source:
            run(["pg_restore", "--list"], stdin=source)
        write_json(
            self.snapshot / "snapshot.json",
            {
                "format": 1,
                "before": self.before,
                "dump_sha": sha(dump),
                "config": self.config,
            },
        )

    def replace(self, source, destination):
        if destination.is_dir():
            shutil.rmtree(destination)
        elif destination.exists():
            destination.unlink()
        if source.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            run(["cp", "-a", source, destination])

    def install(self):
        root = self.stage / "release" / APP
        sources = {
            "binary": root / APP,
            "web": root / "web/dist",
            "official_plugins": root / "plugins/official",
        }
        plugins_parent = self.paths["official_plugins"].parent
        if sources["official_plugins"].exists() and not plugins_parent.exists():
            plugins_parent.mkdir(mode=0o755)
            # worker 使用 0077 umask；首次安装时仍需允许服务用户遍历发行目录。
            plugins_parent.chmod(0o755)
        for name, source in sources.items():
            self.replace(source, self.paths[name])

    def restore(self):
        metadata = json.loads((self.snapshot / "snapshot.json").read_text())
        require(
            sha(self.snapshot / "database.dump") == metadata["dump_sha"],
            "Database backup checksum mismatch",
        )
        query = (
            "select pg_terminate_backend(pid) from pg_stat_activity "
            "where datname=current_database() and pid<>pg_backend_pid()"
        )
        psql(self.config, query)
        with (self.snapshot / "database.dump").open("rb") as source:
            run(
                [
                    "runuser",
                    "-u",
                    "postgres",
                    "--",
                    "pg_restore",
                    "--no-password",
                    "--exit-on-error",
                    "--clean",
                    "--if-exists",
                    "--create",
                    "--dbname",
                    "postgres",
                ],
                stdin=source,
                timeout=1800,
            )
        for name, destination in self.paths.items():
            self.replace(self.snapshot / name, destination)

    def retain(self):
        previous = self.backups / "previous"
        retired = self.backups / (".retired-" + self.stage.name)
        moved_previous = False
        if previous.exists():
            previous.rename(retired)
            moved_previous = True
        try:
            self.snapshot.rename(previous)
            self.snapshot = previous
        except BaseException:
            if moved_previous and not previous.exists():
                retired.rename(previous)
            raise
        if retired.exists():
            try:
                shutil.rmtree(retired)
            except OSError:
                pass
        for old in self.backups.iterdir():
            if (
                re.fullmatch(r"v\d+\.\d+\.\d+-\d{8}T\d{6}Z", old.name)
                and (old / APP).is_file()
                and (old / "web-dist").is_dir()
            ):
                try:
                    shutil.rmtree(old)
                except OSError:
                    pass

    def execute(self):
        gated = changed = committed = False
        try:
            self.status("preflight")
            current = inspect_vps(self.config, self.release)
            require(
                current["files"] == self.before["files"]
                and current["migrations"] == self.before["migrations"],
                "Deployment changed after preview; run the command again",
            )
            self.backups.mkdir(mode=0o700, parents=True, exist_ok=True)
            require(
                not self.backups.is_symlink(), "Backup directory cannot be a symlink"
            )
            self.backups.chmod(0o700)
            self.gate_on()
            gated = True
            require(
                health(self.config, self.mark) and not health(self.config),
                "Cannot verify the maintenance gate",
            )
            self.status("backing-up")
            self.stop()
            self.backup()
            self.status("installing")
            changed = True
            self.install()
            self.start()
            self.status("verifying")
            expected = {
                **self.release["files"],
                "config": self.before["files"]["config"],
            }
            self.verify(expected, self.release["migrations"])
            self.retain()
            self.status("reopening")
            # 放行新请求后不可恢复数据库，否则会丢弃这些请求的写入。
            committed = True
            self.gate_off()
            self.status("succeeded")
        except BaseException as error:
            reason = (
                str(error)
                if isinstance(error, UpgradeError)
                else "Upgrade interrupted or filesystem operation failed"
            )
            if not gated:
                self.status("failed", reason=reason)
                return
            if committed:
                self.status(
                    "blocked",
                    reason="Deployment verified; finalization requires inspection. "
                    + reason,
                )
                return
            try:
                self.status("restoring", reason=reason)
                require(
                    gate_present() and not health(self.config),
                    "Traffic gate was lost; automatic database restore refused",
                )
                self.stop()
                if changed:
                    self.restore()
                self.start()
                self.verify(self.before["files"], self.before["migrations"])
                if changed:
                    self.retain()
                elif self.snapshot.exists():
                    shutil.rmtree(self.snapshot)
                committed = True
                self.gate_off()
                self.status("restored", reason=reason)
            except BaseException:
                if not committed:
                    try:
                        self.stop()
                    except UpgradeError:
                        pass
                self.status(
                    "blocked",
                    reason="Automatic recovery could not complete; inspect backup and service. "
                    + reason,
                )


def worker(plan_path):
    plan_file = Path(plan_path)
    plan = json.loads(plan_file.read_text())
    stage = plan_file.parent
    deployment = Deployment(plan, stage)
    try:
        with open("/var/lock/codex-proxy-rs-upgrade.lock", "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                deployment.status("failed", reason="Another native upgrade is running")
                return

            def interrupted(_signal, _frame):
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                raise UpgradeError("Upgrade worker interrupted")

            signal.signal(signal.SIGTERM, interrupted)
            deployment.status("checking-release")
            archive = stage / "release.tar.gz"
            download(
                f"{plan['release']['base_url']}/{plan['release']['archive']}", archive
            )
            require(
                sha(archive) == plan["release"]["archive_sha"],
                "Release archive checksum mismatch",
            )
            root = extract_release(archive, stage / "release")
            validate_release_plugins(root, plan["release"])
            plan["release"]["files"] = {
                "binary": fingerprint(root / APP),
                "web": fingerprint(root / "web/dist"),
                "official_plugins": fingerprint(root / "plugins/official"),
            }
            write_json(plan_file, plan)
            if all(
                plan["before"]["files"][key] == value
                for key, value in plan["release"]["files"].items()
            ):
                require(
                    plan["before"]["migrations"] == plan["release"]["migrations"],
                    "Current files have unexpected migration state",
                )
                deployment.status("already-current")
                return
            deployment.execute()
    except BaseException as error:
        status = {
            "phase": "failed",
            "release": plan["release"]["tag"],
            "reason": str(error)
            if isinstance(error, UpgradeError)
            else "Release staging failed; application was not changed",
        }
        if deployment.snapshot.exists():
            status["incomplete_backup"] = str(deployment.snapshot)
        previous = deployment.backups / "previous"
        if previous.is_dir():
            status["backup"] = str(previous)
        write_json(stage / "status.json", status)


def launch(plan_path):
    path = Path(plan_path)
    stage = path.parent
    write_json(stage / "status.json", {"phase": "queued"})
    run(
        [
            "systemd-run",
            "--quiet",
            "--no-block",
            "--collect",
            f"--unit={UNIT}",
            "--property=UMask=0077",
            "--property=TimeoutStopSec=300",
            "--",
            sys.executable,
            str(Path(__file__).resolve()),
            "_worker",
            str(path),
        ]
    )


def ssh_command(options, arguments, **kwargs):
    return run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=15",
            f"{options['user']}@{options['host']}",
            shlex.join([str(arg) for arg in arguments]),
        ],
        **kwargs,
    )


def preview(options, config, release, before):
    current = before["version"] or (
        "unknown (binary " + before["files"]["binary"][:12] + ")"
    )
    print(
        f"\nHost:       {options['user']}@{options['host']}\n"
        f"Service:    {config['service']}\n"
        f"Version:    {current} → {release['tag']}\n"
        f"Source:     {release['commit'][:12]}\n"
        f"Archive:    SHA-256 {release['archive_sha'][:16]}…\n"
        f"Database:   {len(before['migrations'])} → {len(release['migrations'])}\n"
        f"Backup:     application + config + database "
        f"({before['database_bytes'] // 1024**2} MiB database)\n"
        f"Keep:       one previous deployment at {config['backups']}/previous\n"
        "Downtime:   backup, restart and validation; incoming requests blocked\n"
        "On failure: restore the previous application and database before reopening traffic\n",
        flush=True,
    )


def client(options):
    config = configuration()
    with tempfile.TemporaryDirectory(prefix="cpr-upgrade-") as temporary:
        work = Path(temporary)
        release = resolve_release()
        payload = json.dumps({"config": config, "release": release})
        source = Path(__file__).read_bytes()
        before = json.loads(
            ssh_command(options, ["python3", "-", "_inspect", payload], input=source)
        )
        preview(options, config, release, before)
        if before["version"] == release["tag"]:
            require(
                before["migrations"] == release["migrations"],
                "Current version has unexpected migration state",
            )
            print("Already up to date. No restart needed.")
            return 0
        if options["check"]:
            print("Check complete. Release metadata and VPS verified; VPS unchanged.")
            return 0
        if not options["yes"]:
            require(
                sys.stdin.isatty(),
                "Confirmation needs a terminal; use check or yes=true",
            )
            if input("Proceed? [y/N] ").strip().lower() not in {"y", "yes"}:
                print("Cancelled.")
                return 0
        plan_path = work / "plan.json"
        write_json(plan_path, {"config": config, "release": release, "before": before})
        stage = (
            ssh_command(options, ["mktemp", "-d", "/var/tmp/cpr-upgrade-XXXXXXXX"])
            .decode()
            .strip()
        )
        require(
            re.fullmatch(r"/var/tmp/cpr-upgrade-[A-Za-z0-9]+", stage),
            "Unexpected remote staging path",
        )
        print(f"Remote status: {stage}/status.json\nWorker: {UNIT}.service", flush=True)
        host = options["host"]
        target = (
            f"{options['user']}@[{host}]"
            if ":" in host
            else f"{options['user']}@{host}"
        )
        run(
            [
                "scp",
                "-q",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ConnectTimeout=15",
                plan_path,
                Path(__file__).resolve(),
                f"{target}:{stage}/",
            ],
            timeout=1000,
        )
        ssh_command(
            options,
            [
                "python3",
                f"{stage}/native_vps_upgrade.py",
                "_launch",
                f"{stage}/plan.json",
            ],
        )
        last_phase = None
        messages = {
            "queued": "Starting the protected upgrade worker…",
            "checking-release": "Downloading and verifying the release on the VPS…",
            "preflight": "Rechecking the live deployment…",
            "backing-up": "Blocking requests and backing up the deployment…",
            "installing": "Installing the release…",
            "verifying": "Checking migrations and service health…",
            "reopening": "Checks passed; reopening requests…",
            "restoring": "Upgrade failed; restoring the previous deployment…",
            "succeeded": "Upgrade completed.",
            "restored": "Upgrade failed; previous deployment restored.",
            "already-current": "Already up to date. No restart needed.",
            "failed": "Upgrade stopped; application was not changed.",
            "blocked": "Automatic recovery needs attention; inspect the service before routing requests.",
        }
        while True:
            state = json.loads(ssh_command(options, ["cat", f"{stage}/status.json"]))
            if state["phase"] != last_phase:
                detail = ": " + state["reason"] if state.get("reason") else ""
                message = messages.get(state["phase"])
                if message is None:
                    message = state["phase"]
                print(message + detail, flush=True)
                last_phase = state["phase"]
            if last_phase in TERMINAL:
                for label in ("backup", "incomplete_backup"):
                    if state.get(label):
                        print(label.replace("_", " ").title() + ": " + state[label])
                if last_phase != "blocked":
                    try:
                        ssh_command(options, ["rm", "-rf", "--", stage])
                    except UpgradeError:
                        pass
                return 0 if last_phase in {"succeeded", "already-current"} else 1
            time.sleep(2)


def main(arguments):
    os.umask(0o077)
    if arguments and arguments[0] == "_inspect":
        payload = json.loads(arguments[1])
        print(json.dumps(inspect_vps(payload["config"], payload["release"])))
        return 0
    if arguments and arguments[0] in {"_launch", "_worker"}:
        (launch if arguments[0] == "_launch" else worker)(arguments[1])
        return 0
    options = parse_args(arguments)
    return client(options) if options else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (UpgradeError, OSError, ValueError, KeyError) as error:
        print(f"[native-vps-upgrade] {error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print(
            "\nClient stopped. A launched remote worker continues; inspect its printed status path.",
            file=sys.stderr,
        )
        sys.exit(130)
