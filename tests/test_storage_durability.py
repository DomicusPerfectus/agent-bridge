import os
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_bridge import Bridge, BridgeError
from agent_bridge.runners import ExplicitHandoffRunner
from agent_bridge.storage import filesystem

from tests.support import BridgeTestCase


class StorageDurabilityTests(BridgeTestCase):
    def runner(self):
        _, handoff = self.dispatched()
        return ExplicitHandoffRunner(self.bridge, "agent-b"), handoff

    def assert_blocked(self, boundary, failure):
        runner, handoff = self.runner()
        invoke = Mock(return_value=("succeeded", "synthetic"))
        with patch(boundary, side_effect=failure):
            with self.assertRaisesRegex(BridgeError, "^bridge_storage_durability_failed$"):
                runner.run(handoff.message_id, invoke)
        invoke.assert_not_called()
        self.assertFalse(any(m.kind == "report" for m in self.bridge.events()))
        self.assertFalse(list(self.bridge.store.log.glob(".pending-*")))

    def test_temp_fsync_failure_blocks_append_and_invoke(self):
        self.assert_blocked("agent_bridge.storage.filesystem.os.fsync", OSError("PRIVATE"))

    def test_replace_failure_blocks_append_and_invoke(self):
        self.assert_blocked("agent_bridge.storage.filesystem._durable_publish", OSError("PRIVATE"))

    def test_final_barrier_failure_leaves_claim_and_blocks_invoke_and_reopen(self):
        publish = filesystem._durable_publish

        def fail_after_publication(*args):
            publish(*args)
            raise OSError("PRIVATE final barrier failure")

        self.assert_blocked(
            "agent_bridge.storage.filesystem._durable_publish", fail_after_publication
        )
        handoff = next(m for m in self.bridge.events() if m.kind == "handoff")
        invoke = Mock()
        reopened = ExplicitHandoffRunner(Bridge.open(self.root), "agent-b")
        with self.assertRaisesRegex(BridgeError, "runner_handoff_already_claimed_no_retry"):
            reopened.run(handoff.message_id, invoke)
        invoke.assert_not_called()

    def posix_boundary(self, *, replace_error=None, fsync_error=None):
        calls = []

        def replace(*args, **kwargs):
            calls.append(("replace", args, kwargs))
            if replace_error:
                raise replace_error

        def fsync(fd):
            calls.append(("fsync", fd))
            if fsync_error:
                raise fsync_error

        with (
            patch.object(os, "O_DIRECTORY", 0x10000, create=True),
            patch.object(os, "O_NOFOLLOW", 0x20000, create=True),
            patch.object(os, "open", return_value=42) as opened,
            patch.object(os, "replace", side_effect=replace),
            patch.object(os, "fsync", side_effect=fsync),
            patch.object(os, "close") as closed,
        ):
            try:
                filesystem._publish_posix(
                    str(self.root / ".pending-test"), self.root / "final.json"
                )
            finally:
                opened.assert_called_once_with(self.root, os.O_RDONLY | 0x10000 | 0x20000)
                closed.assert_called_once_with(42)
        return calls

    def test_posix_replace_then_parent_fsync_same_directory_descriptor(self):
        calls = self.posix_boundary()
        self.assertEqual(
            calls,
            [
                ("replace", (".pending-test", "final.json"), {"src_dir_fd": 42, "dst_dir_fd": 42}),
                ("fsync", 42),
            ],
        )

    def test_posix_parent_fsync_failure_propagates_and_closes(self):
        with self.assertRaises(OSError):
            self.posix_boundary(fsync_error=OSError("EIO"))

    def test_posix_replace_failure_propagates_and_closes(self):
        with self.assertRaises(OSError):
            self.posix_boundary(replace_error=OSError("EPERM"))

    def windows_boundary(self, succeeds):
        move = Mock(return_value=succeeds)
        with (
            patch.object(
                filesystem.ctypes,
                "WinDLL",
                return_value=SimpleNamespace(MoveFileExW=move),
                create=True,
            ) as dll,
            patch.object(filesystem.ctypes, "get_last_error", return_value=5, create=True),
        ):
            try:
                filesystem._publish_windows(
                    str(self.root / ".pending-test"), self.root / "final.json"
                )
            finally:
                dll.assert_called_once_with("kernel32", use_last_error=True)
                move.assert_called_once_with(
                    str(self.root / ".pending-test"), str(self.root / "final.json"), 0x9
                )
                self.assertIsNotNone(move.argtypes)
                self.assertIsNotNone(move.restype)

    def test_windows_write_through_replace_success(self):
        self.windows_boundary(True)

    def test_windows_write_through_replace_failure_propagates(self):
        with self.assertRaises(OSError):
            self.windows_boundary(False)

    def test_windows_failure_blocks_append_and_invoke(self):
        def failure(*args):
            self.windows_boundary(False)

        self.assert_blocked("agent_bridge.storage.filesystem._durable_publish", failure)

    def test_posix_fsync_failure_blocks_append_and_invoke(self):
        def failure(*args):
            self.posix_boundary(fsync_error=OSError("PRIVATE"))

        self.assert_blocked("agent_bridge.storage.filesystem._durable_publish", failure)

    def test_durable_claim_precedes_invoke_and_completed_reopen_replays(self):
        runner, handoff = self.runner()

        def invoke(message):
            # A separate reader already sees the acknowledged claim, not a lock.
            events = Bridge.open(self.root).events()
            self.assertEqual(events[-1].kind, "acknowledgment")
            return "succeeded", "synthetic"

        callback = Mock(side_effect=invoke)
        report = runner.run(handoff.message_id, callback)
        reopened = ExplicitHandoffRunner(Bridge.open(self.root), "agent-b")
        self.assertEqual(reopened.run(handoff.message_id, callback), report)
        callback.assert_called_once()

    def test_crash_after_durable_claim_before_report_refuses_redispatch(self):
        runner, handoff = self.runner()
        callback = Mock(side_effect=SystemExit("simulated process termination"))
        with self.assertRaises(SystemExit):
            runner.run(handoff.message_id, callback)
        callback.assert_called_once()
        reopened = ExplicitHandoffRunner(Bridge.open(self.root), "agent-b")
        with self.assertRaisesRegex(BridgeError, "runner_handoff_already_claimed_no_retry"):
            reopened.run(handoff.message_id, callback)
        callback.assert_called_once()
        self.assertFalse(any(m.kind == "report" for m in self.bridge.events()))

    def test_concurrent_durable_claims_invoke_once(self):
        _, handoff = self.runner()
        entered = threading.Event()
        release = threading.Event()

        def invoke(message):
            entered.set()
            if not release.wait(3):
                raise AssertionError("test synchronization timeout")
            return "succeeded", "synthetic"

        callback = Mock(side_effect=invoke)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = ExplicitHandoffRunner(Bridge.open(self.root), "agent-b")
            second = ExplicitHandoffRunner(Bridge.open(self.root), "agent-b")
            future = pool.submit(first.run, handoff.message_id, callback)
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaisesRegex(BridgeError, "runner_handoff_already_claimed_no_retry"):
                    second.run(handoff.message_id, callback)
            finally:
                release.set()
            self.assertEqual(future.result(timeout=3).status, "succeeded")
        callback.assert_called_once()

    def test_atomic_publication_replaces_complete_file_without_pending_files(self):
        path = self.root / "atomic.json"
        filesystem._atomic_write(path, {"version": 1})
        filesystem._atomic_write(path, {"version": 2})
        self.assertEqual(filesystem.read_json_file(path), {"version": 2})
        self.assertFalse(list(self.root.glob(".pending-*")))
