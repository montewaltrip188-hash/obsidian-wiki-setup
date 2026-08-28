import json
import hashlib
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).absolute().parents[1]
CLI = ROOT / "tools" / "manage_wiki_skills.py"
CONTRACT = ROOT / "contracts" / "wiki-skill-lifecycle.json"
CORE = ("design-juan-wiki", "wiki-hybrid-search", "ocr-and-documents")


class WikiSkillLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.source = self.root / "source"
        self._make_source("2.0.1")

    def tearDown(self):
        self.temp.cleanup()

    def _make_source(self, version):
        self.source.mkdir(parents=True, exist_ok=True)
        (self.source / "VERSION").write_text(version + "\n", encoding="utf-8")
        (self.source / "references").mkdir(exist_ok=True)
        (self.source / "references" / "platform-commands.md").write_text(
            "shared reference\n", encoding="utf-8"
        )
        for name in CORE:
            skill = self.source / "core" / name
            skill.mkdir(parents=True, exist_ok=True)
            (skill / "SKILL.md").write_text(
                f"---\nname: {name}\n---\n../../references/platform-commands.md\n",
                encoding="utf-8",
            )
        search = self.source / "core" / "wiki-hybrid-search" / "scripts"
        search.mkdir(parents=True, exist_ok=True)
        (search / "wiki_search.py").write_text("print('keyword-ready')\n", encoding="utf-8")
        ima = self.source / "external" / "ima-skill"
        ima.mkdir(parents=True, exist_ok=True)
        (ima / "SKILL.md").write_text("---\nname: ima-skill\n---\n", encoding="utf-8")

    def run_cli(self, *args, expected=0):
        result = subprocess.run(
            [sys.executable, str(CLI), *map(str, args)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        self.assertEqual(expected, result.returncode, result.stderr or result.stdout)
        stream = result.stdout if expected == 0 else result.stderr
        return json.loads(stream)

    def start_lock_held_install(self):
        held = self.root / ("lock-held-" + next(tempfile._get_candidate_names()))
        release = self.root / ("lock-release-" + next(tempfile._get_candidate_names()))
        process = subprocess.Popen(
            [
                sys.executable,
                str(CLI),
                "install",
                "--source",
                str(self.source),
                "--home",
                str(self.home),
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            env={
                **os.environ,
                "PYTHONUTF8": "1",
                "WIKI_SKILL_TEST_LOCK_HELD_FILE": str(held),
                "WIKI_SKILL_TEST_LOCK_RELEASE_FILE": str(release),
            },
        )
        deadline = time.monotonic() + 5
        while not held.exists():
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(f"锁持有进程提前退出：{stdout} {stderr}")
            if time.monotonic() >= deadline:
                process.kill()
                process.communicate()
                self.fail("锁持有钩子未在时限内发出同步信号")
            time.sleep(0.01)
        return process, release

    def run_concurrent_installs(self):
        winner, release = self.start_lock_held_install()
        blocked = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "install",
                "--source",
                str(self.source),
                "--home",
                str(self.home),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        release.write_text("release\n", encoding="utf-8")
        winner_output = winner.communicate(timeout=60)
        return [
            winner_output + (winner.returncode,),
            (blocked.stdout, blocked.stderr, blocked.returncode),
        ]

    def alias_claude_root_to_codex_root(self):
        codex_root = self.home / ".agents" / "skills"
        claude_root = self.home / ".claude" / "skills"
        codex_root.mkdir(parents=True)
        claude_root.parent.mkdir(parents=True)
        if os.name == "nt":
            result = subprocess.run(
                ["cmd", "/d", "/c", "mklink", "/J", str(claude_root), str(codex_root)],
                capture_output=True,
            )
            self.assertEqual(0, result.returncode, result.stderr or result.stdout)
        else:
            os.symlink(codex_root, claude_root, target_is_directory=True)

    def snapshot_home(self):
        if not self.home.exists():
            return None
        home_stat = os.lstat(self.home)
        snapshot = {
            ".": {
                "mode": home_stat.st_mode,
                "size": home_stat.st_size,
                "mtime_ns": home_stat.st_mtime_ns,
            }
        }
        for current, directories, files in os.walk(self.home, followlinks=False):
            current_path = Path(current)
            for name in sorted([*directories, *files]):
                path = current_path / name
                stat = os.lstat(path)
                relative = path.relative_to(self.home).as_posix()
                record = {
                    "mode": stat.st_mode,
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
                if path.is_file() and not path.is_symlink():
                    record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                snapshot[relative] = record
        return snapshot

    def downgrade_managed_state_to_legacy_v1(self):
        state_path = (
            self.home / ".agents" / "packages" / "claudecode-wiki-skills" / "state.json"
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["schema_version"] = 1
        state.pop("install_generation", None)
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return state_path

    def test_plan_is_read_only_and_defaults_to_three_core_skills(self):
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))

        plan = self.run_cli("plan", "--source", self.source, "--home", self.home)

        after = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        self.assertEqual(before, after)
        self.assertEqual("2.0.1", plan["version"])
        self.assertEqual(list(CORE), plan["skills"])
        self.assertFalse(plan["include_ima"])
        self.assertEqual("keyword", plan["offline_baseline"])
        self.assertEqual("optional", plan["vector_capability"])
        self.assertFalse(plan["skill_installed"])
        self.assertFalse(plan["keyword_runtime_ready"])
        self.assertEqual("KEYWORD_RUNTIME_UNPROVISIONED", plan["keyword_runtime_error"])
        self.assertEqual("install", plan["action"])
        self.assertEqual("ready", plan["status"])

    def test_plan_blocks_unknown_same_name_entry_without_writing_home(self):
        collision = self.home / ".agents" / "skills" / "design-juan-wiki"
        collision.mkdir(parents=True)
        (collision / "owner.txt").write_text("unknown\n", encoding="utf-8")
        before = self.snapshot_home()

        blocked = self.run_cli(
            "plan", "--source", self.source, "--home", self.home, expected=2
        )

        self.assertEqual("blocked", blocked["status"])
        self.assertIn("拒绝覆盖", blocked["error"])
        self.assertEqual(before, self.snapshot_home())

    def test_plan_blocks_unowned_package_directory_without_writing_home(self):
        orphan = (
            self.home
            / ".agents"
            / "packages"
            / "claudecode-wiki-skills"
            / "orphan.txt"
        )
        orphan.parent.mkdir(parents=True)
        orphan.write_text("unknown\n", encoding="utf-8")
        before = self.snapshot_home()

        blocked = self.run_cli(
            "plan", "--source", self.source, "--home", self.home, expected=2
        )

        self.assertEqual("blocked", blocked["status"])
        self.assertIn("无状态包目录", blocked["error"])
        self.assertEqual(before, self.snapshot_home())

    def test_plan_reports_managed_upgrade_without_writing_home(self):
        self.run_cli("install", "--source", self.source, "--home", self.home)
        self._make_source("2.0.2")
        before = self.snapshot_home()

        plan = self.run_cli("plan", "--source", self.source, "--home", self.home)

        self.assertEqual("upgrade", plan["action"])
        self.assertEqual("2.0.1", plan["active_version"])
        self.assertEqual(before, self.snapshot_home())

    def test_plan_reports_already_installed_and_blocks_state_drift_read_only(self):
        self.run_cli("install", "--source", self.source, "--home", self.home)
        before = self.snapshot_home()
        plan = self.run_cli("plan", "--source", self.source, "--home", self.home)
        self.assertEqual("already_installed", plan["action"])
        self.assertEqual(before, self.snapshot_home())

        state_path = (
            self.home / ".agents" / "packages" / "claudecode-wiki-skills" / "state.json"
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["active_version"] = "9.9.9"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        drifted = self.snapshot_home()
        blocked = self.run_cli(
            "plan", "--source", self.source, "--home", self.home, expected=2
        )
        self.assertEqual("blocked", blocked["status"])
        self.assertEqual(drifted, self.snapshot_home())

    def test_legacy_v1_plan_and_verify_are_read_only(self):
        self.run_cli("install", "--source", self.source, "--home", self.home)
        state_path = self.downgrade_managed_state_to_legacy_v1()
        legacy_bytes = state_path.read_bytes()
        before = self.snapshot_home()

        plan = self.run_cli("plan", "--source", self.source, "--home", self.home)
        verified = self.run_cli("verify", "--home", self.home)

        self.assertEqual("already_installed", plan["action"])
        self.assertEqual("verified", verified["status"])
        self.assertEqual(1, verified["state_schema_version"])
        self.assertEqual(before, self.snapshot_home())
        self.assertEqual(legacy_bytes, state_path.read_bytes())

    def test_transitional_v1_with_valid_generation_is_read_only_compatible_and_migrates(self):
        self.run_cli("install", "--source", self.source, "--home", self.home)
        state_path = (
            self.home / ".agents" / "packages" / "claudecode-wiki-skills" / "state.json"
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["schema_version"] = 1
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        before = self.snapshot_home()

        plan = self.run_cli(
            "plan", "--source", self.source, "--home", self.home
        )
        verified = self.run_cli("verify", "--home", self.home)

        self.assertEqual("already_installed", plan["action"])
        self.assertEqual(1, verified["state_schema_version"])
        self.assertEqual(before, self.snapshot_home())

        migrated = self.run_cli(
            "install", "--source", self.source, "--home", self.home
        )
        current = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual("migrated", migrated["status"])
        self.assertEqual(2, current["schema_version"])
        self.assertEqual(migrated["transaction_id"], current["install_generation"])

        undone = self.run_cli(
            "undo",
            "--home",
            self.home,
            "--receipt",
            migrated["undo_receipt"],
        )
        restored = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual("undone", undone["status"])
        self.assertEqual(state, restored)
        self.assertEqual(1, self.run_cli("verify", "--home", self.home)["state_schema_version"])

    def test_v1_with_invalid_generation_is_rejected_without_writes(self):
        self.run_cli("install", "--source", self.source, "--home", self.home)
        state_path = (
            self.home / ".agents" / "packages" / "claudecode-wiki-skills" / "state.json"
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["schema_version"] = 1
        state["install_generation"] = "not-a-valid-transaction-id"
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        before = self.snapshot_home()

        blocked = self.run_cli(
            "plan", "--source", self.source, "--home", self.home, expected=2
        )

        self.assertIn("generation", blocked["error"])
        self.assertEqual(before, self.snapshot_home())

    def test_machine_readable_contract_freezes_public_seams_and_manual_gates(self):
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        self.assertEqual(
            ["plan", "install", "verify", "rollback", "uninstall", "undo", "undo-check"],
            contract["commands"],
        )
        self.assertEqual(list(CORE), contract["defaults"]["skills"])
        self.assertEqual("explicit_opt_in", contract["defaults"]["ima_skill"])
        self.assertEqual("keyword", contract["defaults"]["offline_baseline"])
        self.assertIn("unknown_same_name_entry", contract["fail_closed_on"])
        self.assertIn("fingerprint_drift", contract["fail_closed_on"])
        self.assertEqual("logical_runtime_to_owned_root", contract["ownership"]["runtime_aliases"])
        self.assertTrue(contract["plan"]["strictly_read_only"])
        self.assertEqual(
            ["install", "upgrade", "already_installed"], contract["plan"]["actions"]
        )
        self.assertEqual(
            ["install", "rollback", "uninstall", "undo"],
            contract["transaction_lock"]["commands"],
        )
        self.assertEqual("kernel_file_lock", contract["transaction_lock"]["lease"])
        self.assertEqual("automatic_on_process_exit", contract["transaction_lock"]["stale_recovery"])
        self.assertEqual(
            ["plan", "verify", "undo-check"],
            contract["transaction_lock"]["read_only_commands"],
        )
        self.assertIn("concurrent_mutation", contract["fail_closed_on"])
        self.assertIn("after_state_drift", contract["fail_closed_on"])
        self.assertEqual(
            "install_output.undo_receipt", contract["undo"]["receipt_source"]
        )
        self.assertEqual(
            [
                "transaction_id",
                "home",
                "changed",
                "before",
                "after",
                "package_fingerprint",
                "entries_fingerprint",
                "generation",
            ],
            contract["undo"]["bindings"],
        )
        self.assertEqual("no_op", contract["undo"]["unchanged_transaction"])
        self.assertEqual(
            "must_equal_receipt_after_state",
            contract["undo"]["current_state_requirement"],
        )
        self.assertEqual(
            "persistent_transaction_id",
            contract["ownership"]["install_generation"],
        )
        self.assertEqual(
            "best_effort_result_warning",
            contract["transaction_lock"]["release_audit_failure"],
        )
        self.assertEqual(
            "overwrite_only_after_kernel_lock_acquired",
            contract["transaction_lock"]["corrupt_stale_audit_recovery"],
        )
        self.assertEqual(2, contract["ownership"]["state_schema_version"])
        self.assertEqual(
            "read_only_compatible_with_optional_valid_generation",
            contract["ownership"]["legacy_schema_v1"],
        )
        self.assertEqual(
            "first_locked_install_transaction",
            contract["ownership"]["legacy_migration"],
        )

    def test_unsafe_version_cannot_escape_version_directory(self):
        (self.source / "VERSION").write_text("..\n", encoding="utf-8")

        blocked = self.run_cli(
            "install", "--source", self.source, "--home", self.home, expected=2
        )

        self.assertEqual("blocked", blocked["status"])
        self.assertIn("VERSION", blocked["error"])
        self.assertFalse(self.home.exists())

    @unittest.skipUnless(os.name == "nt", "Windows 路径预算合同")
    def test_install_fails_before_writes_when_windows_target_path_is_too_long(self):
        long_home = self.root / ("h" * 190)

        blocked = self.run_cli(
            "install", "--source", self.source, "--home", long_home, expected=2
        )

        self.assertEqual("blocked", blocked["status"])
        self.assertIn("路径过长", blocked["error"])
        self.assertFalse(long_home.exists())

    def test_install_preserves_repository_tree_and_adds_owned_runtime_entries(self):
        receipt = self.run_cli("install", "--source", self.source, "--home", self.home)

        package = self.home / ".agents" / "packages" / "claudecode-wiki-skills"
        installed = package / "versions" / "2.0.1"
        self.assertEqual("shared reference\n", (installed / "references" / "platform-commands.md").read_text(encoding="utf-8"))
        self.assertTrue((installed / "core" / "wiki-hybrid-search" / "scripts" / "wiki_search.py").is_file())
        self.assertFalse((self.home / ".agents" / "skills" / "ima-skill").exists())
        for runtime in (self.home / ".agents" / "skills", self.home / ".claude" / "skills"):
            for name in CORE:
                entry = runtime / name
                self.assertTrue((entry / "SKILL.md").is_file())
        state = json.loads((package / "state.json").read_text(encoding="utf-8"))
        manifest = json.loads((installed / ".wiki-skill-install.json").read_text(encoding="utf-8"))
        self.assertEqual("2.0.1", state["active_version"])
        self.assertEqual(list(CORE), state["owned_skills"])
        self.assertEqual("keyword", manifest["capabilities"]["offline_baseline"])
        self.assertTrue(receipt["skill_installed"])
        self.assertFalse(receipt["keyword_runtime_ready"])
        self.assertEqual("KEYWORD_RUNTIME_UNPROVISIONED", receipt["keyword_runtime_error"])
        self.assertGreater(len(manifest["files"]), 6)
        self.assertEqual("installed", receipt["status"])

    def test_install_receipt_can_undo_a_fresh_install(self):
        installed = self.run_cli(
            "install", "--source", self.source, "--home", self.home
        )

        self.assertTrue(installed["changed"])
        self.assertEqual("install", installed["action"])
        self.assertRegex(installed["transaction_id"], r"^[0-9a-f]{32}$")
        undo_receipt = Path(installed["undo_receipt"])
        self.assertTrue(undo_receipt.is_file())
        payload = json.loads(undo_receipt.read_text(encoding="utf-8"))
        self.assertFalse(payload["before"]["installed"])
        self.assertTrue(payload["after"]["installed"])
        self.assertEqual("2.0.1", payload["after"]["active_version"])
        self.assertIn("package_fingerprint", payload["after"])
        self.assertIn("entries_fingerprint", payload["after"])

        undone = self.run_cli(
            "undo", "--home", self.home, "--receipt", undo_receipt
        )

        self.assertEqual("undone", undone["status"])
        self.assertEqual(installed["transaction_id"], undone["transaction_id"])
        self.assertFalse(
            (
                self.home
                / ".agents"
                / "packages"
                / "claudecode-wiki-skills"
            ).exists()
        )
        for runtime in (
            self.home / ".agents" / "skills",
            self.home / ".claude" / "skills",
        ):
            for name in CORE:
                self.assertFalse((runtime / name).exists())

    def test_undo_check_is_read_only_and_detects_after_state_drift(self):
        installed = self.run_cli(
            "install", "--source", self.source, "--home", self.home
        )
        receipt = Path(installed["undo_receipt"])
        before = self.snapshot_home()

        ready = self.run_cli(
            "undo-check", "--home", self.home, "--receipt", receipt
        )

        self.assertEqual("undo_ready", ready["status"])
        self.assertTrue(ready["changed"])
        self.assertEqual(installed["transaction_id"], ready["transaction_id"])
        self.assertEqual(before, self.snapshot_home())

        skill_md = self.home / ".agents" / "skills" / "design-juan-wiki" / "SKILL.md"
        skill_md.write_text(skill_md.read_text(encoding="utf-8") + "drift\n", encoding="utf-8")
        drifted = self.snapshot_home()
        blocked = self.run_cli(
            "undo-check", "--home", self.home, "--receipt", receipt, expected=2
        )
        self.assertEqual("UNDO_AFTER_STATE_DRIFT", blocked["error"])
        self.assertEqual(drifted, self.snapshot_home())

    def test_consumed_receipt_cannot_remove_later_identical_install(self):
        first = self.run_cli(
            "install", "--source", self.source, "--home", self.home
        )
        self.run_cli(
            "undo", "--home", self.home, "--receipt", first["undo_receipt"]
        )
        second = self.run_cli(
            "install", "--source", self.source, "--home", self.home
        )
        self.assertNotEqual(first["transaction_id"], second["transaction_id"])

        replay = self.run_cli(
            "undo",
            "--home",
            self.home,
            "--receipt",
            first["undo_receipt"],
            expected=2,
        )

        self.assertIn("UNDO_AFTER_STATE_DRIFT", replay["error"])
        verified = self.run_cli("verify", "--home", self.home)
        self.assertEqual("verified", verified["status"])
        state = json.loads(
            (
                self.home
                / ".agents"
                / "packages"
                / "claudecode-wiki-skills"
                / "state.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(second["transaction_id"], state["install_generation"])

    def test_lock_release_audit_failure_keeps_success_result_and_unlocks(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "install",
                "--source",
                str(self.source),
                "--home",
                str(self.home),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            encoding="utf-8",
            env={
                **os.environ,
                "PYTHONUTF8": "1",
                "WIKI_SKILL_TEST_FORCE_LOCK_RELEASE_AUDIT_FAILURE": "1",
            },
        )

        self.assertEqual(0, completed.returncode, completed.stderr or completed.stdout)
        result = json.loads(completed.stdout)
        self.assertEqual("installed", result["status"])
        self.assertEqual(
            "LOCK_RELEASE_AUDIT_WRITE_FAILED", result["lock_audit_error"]
        )
        self.assertTrue(Path(result["undo_receipt"]).is_file())
        self.assertEqual(
            "verified", self.run_cli("verify", "--home", self.home)["status"]
        )
        follower = self.run_cli(
            "install", "--source", self.source, "--home", self.home
        )
        self.assertEqual("already_installed", follower["status"])

    def test_partial_release_audit_write_is_recovered_only_after_follower_gets_lock(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "install",
                "--source",
                str(self.source),
                "--home",
                str(self.home),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            encoding="utf-8",
            env={
                **os.environ,
                "PYTHONUTF8": "1",
                "WIKI_SKILL_TEST_FORCE_LOCK_RELEASE_AUDIT_PARTIAL_WRITE": "1",
            },
        )
        self.assertEqual(0, completed.returncode, completed.stderr or completed.stdout)
        installed = json.loads(completed.stdout)
        self.assertEqual(
            "LOCK_RELEASE_AUDIT_WRITE_FAILED", installed["lock_audit_error"]
        )

        follower = self.run_cli(
            "install", "--source", self.source, "--home", self.home
        )
        self.assertEqual("already_installed", follower["status"])
        self.assertEqual(
            "verified", self.run_cli("verify", "--home", self.home)["status"]
        )
        undone = self.run_cli(
            "undo",
            "--home",
            self.home,
            "--receipt",
            installed["undo_receipt"],
        )
        self.assertEqual("undone", undone["status"])

    def test_legacy_v1_install_migrates_to_v2_and_receipt_undo_restores_v1(self):
        self.run_cli("install", "--source", self.source, "--home", self.home)
        state_path = self.downgrade_managed_state_to_legacy_v1()
        legacy_state = json.loads(state_path.read_text(encoding="utf-8"))

        migrated = self.run_cli(
            "install", "--source", self.source, "--home", self.home
        )

        self.assertEqual("migrated", migrated["status"])
        self.assertEqual("migrate", migrated["action"])
        self.assertTrue(migrated["changed"])
        current = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(2, current["schema_version"])
        self.assertEqual(migrated["transaction_id"], current["install_generation"])

        undone = self.run_cli(
            "undo",
            "--home",
            self.home,
            "--receipt",
            migrated["undo_receipt"],
        )

        self.assertEqual("undone", undone["status"])
        self.assertEqual(legacy_state, json.loads(state_path.read_text(encoding="utf-8")))
        verified = self.run_cli("verify", "--home", self.home)
        self.assertEqual(1, verified["state_schema_version"])

    def test_receipt_write_failure_reverts_install_in_same_transaction(self):
        receipt_parent = self.home / ".agents" / "receipts"
        receipt_parent.mkdir(parents=True)
        (receipt_parent / "claudecode-wiki-skills").write_text(
            "blocks receipt directory\n", encoding="utf-8"
        )

        blocked = self.run_cli(
            "install",
            "--source",
            self.source,
            "--home",
            self.home,
            expected=2,
        )

        self.assertEqual("OPERATION_FAILED", blocked["error"])
        self.assertFalse(
            (
                self.home
                / ".agents"
                / "packages"
                / "claudecode-wiki-skills"
            ).exists()
        )
        for runtime in (
            self.home / ".agents" / "skills",
            self.home / ".claude" / "skills",
        ):
            for name in CORE:
                self.assertFalse((runtime / name).exists())

    def test_install_self_verify_failure_reverts_in_same_transaction(self):
        result = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "install",
                "--source",
                str(self.source),
                "--home",
                str(self.home),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            encoding="utf-8",
            env={
                **os.environ,
                "PYTHONUTF8": "1",
                "WIKI_SKILL_TEST_FORCE_VERIFY_FAILURE": "1",
            },
        )

        self.assertEqual(2, result.returncode, result.stdout)
        self.assertIn("TEST_FORCED_VERIFY_FAILURE", result.stderr)
        self.assertFalse(
            (
                self.home
                / ".agents"
                / "packages"
                / "claudecode-wiki-skills"
            ).exists()
        )
        for runtime in (
            self.home / ".agents" / "skills",
            self.home / ".claude" / "skills",
        ):
            for name in CORE:
                self.assertFalse((runtime / name).exists())

    def test_undo_fails_closed_when_package_drifted_after_install(self):
        installed = self.run_cli(
            "install", "--source", self.source, "--home", self.home
        )
        package_file = (
            self.home
            / ".agents"
            / "packages"
            / "claudecode-wiki-skills"
            / "versions"
            / "2.0.1"
            / "core"
            / "design-juan-wiki"
            / "SKILL.md"
        )
        package_file.write_text("drifted after install\n", encoding="utf-8")

        blocked = self.run_cli(
            "undo",
            "--home",
            self.home,
            "--receipt",
            installed["undo_receipt"],
            expected=2,
        )

        self.assertIn("UNDO_AFTER_STATE_DRIFT", blocked["error"])
        self.assertEqual(
            "drifted after install\n", package_file.read_text(encoding="utf-8")
        )

    def test_undo_fails_closed_when_entry_replaced_after_install(self):
        installed = self.run_cli(
            "install", "--source", self.source, "--home", self.home
        )
        entry = self.home / ".agents" / "skills" / "design-juan-wiki"
        if os.name == "nt":
            os.rmdir(entry)
        else:
            entry.unlink()
        entry.mkdir()
        replacement = entry / "SKILL.md"
        replacement.write_text("another transaction\n", encoding="utf-8")

        blocked = self.run_cli(
            "undo",
            "--home",
            self.home,
            "--receipt",
            installed["undo_receipt"],
            expected=2,
        )

        self.assertIn("UNDO_AFTER_STATE_DRIFT", blocked["error"])
        self.assertEqual(
            "another transaction\n", replacement.read_text(encoding="utf-8")
        )

    def test_setup_style_verify_failure_uses_install_receipt_to_restore(self):
        installed = self.run_cli(
            "install", "--source", self.source, "--home", self.home
        )
        verification = subprocess.run(
            [sys.executable, str(CLI), "verify", "--home", str(self.home)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            encoding="utf-8",
            env={
                **os.environ,
                "PYTHONUTF8": "1",
                "WIKI_SKILL_TEST_FORCE_VERIFY_FAILURE": "1",
            },
        )
        self.assertEqual(2, verification.returncode, verification.stdout)
        self.assertIn("TEST_FORCED_VERIFY_FAILURE", verification.stderr)

        restored = self.run_cli(
            "undo",
            "--home",
            self.home,
            "--receipt",
            installed["undo_receipt"],
        )

        self.assertEqual("undone", restored["status"])
        self.assertFalse(
            (
                self.home
                / ".agents"
                / "packages"
                / "claudecode-wiki-skills"
            ).exists()
        )

    def test_persistent_public_verify_failure_does_not_block_receipt_bound_undo(self):
        installed = self.run_cli(
            "install", "--source", self.source, "--home", self.home
        )
        persistent_failure_env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "WIKI_SKILL_TEST_FORCE_VERIFY_FAILURE": "1",
        }
        verification = subprocess.run(
            [sys.executable, str(CLI), "verify", "--home", str(self.home)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            encoding="utf-8",
            env=persistent_failure_env,
        )
        self.assertEqual(2, verification.returncode, verification.stdout)
        self.assertIn("TEST_FORCED_VERIFY_FAILURE", verification.stderr)

        undone = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "undo",
                "--home",
                str(self.home),
                "--receipt",
                installed["undo_receipt"],
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            encoding="utf-8",
            env=persistent_failure_env,
        )

        self.assertEqual(0, undone.returncode, undone.stderr or undone.stdout)
        self.assertEqual("undone", json.loads(undone.stdout)["status"])
        self.assertFalse(
            (
                self.home
                / ".agents"
                / "packages"
                / "claudecode-wiki-skills"
            ).exists()
        )

    def test_concurrent_install_on_same_home_allows_one_transaction_and_preserves_winner(self):
        results = self.run_concurrent_installs()

        self.assertEqual([0, 2], sorted(result[2] for result in results), results)
        blocked = next(result for result in results if result[2] == 2)
        blocked_receipt = json.loads(blocked[1])
        self.assertEqual("blocked", blocked_receipt["status"])
        self.assertEqual("INSTALL_TRANSACTION_LOCKED", blocked_receipt["error"])
        self.assertEqual("verified", self.run_cli("verify", "--home", self.home)["status"])

    def test_stale_plan_noop_undo_does_not_remove_concurrent_winner(self):
        winner, release = self.start_lock_held_install()
        stale_plan = self.run_cli(
            "plan", "--source", self.source, "--home", self.home
        )
        self.assertEqual("install", stale_plan["action"])
        release.write_text("release\n", encoding="utf-8")
        winner_stdout, winner_stderr = winner.communicate(timeout=60)
        self.assertEqual(0, winner.returncode, winner_stderr or winner_stdout)

        follower = self.run_cli(
            "install", "--source", self.source, "--home", self.home
        )
        self.assertEqual("already_installed", follower["action"])
        self.assertFalse(follower["changed"])

        undone = self.run_cli(
            "undo",
            "--home",
            self.home,
            "--receipt",
            follower["undo_receipt"],
        )
        self.assertEqual("undo_noop", undone["status"])
        self.assertFalse(undone["changed"])
        self.assertEqual(
            "verified", self.run_cli("verify", "--home", self.home)["status"]
        )

    def test_install_recovers_stale_unlocked_metadata_without_preserving_owner_chain(self):
        lock_path = (
            self.home
            / ".agents"
            / "locks"
            / "claudecode-wiki-skills.lock"
        )
        lock_path.parent.mkdir(parents=True)
        stale = {
            "schema_version": 1,
            "package": "claudecode-wiki-skills",
            "owner_id": "stale-owner",
            "pid": 99999999,
            "hostname": "stale-host",
            "operation": "install",
            "home": str(self.home),
            "created_at": "2000-01-01T00:00:00+00:00",
            "created_unix": 946684800,
            "previous_owner": {"owner_id": "must-not-grow-recursively"},
        }
        lock_path.write_bytes(
            b"\x00" + (json.dumps(stale, ensure_ascii=False) + "\n").encode("utf-8")
        )

        receipt = self.run_cli("install", "--source", self.source, "--home", self.home)

        metadata = json.loads(lock_path.read_bytes()[1:].decode("utf-8"))
        self.assertEqual("installed", receipt["status"])
        self.assertEqual(
            {
                "owner_id": "stale-owner",
                "pid": 99999999,
                "created_at": "2000-01-01T00:00:00+00:00",
                "released_at": None,
            },
            metadata["previous_owner"],
        )
        self.assertIn("released_at", metadata)
        self.assertEqual("verified", self.run_cli("verify", "--home", self.home)["status"])

    def test_install_preserves_unrelated_entries_but_blocks_unknown_name_collision(self):
        root = self.home / ".agents" / "skills"
        unrelated = root / "my-private-skill"
        unrelated.mkdir(parents=True)
        (unrelated / "SKILL.md").write_text("private\n", encoding="utf-8")
        collision = self.home / ".claude" / "skills" / "design-juan-wiki"
        collision.mkdir(parents=True)
        (collision / "owner.txt").write_text("unknown\n", encoding="utf-8")

        blocked = self.run_cli(
            "install", "--source", self.source, "--home", self.home, expected=2
        )

        self.assertEqual("blocked", blocked["status"])
        self.assertIn("拒绝覆盖", blocked["error"])
        self.assertEqual("private\n", (unrelated / "SKILL.md").read_text(encoding="utf-8"))
        self.assertEqual("unknown\n", (collision / "owner.txt").read_text(encoding="utf-8"))
        self.assertFalse((self.home / ".agents" / "packages" / "claudecode-wiki-skills").exists())

    def test_ima_is_installed_only_with_explicit_opt_in(self):
        receipt = self.run_cli(
            "install", "--source", self.source, "--home", self.home, "--include-ima"
        )

        self.assertEqual([*CORE, "ima-skill"], receipt["skills"])
        self.assertTrue((self.home / ".agents" / "skills" / "ima-skill" / "SKILL.md").is_file())
        self.assertTrue((self.home / ".claude" / "skills" / "ima-skill" / "SKILL.md").is_file())

    def test_verify_detects_package_fingerprint_drift(self):
        self.run_cli("install", "--source", self.source, "--home", self.home)
        verified = self.run_cli("verify", "--home", self.home)
        self.assertEqual("verified", verified["status"])
        self.assertTrue(verified["skill_installed"])
        self.assertFalse(verified["keyword_runtime_ready"])
        self.assertEqual("KEYWORD_RUNTIME_UNPROVISIONED", verified["keyword_runtime_error"])
        installed_skill = (
            self.home
            / ".agents"
            / "packages"
            / "claudecode-wiki-skills"
            / "versions"
            / "2.0.1"
            / "core"
            / "design-juan-wiki"
            / "SKILL.md"
        )
        installed_skill.write_text("drift\n", encoding="utf-8")

        blocked = self.run_cli("verify", "--home", self.home, expected=2)

        self.assertEqual("blocked", blocked["status"])
        self.assertIn("指纹漂移", blocked["error"])

    def test_verify_rejects_incomplete_ownership_state(self):
        self.run_cli("install", "--source", self.source, "--home", self.home)
        state_path = (
            self.home
            / ".agents"
            / "packages"
            / "claudecode-wiki-skills"
            / "state.json"
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        del state["entries"]["claude"]
        state_path.write_text(json.dumps(state), encoding="utf-8")

        blocked = self.run_cli("verify", "--home", self.home, expected=2)

        self.assertEqual("blocked", blocked["status"])
        self.assertIn("所有权", blocked["error"])

    def test_install_upgrades_side_by_side_and_switches_owned_entries(self):
        self.run_cli("install", "--source", self.source, "--home", self.home)
        self._make_source("2.0.2")
        marker = self.source / "core" / "design-juan-wiki" / "release.txt"
        marker.write_text("2.0.2\n", encoding="utf-8")

        upgraded = self.run_cli("install", "--source", self.source, "--home", self.home)

        package = self.home / ".agents" / "packages" / "claudecode-wiki-skills"
        state = json.loads((package / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("upgraded", upgraded["status"])
        self.assertEqual("2.0.2", state["active_version"])
        self.assertEqual(["2.0.1"], state["previous_versions"])
        self.assertTrue((package / "versions" / "2.0.1").is_dir())
        self.assertTrue((package / "versions" / "2.0.2").is_dir())
        self.assertEqual(
            "2.0.2\n",
            (self.home / ".agents" / "skills" / "design-juan-wiki" / "release.txt").read_text(encoding="utf-8"),
        )

    def test_upgrade_receipt_restores_exact_previous_state(self):
        self.run_cli("install", "--source", self.source, "--home", self.home)
        self._make_source("2.0.2")
        marker = self.source / "core" / "design-juan-wiki" / "release.txt"
        marker.write_text("2.0.2\n", encoding="utf-8")

        upgraded = self.run_cli(
            "install", "--source", self.source, "--home", self.home
        )
        self.assertEqual("upgrade", upgraded["action"])
        self.assertTrue(upgraded["changed"])

        undone = self.run_cli(
            "undo",
            "--home",
            self.home,
            "--receipt",
            upgraded["undo_receipt"],
        )

        self.assertEqual("undone", undone["status"])
        verified = self.run_cli("verify", "--home", self.home)
        self.assertEqual("2.0.1", verified["version"])
        package = (
            self.home / ".agents" / "packages" / "claudecode-wiki-skills"
        )
        self.assertFalse((package / "versions" / "2.0.2").exists())
        self.assertFalse(
            (
                self.home
                / ".agents"
                / "skills"
                / "design-juan-wiki"
                / "release.txt"
            ).exists()
        )

    def test_upgrade_blocks_link_mode_migration_without_separate_migration(self):
        self.run_cli("install", "--source", self.source, "--home", self.home)
        self._make_source("2.0.2")

        blocked = self.run_cli(
            "install",
            "--source",
            self.source,
            "--home",
            self.home,
            "--link-mode",
            "copy",
            "--allow-copy-fallback",
            expected=2,
        )

        self.assertEqual("blocked", blocked["status"])
        self.assertIn("入口模式迁移", blocked["error"])
        self.assertEqual("2.0.1", self.run_cli("verify", "--home", self.home)["version"])

    def test_rollback_switches_entries_to_previous_version_without_deleting_versions(self):
        self.run_cli("install", "--source", self.source, "--home", self.home)
        self._make_source("2.0.2")
        (self.source / "core" / "design-juan-wiki" / "release.txt").write_text(
            "2.0.2\n", encoding="utf-8"
        )
        self.run_cli("install", "--source", self.source, "--home", self.home)

        receipt = self.run_cli("rollback", "--home", self.home)

        package = self.home / ".agents" / "packages" / "claudecode-wiki-skills"
        state = json.loads((package / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("rolled_back", receipt["status"])
        self.assertEqual("2.0.1", state["active_version"])
        self.assertEqual([], state["previous_versions"])
        self.assertFalse(
            (self.home / ".agents" / "skills" / "design-juan-wiki" / "release.txt").exists()
        )
        self.assertTrue((package / "versions" / "2.0.2").is_dir())
        self.assertEqual("verified", self.run_cli("verify", "--home", self.home)["status"])

    def test_uninstall_removes_only_manifest_owned_content(self):
        unrelated = self.home / ".agents" / "skills" / "my-private-skill"
        unrelated.mkdir(parents=True)
        (unrelated / "SKILL.md").write_text("private\n", encoding="utf-8")
        self.run_cli("install", "--source", self.source, "--home", self.home)

        receipt = self.run_cli("uninstall", "--home", self.home)

        self.assertEqual("uninstalled", receipt["status"])
        self.assertEqual("private\n", (unrelated / "SKILL.md").read_text(encoding="utf-8"))
        for runtime in (self.home / ".agents" / "skills", self.home / ".claude" / "skills"):
            for name in CORE:
                self.assertFalse((runtime / name).exists())
        self.assertFalse(
            (self.home / ".agents" / "packages" / "claudecode-wiki-skills").exists()
        )

    def test_uninstall_fails_closed_when_owned_entry_target_drifted(self):
        self.run_cli("install", "--source", self.source, "--home", self.home)
        entry = self.home / ".agents" / "skills" / "design-juan-wiki"
        if os.name == "nt":
            os.rmdir(entry)
        else:
            entry.unlink()
        entry.mkdir()
        (entry / "SKILL.md").write_text("unknown replacement\n", encoding="utf-8")

        blocked = self.run_cli("uninstall", "--home", self.home, expected=2)

        self.assertEqual("blocked", blocked["status"])
        self.assertIn("漂移", blocked["error"])
        self.assertEqual(
            "unknown replacement\n", (entry / "SKILL.md").read_text(encoding="utf-8")
        )

    def test_explicit_copy_fallback_uses_fingerprinted_wrapper_to_canonical_tree(self):
        receipt = self.run_cli(
            "install",
            "--source",
            self.source,
            "--home",
            self.home,
            "--link-mode",
            "copy",
            "--allow-copy-fallback",
        )

        self.assertEqual("installed", receipt["status"])
        entry = self.home / ".agents" / "skills" / "design-juan-wiki"
        wrapper = (entry / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("受管发现包装器", wrapper)
        self.assertIn("versions\\2.0.1\\core\\design-juan-wiki\\SKILL.md", wrapper)
        package = self.home / ".agents" / "packages" / "claudecode-wiki-skills"
        state = json.loads((package / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("copy", state["entries"]["codex"]["design-juan-wiki"]["mode"])
        self.assertEqual("verified", self.run_cli("verify", "--home", self.home)["status"])

    def test_runtime_root_alias_is_owned_once_across_full_lifecycle(self):
        self.alias_claude_root_to_codex_root()

        plan = self.run_cli("plan", "--source", self.source, "--home", self.home)
        self.assertEqual({"codex": "codex", "claude": "codex"}, plan["runtime_aliases"])
        self.run_cli("install", "--source", self.source, "--home", self.home)
        package = self.home / ".agents" / "packages" / "claudecode-wiki-skills"
        state = json.loads((package / "state.json").read_text(encoding="utf-8"))
        self.assertEqual({"codex": "codex", "claude": "codex"}, state["runtime_aliases"])
        self.assertEqual(["codex"], list(state["owned_roots"]))
        self.assertEqual(["codex"], list(state["entries"]))
        self.assertEqual("verified", self.run_cli("verify", "--home", self.home)["status"])

        self._make_source("2.0.2")
        (self.source / "core" / "design-juan-wiki" / "release.txt").write_text(
            "2.0.2\n", encoding="utf-8"
        )
        self.assertEqual(
            "upgraded",
            self.run_cli("install", "--source", self.source, "--home", self.home)["status"],
        )
        self.assertEqual("rolled_back", self.run_cli("rollback", "--home", self.home)["status"])
        self.assertEqual("verified", self.run_cli("verify", "--home", self.home)["status"])
        self.assertEqual("uninstalled", self.run_cli("uninstall", "--home", self.home)["status"])
        self.assertTrue((self.home / ".claude" / "skills").is_dir())
        self.assertEqual([], list((self.home / ".agents" / "skills").iterdir()))

    def test_verify_fails_closed_when_runtime_root_alias_drifted(self):
        self.alias_claude_root_to_codex_root()
        self.run_cli("install", "--source", self.source, "--home", self.home)
        claude_root = self.home / ".claude" / "skills"
        if os.name == "nt":
            os.rmdir(claude_root)
        else:
            claude_root.unlink()
        claude_root.mkdir()

        blocked = self.run_cli("verify", "--home", self.home, expected=2)

        self.assertEqual("blocked", blocked["status"])
        self.assertIn("别名状态漂移", blocked["error"])
        self.assertTrue((self.home / ".agents" / "skills" / "design-juan-wiki").exists())


if __name__ == "__main__":
    unittest.main()
