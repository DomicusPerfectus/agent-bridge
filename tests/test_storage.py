import os
import stat
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_bridge import Bridge, BridgeError
from agent_bridge.protocol import json_dumps
from agent_bridge.storage import FileSystemStore
from agent_bridge.storage.filesystem import _safe

from tests.support import BridgeTestCase


class StorageTests(BridgeTestCase):
    def test_init_is_idempotent_and_preserves_legacy_markdown(self):
        note = self.root / ".agentbridge" / "CURRENT_STATE.md"
        note.write_text("# Existing local note\n", encoding="utf-8")
        task = self.task()
        before = self.bridge.status()
        again = Bridge.initialize(self.root, "Another label").status()
        self.assertEqual(again, before)
        self.assertEqual(note.read_text(encoding="utf-8"), "# Existing local note\n")
        self.assertEqual(Bridge.open(self.root).show(task.message_id), task)

    def test_lock_timeout_preserves_other_writers_lock(self):
        lock = self.root / ".agentbridge" / ".lock"
        lock.write_text("other-writer", encoding="ascii")
        store = FileSystemStore(self.root, lock_timeout=0.01)
        with self.assertRaisesRegex(BridgeError, "locked"):
            with store.transaction():
                self.fail("Must not enter the transaction")
        self.assertEqual(lock.read_text(encoding="ascii"), "other-writer")
        lock.unlink()

    def test_atomic_write_failure_leaves_no_event_or_lock(self):
        before = self.bridge.status()
        with patch(
            "agent_bridge.storage.filesystem._durable_publish",
            side_effect=OSError("Simulated write failure"),
        ):
            with self.assertRaisesRegex(BridgeError, "bridge_storage_durability_failed"):
                self.task()
        self.assertEqual(self.bridge.status(), before)
        self.assertEqual(list((self.root / ".agentbridge" / "messages").iterdir()), [])
        self.assertFalse((self.root / ".agentbridge" / ".lock").exists())

    def test_reads_require_transaction(self):
        store = FileSystemStore(self.root)
        with self.assertRaises(BridgeError):
            store.read()
        with self.assertRaises(BridgeError):
            store.config()

    def test_corrupt_log_fails_closed(self):
        task = self.task()
        event = next((self.root / ".agentbridge" / "messages").glob("*.json"))
        event.write_text("{bad json", encoding="utf-8")
        with self.assertRaisesRegex(BridgeError, "Invalid JSON"):
            self.bridge.status()
        event.write_text(json_dumps(task.to_dict()), encoding="utf-8")
        renamed = event.with_name(event.name.replace("000000000001", "000000000002"))
        event.rename(renamed)
        with self.assertRaisesRegex(BridgeError, "sequence"):
            self.bridge.status()

    def test_symlinks_are_rejected(self):
        # Windows may restrict symlink creation; retain the synthetic guard coverage.
        with patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaisesRegex(BridgeError, "Symlink/junction"):
                FileSystemStore(self.root)

    def test_ordinary_directory_and_missing_storage_paths_are_accepted(self):
        _safe(self.root)
        project = self.root / "future project"
        project.mkdir()
        _safe(project / ".agentbridge")
        _safe(project / ".agentbridge" / "messages")
        self.assertFalse((project / ".agentbridge").exists())
        FileSystemStore(project)
        initialized = Bridge.initialize(project, "Synthetic future storage")
        self.assertEqual(initialized.status()["message_count"], 0)

    @unittest.skipUnless(os.name == "nt", "Windows directory junction regression")
    def test_real_windows_junction_rejected_without_path_is_junction(self):
        project = self.root / "junction project"
        target = self.root / "junction target"
        project.mkdir()
        target.mkdir()
        sentinel = target / "sentinel.txt"
        sentinel.write_text("Synthetic target must remain intact", encoding="utf-8")
        junction = project / ".agentbridge"
        try:
            # Fixed relative arguments avoid interpolating workspace paths into
            # cmd's command language. /d and /v:off disable autorun/expansion.
            created = subprocess.run(
                [  # noqa: S607 - fixed Windows builtin; no interpolated command
                    "cmd.exe",
                    "/d",
                    "/v:off",
                    "/c",
                    "mklink",
                    "/J",
                    ".agentbridge",
                    "..\\junction target",
                ],
                cwd=project,
                capture_output=True,
                text=True,
                timeout=10,
                shell=False,
            )
            self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
            metadata = os.lstat(junction)
            self.assertTrue(metadata.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
            self.assertEqual(metadata.st_reparse_tag, stat.IO_REPARSE_TAG_MOUNT_POINT)
            with self.assertRaisesRegex(BridgeError, "Symlink/junction"):
                FileSystemStore(project)
            # Python 3.11 executes the real check above without this Path API.
            # On every version, calling it must be unnecessary for rejection.
            with patch.object(
                Path,
                "is_junction",
                create=True,
                side_effect=AssertionError("Path.is_junction must not be used"),
            ):
                with self.assertRaisesRegex(BridgeError, "Symlink/junction"):
                    FileSystemStore(project)
            self.assertEqual(
                sentinel.read_text(encoding="utf-8"), "Synthetic target must remain intact"
            )
            # A missing target must not make the junction itself appear safe.
            sentinel.unlink()
            target.rmdir()
            with self.assertRaisesRegex(BridgeError, "Symlink/junction"):
                FileSystemStore(project)
        finally:
            # Nonrecursive rmdir removes only the junction, never its target.
            try:
                junction.rmdir()
            except FileNotFoundError:
                pass

    @unittest.skipUnless(os.name == "nt", "Windows reparse metadata")
    def test_unrelated_windows_reparse_tag_is_accepted(self):
        metadata = SimpleNamespace(
            st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
            st_reparse_tag=stat.IO_REPARSE_TAG_APPEXECLINK,
        )
        with (
            patch.object(Path, "is_symlink", return_value=False),
            patch("agent_bridge.storage.filesystem.os.lstat", return_value=metadata),
        ):
            _safe(self.root)

    def test_pending_write_is_not_visible(self):
        (self.root / ".agentbridge" / "messages" / ".pending-crashed-write").write_text(
            "{unfinished", encoding="utf-8"
        )
        self.assertEqual(self.bridge.status()["message_count"], 0)
        self.task()
        self.assertEqual(self.bridge.status()["message_count"], 1)

    def test_invalid_configuration_and_orphan_log_are_rejected(self):
        config = self.root / ".agentbridge" / "config.json"
        config.write_text('{"storage":"remote"}', encoding="utf-8")
        with self.assertRaisesRegex(BridgeError, "configuration"):
            Bridge.open(self.root)
        config.unlink()
        (self.root / ".agentbridge" / "messages" / "orphan.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(BridgeError, "orphaned"):
            Bridge.initialize(self.root)

    def test_oversized_project_name_is_not_published(self):
        child = self.root / "oversized"
        child.mkdir()
        with self.assertRaisesRegex(BridgeError, "1 MiB"):
            Bridge.initialize(child, "x" * 1_048_576)
        self.assertFalse((child / ".agentbridge" / "config.json").exists())
