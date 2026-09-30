import contextlib
import io
import json
import pathlib
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import native_vps_upgrade as upgrade


def migrations(*checksums, failed=()):
    return [
        {"version": version, "checksum": checksum, "success": version not in failed}
        for version, checksum in enumerate(checksums, 1)
    ]


class UpgradeContractTests(unittest.TestCase):
    def test_release_metadata_accepts_published_assets_without_plugins(self):
        repository = "example/gateway"
        tag = "v3.13.1"
        commit = "a" * 40
        asset = "codex-proxy-rs_3.13.1_linux_amd64.tar.gz"
        checksum = "b" * 64
        responses = {
            f"https://api.github.com/repos/{repository}/releases/latest": json.dumps(
                {
                    "tag_name": tag,
                    "draft": False,
                    "prerelease": False,
                    "target_commitish": "main",
                }
            ).encode(),
            f"https://github.com/{repository}/releases/download/{tag}/checksums.txt": f"{checksum}  {asset}\n".encode(),
            f"https://api.github.com/repos/{repository}/commits/{tag}": json.dumps(
                {"sha": commit}
            ).encode(),
        }
        with (
            patch.dict("os.environ", {"CPR_UPDATE_REPOSITORY": repository}),
            patch.object(upgrade, "download", side_effect=responses.__getitem__),
            patch.object(
                upgrade, "migration_contract", return_value=migrations("a")
            ) as contract,
        ):
            release = upgrade.resolve_release()
        self.assertEqual(release["archive_sha"], checksum)
        self.assertEqual(release["commit"], commit)
        contract.assert_called_once_with(repository, commit)

    def test_version_probe_failure_returns_unknown_for_preview(self):
        for error in (
            upgrade.UpgradeError("unsupported --version"),
            upgrade.UpgradeError("timeout"),
        ):
            with (
                self.subTest(error=str(error)),
                patch.object(upgrade, "run", side_effect=error),
            ):
                self.assertIsNone(
                    upgrade.installed_version("/app/binary", "app", "v3.13.1")
                )

    def test_unparseable_version_returns_unknown_for_preview(self):
        for output in (b"", b"unknown", b"3.14.0-rc.1", b"\xff"):
            with (
                self.subTest(output=output),
                patch.object(upgrade, "run", return_value=output),
            ):
                self.assertIsNone(
                    upgrade.installed_version("/app/binary", "app", "v3.13.1")
                )

    def test_known_versions_enforce_no_downgrade(self):
        for output in (b"3.13.0\n", b"v3.13.1"):
            with patch.object(upgrade, "run", return_value=output):
                self.assertEqual(
                    upgrade.installed_version("/app/binary", "app", "v3.13.1"),
                    "v" + output.decode().strip().removeprefix("v"),
                )
        with patch.object(upgrade, "run", return_value=b"3.14.0"):
            with self.assertRaisesRegex(upgrade.UpgradeError, "downgrade refused"):
                upgrade.installed_version("/app/binary", "app", "v3.13.1")

    def test_unknown_version_requires_database_to_be_behind_target(self):
        upgrade.validate_version_state(
            None, migrations("a", "b"), migrations("a", "b", "c")
        )
        with self.assertRaisesRegex(upgrade.UpgradeError, "downgrade refused"):
            upgrade.validate_version_state(
                None, migrations("a", "b"), migrations("a", "b")
            )

    def test_ssh_error_reports_remote_preflight_reason(self):
        error = subprocess.CalledProcessError(
            1,
            ["ssh"],
            stderr=b"[native-vps-upgrade] Cannot read installed version; upgrade refused\n",
        )
        with patch("subprocess.run", side_effect=error):
            with self.assertRaisesRegex(
                upgrade.UpgradeError,
                "Remote command failed: Cannot read installed version; upgrade refused",
            ):
                upgrade.run(["ssh", "root@example", "python3 -"])

    def test_latest_stable_version_accepts_build_metadata_but_not_prereleases(self):
        self.assertEqual(upgrade.stable_version("v3.17.0+build.1"), (3, 17, 0))
        self.assertIsNone(upgrade.stable_version("v3.17.0-rc.1"))

    def test_host_must_be_explicit_even_when_old_environment_variable_is_set(self):
        with patch.dict("os.environ", {"CPR_VPS_HOST": "old-default.invalid"}):
            with self.assertRaisesRegex(upgrade.UpgradeError, "host= is required"):
                upgrade.parse_args(["upgrade", "check"])
        options = upgrade.parse_args(["upgrade", "check", "host=vps.example"])
        if options is None:
            raise AssertionError("valid upgrade options were not parsed")
        self.assertEqual(options["host"], "vps.example")
        self.assertTrue(options["check"])

    def test_migrations_must_match_release_checksums_and_be_a_successful_prefix(self):
        target = migrations("a", "b", "c")
        upgrade.migration_prefix(migrations("a", "b"), target)
        with self.assertRaisesRegex(upgrade.UpgradeError, "checksums differ"):
            upgrade.migration_prefix(migrations("a", "changed"), target)
        with self.assertRaisesRegex(upgrade.UpgradeError, "failed migrations"):
            upgrade.migration_prefix(migrations("a", "b", failed=(2,)), target)
        with self.assertRaisesRegex(upgrade.UpgradeError, "newer than"):
            upgrade.migration_prefix(migrations("a", "b", "c", "d"), target)

    def test_release_extraction_rejects_symlinks_before_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            archive = root / "bad.tar.gz"
            import tarfile

            with tarfile.open(archive, "w:gz") as bundle:
                link = tarfile.TarInfo("codex-proxy-rs/web/dist/link")
                link.type = tarfile.SYMTYPE
                link.linkname = "/etc/passwd"
                bundle.addfile(link)
            with self.assertRaisesRegex(upgrade.UpgradeError, "link or special"):
                upgrade.extract_release(archive, root / "out")
            self.assertFalse((root / "out").exists())

    def test_release_extraction_accepts_published_layout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            archive = root / "release.tar.gz"
            entries = [
                ("codex-proxy-rs", tarfile.DIRTYPE, 0o755, b""),
                ("codex-proxy-rs/web", tarfile.DIRTYPE, 0o755, b""),
                ("codex-proxy-rs/web/dist", tarfile.DIRTYPE, 0o755, b""),
                ("codex-proxy-rs/deploy", tarfile.DIRTYPE, 0o755, b""),
                (
                    "codex-proxy-rs/deploy/config.example.yaml",
                    tarfile.REGTYPE,
                    0o644,
                    b"config",
                ),
                ("codex-proxy-rs/web/dist/index.html", tarfile.REGTYPE, 0o644, b"ok"),
                ("codex-proxy-rs/codex-proxy-rs", tarfile.REGTYPE, 0o755, b"binary"),
            ]
            with tarfile.open(archive, "w:gz") as bundle:
                for name, kind, mode, content in entries:
                    item = tarfile.TarInfo(name)
                    item.type = kind
                    item.mode = mode
                    item.size = len(content)
                    bundle.addfile(item, io.BytesIO(content) if content else None)

            extracted = upgrade.extract_release(archive, root / "out")
            self.assertEqual(extracted, root / "out/codex-proxy-rs")
            self.assertEqual(
                (extracted / "codex-proxy-rs").stat().st_mode & 0o777, 0o755
            )
            self.assertEqual((extracted / "web/dist/index.html").read_bytes(), b"ok")


class FakeDeployment(upgrade.Deployment):
    def __init__(self, stage, failure=None):
        target = migrations("new")
        before = {
            "files": {
                "binary": "old-bin",
                "web": "old-web",
                "config": "config",
            },
            "migrations": migrations("old"),
            "config_path": "/srv/deploy/config.yaml",
        }
        release = {
            "tag": "v2.0.0",
            "files": {
                "binary": "new-bin",
                "web": "new-web",
            },
            "migrations": target,
        }
        super().__init__(
            {
                "before": before,
                "release": release,
                "config": {
                    "backups": str(stage / "backups"),
                    "service": "codex-proxy-rs.service",
                    "binary": "/app/codex-proxy-rs",
                    "web": "/app/web/dist",
                    "port": 8080,
                },
            },
            stage,
        )
        self.events = []
        self.gated = False
        self.failure = failure
        self.service = "old"
        self.snapshot.parent.mkdir()

    def status(self, phase, **details):
        self.events.append("status:" + phase)

    def gate_on(self):
        self.gated = True
        self.events.append("gate-on")

    def gate_off(self):
        self.gated = False
        self.events.append("gate-off")

    def stop(self):
        self.events.append("stop")
        self.service = "stopped"

    def start(self):
        self.events.append("start")
        self.service = "new" if "install" in self.events else "old"

    def backup(self):
        self.events.append("backup")
        if self.failure == "backup":
            raise upgrade.UpgradeError("backup failed")
        self.snapshot.mkdir()

    def install(self):
        self.events.append("install")

    def restore(self):
        self.events.append("restore")

    def verify(self, expected_files, expected_migrations):
        self.assert_gate_during_verify()
        expected = "new" if expected_files["binary"] == "new-bin" else "old"
        self.events.append("verify-" + expected)
        if expected == "new" and self.failure == "startup":
            raise upgrade.UpgradeError("new service failed health verification")

    def assert_gate_during_verify(self):
        if not self.gated:
            raise AssertionError("health verification must run behind the traffic gate")

    def retain(self):
        self.events.append("retain")
        self.snapshot = self.snapshot.parent / "previous"


class UpgradeRecoveryTests(unittest.TestCase):
    def run_fake(self, fake, inspection=None):
        if inspection is None:
            inspection = fake.before
        checks = [
            patch.object(upgrade, "inspect_vps", return_value=inspection),
            patch.object(upgrade, "gate_present", side_effect=lambda: fake.gated),
            patch.object(
                upgrade,
                "health",
                side_effect=lambda config, mark=0, path="/healthz": bool(mark)
                or not fake.gated,
            ),
        ]
        with contextlib.ExitStack() as stack:
            for check in checks:
                stack.enter_context(check)
            fake.execute()

    def test_upgrade_failure_restores_database_and_files_before_opening_traffic(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = FakeDeployment(pathlib.Path(temp), failure="startup")
            self.run_fake(fake)
            self.assertLess(fake.events.index("gate-on"), fake.events.index("backup"))
            self.assertLess(fake.events.index("backup"), fake.events.index("install"))
            self.assertLess(fake.events.index("install"), fake.events.index("restore"))
            self.assertLess(
                fake.events.index("restore"), fake.events.index("verify-old")
            )
            self.assertLess(
                fake.events.index("verify-old"), fake.events.index("gate-off")
            )
            self.assertEqual(fake.events[-1], "status:restored")

    def test_backup_failure_restarts_old_service_without_installing_or_restoring(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = FakeDeployment(pathlib.Path(temp), failure="backup")
            self.run_fake(fake)
            self.assertNotIn("install", fake.events)
            self.assertNotIn("restore", fake.events)
            self.assertEqual(fake.events[-1], "status:restored")
            self.assertEqual(fake.service, "old")

    def test_changed_deployment_aborts_before_enabling_maintenance_gate(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = FakeDeployment(pathlib.Path(temp))
            changed = {
                **fake.before,
                "files": {**fake.before["files"], "binary": "other"},
            }
            self.run_fake(fake, inspection=changed)
            self.assertNotIn("gate-on", fake.events)
            self.assertNotIn("stop", fake.events)
            self.assertEqual(fake.events[-1], "status:failed")


if __name__ == "__main__":
    unittest.main()
