import errno
import hashlib
import os
from pathlib import Path
import select
import shlex
import subprocess
import sys
import time
from threading import Event, Thread
from unittest.mock import patch

from agent_bridge import BridgeError
from agent_bridge.transport.git import ACCEPTED_REF, LOCAL_REF
from agent_bridge.transport.git_backend import GitBackend, GitError
from tests.git_support import GitTestCase


class RecordingBackend(GitBackend):
    """Records real Git operations; it never substitutes Git results."""
    def __init__(self, directory, on_rejection=None):
        super().__init__(directory)
        self.calls = []
        self.on_rejection = on_rejection

    def run(self, args, **kwargs):
        self.calls.append(tuple(args))
        try:
            return super().run(args, **kwargs)
        except GitError:
            if "push" in args and self.on_rejection:
                callback, self.on_rejection = self.on_rejection, None
                callback()
            raise


class GitRecoveryTests(GitTestCase):
    def no_force(self, backend):
        self.assertFalse(any(any(arg.startswith(("--force", "+")) for arg in args) for args in backend.calls))

    def rejection_fixture(self):
        self.ba.state(source="agent-a", summary="Fixture baseline", state={"ready": True})
        self.ba.sync(push=True)
        self.task_a()
        cached = self.ba.sync(push=False)["cached_commit"]
        lock = self.remote / "refs/heads/agentbridge.lock"
        lock.write_text("Owned by this test fixture; no remote writer\n", encoding="ascii")
        return lock, cached

    def test_first_real_rejection_fetches_reconciles_and_retries_without_force(self):
        lock, cached = self.rejection_fixture()
        backend = RecordingBackend(self.ba.transport.cache, on_rejection=lock.unlink)
        self.ba.transport.git = backend
        result = self.ba.sync(push=True)
        self.assertEqual(result["push_attempts"], 2)
        self.assertEqual(result["cached_commit"], cached)
        pushes = [index for index, args in enumerate(backend.calls) if "push" in args]
        self.assertEqual(len(pushes), 2)
        self.assertTrue(any(args[0] == "fetch" for args in backend.calls[pushes[0] + 1:pushes[1]]))
        self.no_force(backend)

    def test_second_rejection_blocks_preserves_cache_and_manual_retry_is_idempotent(self):
        lock, cached = self.rejection_fixture()
        before = self.ba.status()
        backend = RecordingBackend(self.ba.transport.cache)
        self.ba.transport.git = backend
        with self.assertRaisesRegex(BridgeError, "BLOCKED.*one reconciliation retry"):
            self.ba.sync(push=True)
        self.assertEqual(sum("push" in args for args in backend.calls), 2)
        self.assertEqual(self.ba.status(), before)
        self.assertEqual(backend.oid(LOCAL_REF), cached)
        self.assertEqual(self.ba.transport.status(self.ba)["cached_packets_pending_remote_delivery"], 1)
        self.assertTrue(lock.exists())  # No attempt to remove another writer's lock.
        self.assertFalse((self.ba.store.data / ".git-sync.lock").exists())
        self.assertFalse((self.ba.store.data / ".lock").exists())
        self.assertEqual(list(self.ba.store.data.glob("git-index-*")), [])
        self.no_force(backend)
        lock.unlink()  # This fixture owns it; no remote writer is active.
        result = self.ba.sync(push=True)
        self.assertEqual(result["cached_commit"], cached)
        self.assertEqual(self.ba.status(), before)
        self.assertEqual(self.ba.transport.status(self.ba)["cached_packets_pending_remote_delivery"], 0)
        replay = self.ba.sync(push=False)
        self.assertEqual(replay["ingested"], 0)
        self.assertEqual(replay["published_to_cache"], 0)
        self.no_force(backend)

    def test_interrupted_ingestion_releases_locks_and_replays_valid_prefix_once(self):
        task = self.task_a()
        handoff = self.ba.handoff(task.task_id, source="agent-a", destination="agent-b", instructions="Fixture work")
        self.ba.sync(push=True)
        original = self.bb.receive
        count = 0
        def interrupted(message):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("Injected interruption between atomic receives")
            return original(message)
        # Exact interruption timing cannot be induced reliably by a bare remote.
        with patch.object(self.bb, "receive", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "Injected interruption"):
                self.bb.sync(push=False)
        self.assertEqual([m.message_id for m in self.bb.events()], [task.message_id])
        self.assertIsNone(self.bb.transport.git.oid(ACCEPTED_REF))
        self.assertFalse((self.bb.store.data / ".git-sync.lock").exists())
        self.assertFalse((self.bb.store.data / ".lock").exists())
        retry = self.bb.sync(push=False)
        self.assertEqual(retry["ingested"], 1)
        self.assertEqual([m.to_dict() for m in self.bb.events()], [task.to_dict(), handoff.to_dict()])
        replay = self.bb.sync(push=False)
        self.assertEqual(replay["ingested"], 0)
        self.assertEqual(replay["published_to_cache"], 0)

    def test_stale_sync_lock_is_bounded_preserved_and_recovered_only_manually(self):
        lock = self.bb.store.data / ".git-sync.lock"
        content = "Dormant fixture lock; no sync writer is active\n"
        lock.write_text(content, encoding="ascii")
        started = time.monotonic()
        with self.assertRaisesRegex(BridgeError, "locked by another operation"):
            self.bb.sync(push=False)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 5)
        self.assertLess(elapsed, 8)
        self.assertEqual(lock.read_text(encoding="ascii"), content)
        lock.unlink()  # Manual recovery only after confirming this fixture has no writer.
        first = self.bb.sync(push=False)
        replay = self.bb.sync(push=False)
        self.assertEqual(first["cached_commit"], replay["cached_commit"])
        self.assertEqual(replay["ingested"], 0)
        self.assertFalse(lock.exists())

    def stall_args(self):
        helper = self.root / "stall.py"
        helper.write_text("import os, time\nfrom pathlib import Path\n"
                          "Path(__file__).with_suffix('.pid').write_text(str(os.getpid()))\n"
                          "print('fixture started', flush=True)\ntime.sleep(60)\n", encoding="utf-8")
        # A trusted test-only Git alias launches a real child, independent of the network.
        command = "!" + shlex.quote(Path(sys.executable).as_posix()) + " " + shlex.quote(helper.as_posix())
        return ["-c", "alias.agentbridge-stall=" + command, "agentbridge-stall"], helper

    def test_real_git_timeout_reaps_process_tree_and_closes_pipes(self):
        backend = GitBackend(self.remote, timeout=3)
        args, helper = self.stall_args()
        processes = []
        original = subprocess.Popen
        def observe(*arguments, **kwargs):
            process = original(*arguments, **kwargs)
            if arguments[0][0] == backend.executable:
                processes.append(process)
            return process
        started = time.monotonic()
        with patch("agent_bridge.transport.git_backend.subprocess.Popen", side_effect=observe):
            with self.assertRaisesRegex(BridgeError, "timed out.*cleanup completed"):
                backend.run(args)
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(helper.with_suffix(".pid").exists())
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdout.closed)
        self.assertTrue(processes[0].stderr.closed)
        self.assertEqual(backend.run(["rev-parse", "--is-bare-repository"]), b"true\n")

    def test_timeout_during_sync_releases_lock_and_preserves_local_events(self):
        self.task_a()
        before = self.ba.status()
        args, _ = self.stall_args()
        backend = self.ba.transport.git
        backend.timeout = 3
        original = backend.run
        def stalled(arguments, **kwargs):
            return original(args if arguments[0] == "ls-remote" else arguments, **kwargs)
        with patch.object(backend, "run", side_effect=stalled):
            with self.assertRaisesRegex(BridgeError, "timed out"):
                self.ba.sync(push=False)
        self.assertEqual(self.ba.status(), before)
        self.assertFalse((self.ba.store.data / ".git-sync.lock").exists())
        self.assertEqual(list(self.ba.store.data.glob("git-index-*")), [])
        backend.timeout = 30
        self.ba.sync(push=False)
        self.assertEqual(self.ba.status(), before)

    def test_interrupted_wait_reaps_real_git_and_closes_pipes(self):
        backend = GitBackend(self.remote)
        args, helper = self.stall_args()
        processes = []
        original = subprocess.Popen
        def interrupted(*arguments, **kwargs):
            process = original(*arguments, **kwargs)
            if arguments[0][0] == backend.executable:
                processes.append(process)
                wait = process.wait
                first = True
                def interrupt_once(*values, **options):
                    nonlocal first
                    if first:
                        first = False
                        raise KeyboardInterrupt("Injected caller interrupt")
                    return wait(*values, **options)
                process.wait = interrupt_once
            return process
        # Interrupting the test runner itself is unsafe; only this wait is injected.
        with patch("agent_bridge.transport.git_backend.subprocess.Popen", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                backend.run(args)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdout.closed)
        self.assertTrue(processes[0].stderr.closed)

    def test_nonzero_git_failure_reaps_process_and_closes_pipes(self):
        backend = GitBackend(self.remote)
        processes = []
        original = subprocess.Popen
        def observe(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process
        with patch("agent_bridge.transport.git_backend.subprocess.Popen", side_effect=observe):
            with self.assertRaises(GitError):
                backend.run(["rev-parse", "--verify", "refs/heads/absent"])
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdout.closed)
        self.assertTrue(processes[0].stderr.closed)

    def test_interruption_with_blocked_stdin_kills_child_and_joins_workers(self):
        backend = GitBackend(self.remote)
        args, helper = self.stall_args()
        ready = Event()
        interruption = KeyboardInterrupt("Fixture caller interruption")
        processes, threads, watches = [], [], []
        original = subprocess.Popen

        def observe(*arguments, **kwargs):
            process = original(*arguments, **kwargs)
            processes.append(process)
            read = process.stdout.read
            def read_ready(size):
                chunk = read(size)
                if chunk:
                    ready.set()
                return chunk
            process.stdout.read = read_ready
            wait = process.wait
            first = True
            def interrupt_once(*values, **options):
                nonlocal first
                if first:
                    first = False
                    self.assertTrue(ready.wait(timeout=5), "Real child did not signal readiness")
                    pid = int(helper.with_suffix(".pid").read_text())
                    if os.name == "nt":
                        import ctypes
                        from ctypes import wintypes
                        api = ctypes.WinDLL("kernel32", use_last_error=True)
                        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                        api.OpenProcess.restype = wintypes.HANDLE
                        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                        api.WaitForSingleObject.restype = wintypes.DWORD
                        api.CloseHandle.argtypes = [wintypes.HANDLE]
                        api.CloseHandle.restype = wintypes.BOOL
                        handle = api.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
                        self.assertTrue(handle)
                        watches.append(("windows", api, handle))
                    elif hasattr(os, "pidfd_open"):
                        watches.append(("pidfd", None, os.pidfd_open(pid)))
                    else:
                        watches.append(("pid", None, pid))
                    raise interruption
                return wait(*values, **options)
            process.wait = interrupt_once
            return process

        def record_thread(*args, **kwargs):
            thread = Thread(*args, **kwargs)
            threads.append(thread)
            return thread

        started = time.monotonic()
        try:
            with patch("agent_bridge.transport.git_backend.subprocess.Popen", side_effect=observe), \
                    patch("agent_bridge.transport.git_backend.Thread", side_effect=record_thread):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    # The helper never reads stdin: this also exercises a blocked writer.
                    backend.run(args, data=b"x" * 1_048_576)
            self.assertIs(caught.exception, interruption)
            self.assertLess(time.monotonic() - started, 12)
            self.assertIsNotNone(processes[0].poll())
            self.assertTrue(all(stream.closed for stream in
                                (processes[0].stdin, processes[0].stdout, processes[0].stderr)))
            self.assertEqual(len(threads), 3)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            kind, api, handle = watches[0]
            if kind == "windows":
                self.assertEqual(api.WaitForSingleObject(handle, 0), 0)  # Child exited.
            elif kind == "pidfd":
                self.assertEqual(select.select([handle], [], [], 0)[0], [handle])
            else:
                status = subprocess.run(["ps", "-o", "stat=", "-p", str(handle)],
                                        capture_output=True, text=True, timeout=2).stdout.strip()
                self.assertTrue(not status or status.startswith("Z"), status)
        finally:
            for kind, api, handle in watches:
                if kind == "windows":
                    api.CloseHandle(handle)
                elif kind == "pidfd":
                    os.close(handle)

    def test_success_and_git_failure_close_stdin_and_join_workers(self):
        backend = GitBackend(self.remote)
        processes, threads = [], []
        original = subprocess.Popen
        def observe(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process
        def record_thread(*args, **kwargs):
            thread = Thread(*args, **kwargs)
            threads.append(thread)
            return thread
        payload = b"Complete fixture input\n" * 32768
        digest = hashlib.sha1(f"blob {len(payload)}\0".encode() + payload).hexdigest().encode() + b"\n"
        with patch("agent_bridge.transport.git_backend.subprocess.Popen", side_effect=observe), \
                patch("agent_bridge.transport.git_backend.Thread", side_effect=record_thread):
            self.assertEqual(backend.run(["hash-object", "--stdin"], data=payload), digest)
            with self.assertRaises(GitError):
                backend.run(["rev-parse", "--verify", "refs/heads/absent"], data=payload)
        self.assertTrue(all(process.poll() is not None for process in processes))
        self.assertTrue(all(stream.closed for process in processes for stream in
                            (process.stdin, process.stdout, process.stderr)))
        self.assertFalse(any(thread.is_alive() for thread in threads))

    def test_unrelated_reader_error_is_reported_after_cleanup(self):
        backend = GitBackend(self.remote)
        processes = []
        original = subprocess.Popen
        def fail_read(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            def broken_read(size):
                raise OSError(errno.EIO, "Injected pipe read failure")
            process.stdout.read = broken_read
            return process
        with patch("agent_bridge.transport.git_backend.subprocess.Popen", side_effect=fail_read):
            with self.assertRaisesRegex(BridgeError, "process/pipe cleanup.*failed"):
                backend.run(["rev-parse", "--is-bare-repository"])
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdout.closed)
        self.assertTrue(processes[0].stderr.closed)
