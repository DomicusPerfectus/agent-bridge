from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from agent_bridge import Bridge, BridgeError, Message
from agent_bridge.protocol import json_dumps
from agent_bridge.transport import GitTransport, LocalTransport
from agent_bridge.transport.git_backend import GitBackend, endpoint, validate_branch
from agent_bridge.transport.git_packets import ordered, paths
from tests.git_support import GitTestCase
from tests.support import ROOT


class GitTransportTests(GitTestCase):
    def test_configuration_and_empty_sync(self):
        self.assertIsInstance(self.ba.transport, LocalTransport)
        configured = Bridge.open(self.a)
        self.assertIsInstance(configured.transport, GitTransport)
        self.assertFalse(configured.transport.status(configured)["push_by_default"])
        before = self.ba.status()
        empty = configured.sync()
        self.assertEqual(empty["ingested"], 0)
        self.assertEqual(empty["cached_packet_count"], 0)
        self.assertFalse(empty["pushed"])
        self.assertEqual(self.ba.status(), before)
        self.assertEqual(self.ba.configure_git(self.a)["cached_commit"], empty["cached_commit"])

    def test_roundtrip_handoff_report_and_idempotent_sync(self):
        task = self.task_a()
        handoff = self.ba.handoff(task.task_id, source="agent-a", destination="agent-b", instructions="Check sample")
        self.ba.sync(push=True)
        self.bb.sync()
        self.assertIn(handoff, self.bb.inbox("agent-b"))
        self.bb.acknowledge(handoff.message_id, actor="agent-b")
        report = self.bb.report(task.task_id, source="agent-b", result="Sample passed")
        self.bb.sync(push=True)
        self.ba.transport.fetch(self.ba)
        self.assertIn(report, self.ba.inbox("agent-a"))
        self.ba.acknowledge(report.message_id, actor="agent-a")
        self.ba.sync(push=True)
        self.bb.sync()
        for bridge in (self.ba, self.bb):
            snapshot = bridge.status()
            result = bridge.sync()
            self.assertEqual(result["ingested"], 0)
            self.assertEqual(result["published_to_cache"], 0)
            self.assertEqual(bridge.status(), snapshot)
            self.assertEqual(snapshot["tasks"][0]["status"], "completed")
        self.assertEqual(report.project_id, self.project_id)
        self.assertEqual(report.correlation_id, task.task_id)
        self.assertEqual(report.source, "agent-b")
        self.assertEqual(report.destination, "agent-a")

    def test_publish_is_local_until_push_is_explicit(self):
        self.task_a()
        result = self.ba.transport.publish(self.ba)
        self.assertFalse(result["pushed"])
        self.assertIsNone(GitBackend(self.remote).oid("refs/heads/agentbridge"))
        self.ba.transport.publish(self.ba, push=True)
        self.assertIsNotNone(GitBackend(self.remote).oid("refs/heads/agentbridge"))

    def test_child_before_parent_and_state_order(self):
        task = replace(self.task_a(), message_id="ffffffff-ffff-4fff-8fff-ffffffffffff")
        handoff = Message.create(project_id=self.project_id, kind="handoff", source="agent-a", destination="agent-b",
            task_id=task.task_id, in_reply_to=task.message_id, status="handed_off", summary="Synthetic handoff",
            payload={"instructions": "Run", "context": {}, "blockers": [], "risks": [], "next_action": ""})
        handoff = replace(handoff, message_id="11111111-1111-4111-8111-111111111111")
        self.inject({**self.packet(handoff, task.message_id), **self.packet(task)})
        self.bb.transport.fetch(self.bb)
        self.assertEqual([m.kind for m in self.bb.events()], ["task", "handoff"])

    def test_pending_approval_order_is_preserved(self):
        decision = self.ba.decision(source="agent-a", destination="human:reviewer", summary="Boundary",
                                    rationale="Synthetic review", requires_approval=True)
        task = self.task_a(approval_ids=[decision.message_id])
        self.ba.approve(decision.message_id, actor="human:reviewer", outcome="approved", rationale="Reviewed")
        self.ba.handoff(task.task_id, source="agent-a", destination="agent-b", instructions="Run")
        self.ba.sync(push=True)
        self.bb.sync()
        self.assertEqual(self.bb.status()["decisions"][0]["status"], "approved")
        self.assertEqual(self.bb.status()["tasks"][0]["status"], "handed_off")

    def test_multiple_state_events_keep_origin_order(self):
        for value in (1, 2, 3):
            self.ba.state(source="agent-a", summary="Synthetic state", state={"revision": value})
        self.ba.sync(push=True)
        self.bb.sync()
        self.assertEqual(self.bb.status()["state"]["payload"]["state"], {"revision": 3})

    def test_same_id_different_content_is_rejected_before_ingestion(self):
        task = self.task_a()
        self.ba.sync(push=True)
        self.bb.sync()
        before = self.bb.status()
        self.inject(self.packet(replace(task, summary="Tampered summary")))
        with self.assertRaisesRegex(BridgeError, "Conflicting"):
            self.bb.sync()
        self.assertEqual(before, self.bb.status())

    def test_missing_parent_and_cycles_fail_without_partial_ingestion(self):
        task = self.task_a()
        artifacts = self.packet(task, str(uuid4()))
        self.inject(artifacts)
        with self.assertRaisesRegex(BridgeError, "Missing Git message dependency"):
            self.bb.sync()
        self.assertEqual(self.bb.status()["message_count"], 0)

    def test_dependency_cycle_is_rejected(self):
        first = self.ba.state(source="agent-a", summary="First", state={})
        second = self.ba.state(source="agent-a", summary="Second", state={})
        self.inject({**self.packet(first, second.message_id), **self.packet(second, first.message_id)})
        with self.assertRaisesRegex(BridgeError, "cycle"):
            self.bb.sync()
        self.assertEqual(self.bb.status()["message_count"], 0)

    def test_wrong_project_body_rejected_other_project_ignored(self):
        task = self.task_a()
        other = replace(task, project_id=str(uuid4()))
        self.inject(self.packet(other))
        self.assertEqual(self.bb.transport.fetch(self.bb)["ingested"], 0)
        self.inject({paths(self.project_id, task.message_id)[0]: json_dumps(other.to_dict()).encode("utf-8"),
                     paths(self.project_id, task.message_id)[1]: self.packet(task)[paths(self.project_id, task.message_id)[1]]})
        with self.assertRaisesRegex(BridgeError, "differs from its path"):
            self.bb.sync()
        self.assertEqual(self.bb.status()["message_count"], 0)

    def test_malformed_json_rejected(self):
        task = self.task_a()
        artifacts = self.packet(task)
        artifacts[paths(self.project_id, task.message_id)[0]] = b"{broken JSON"
        self.inject(artifacts)
        with self.assertRaisesRegex(BridgeError, "Invalid JSON"):
            self.bb.sync()
        self.assertEqual(self.bb.status()["message_count"], 0)

    def test_oversized_envelope_rejected(self):
        task = self.task_a()
        artifacts = self.packet(task)
        artifacts[paths(self.project_id, task.message_id)[0]] = b"x" * 1_048_577
        self.inject(artifacts)
        with self.assertRaisesRegex(BridgeError, "size limit"):
            self.bb.sync()

    def test_path_traversal_malicious_filename_and_symlink_rejected(self):
        with self.assertRaises(BridgeError):
            paths(self.project_id, "../escape")
        self.inject({f".agentbridge-remote/projects/{self.project_id}/messages/not-a-uuid.json": b"{}"})
        with self.assertRaisesRegex(BridgeError, "unsafe filename"):
            self.bb.sync()

    def test_symlink_tree_entry_rejected(self):
        task = self.task_a()
        self.inject({paths(self.project_id, task.message_id)[0]: ("120000", b"../../secret")})
        with self.assertRaisesRegex(BridgeError, "symlink"):
            self.bb.sync()

    def test_missing_sidecar_rejected(self):
        task = self.task_a()
        self.inject({paths(self.project_id, task.message_id)[0]: json_dumps(task.to_dict()).encode("utf-8")})
        with self.assertRaisesRegex(BridgeError, "sidecar"):
            self.bb.sync()

    def test_removed_published_messages_and_history_rewrites_rejected(self):
        task = self.task_a()
        self.ba.sync(push=True)
        self.bb.sync()
        self.inject({}, remove=paths(self.project_id, task.message_id))
        with self.assertRaisesRegex(BridgeError, "removed remotely"):
            self.bb.sync()
        self.assertEqual(self.bb.status()["message_count"], 1)

    def test_remote_history_rewrite_is_rejected(self):
        task = self.task_a()
        self.ba.sync(push=True)
        self.bb.sync()
        self.inject(self.packet(task), reset=True)
        with self.assertRaises(BridgeError):
            self.bb.sync()
        self.assertEqual(self.bb.status()["message_count"], 1)

    def test_local_operations_work_without_remote_or_git_executable(self):
        task = self.task_a()
        self.remote.rename(self.root / "offline-remote.git")
        with patch("agent_bridge.transport.git_backend.shutil.which", return_value=None):
            local = Bridge.open(self.a)
            self.assertEqual(local.show(task.message_id), task)
            self.assertIn(task, local.inbox("agent-b"))
            local.state(source="agent-a", summary="Offline", state={"offline": True})
            with self.assertRaises(BridgeError):
                local.sync()

    def test_configured_push_and_per_operation_override(self):
        clone = self.root / "explicit-push"
        self.command("git", "clone", self.remote, clone)
        bridge = Bridge.initialize(clone, project_id=self.project_id)
        bridge.configure_git(clone, push=True)
        bridge.task(source="agent-a", destination="agent-b", title="Explicit policy", description="Synthetic")
        self.assertFalse(bridge.sync(push=False)["pushed"])
        self.assertIsNone(GitBackend(self.remote).oid("refs/heads/agentbridge"))
        self.assertTrue(bridge.sync()["pushed"])
        self.assertTrue(Bridge.initialize(clone).transport.config["push"])

    def test_shared_project_identity_cannot_be_overwritten(self):
        with self.assertRaisesRegex(BridgeError, "different project_id"):
            Bridge.initialize(self.a, project_id=str(uuid4()))
        self.assertEqual(self.ba.status()["project_id"], self.project_id)

    def test_git_output_is_bounded(self):
        with self.assertRaisesRegex(BridgeError, "output bound"):
            GitBackend(self.a, bare=False).run(["--version"], limit=1)

    def test_remote_substitution_and_poisoned_cache_rejected(self):
        self.command("git", "-C", self.a, "remote", "set-url", "origin", str(self.root))
        with self.assertRaisesRegex(BridgeError, "substituted"):
            self.ba.sync()
        self.command("git", "-C", self.a, "remote", "set-url", "origin", str(self.remote))
        self.command("git", "--git-dir", self.a / ".agentbridge" / "git-cache.git", "config", "core.sshCommand", "forbidden")
        with self.assertRaisesRegex(BridgeError, "unsupported executable"):
            self.ba.sync()

    def test_invalid_refs_and_credential_endpoints_rejected(self):
        for branch in ("--force", "../main", "a..b", "main.lock", "a@{0}", "a//b", "a\n"):
            with self.subTest(branch=branch), self.assertRaises(BridgeError):
                validate_branch(branch)
        for url in ("ext::arbitrary-command", "https://user:token@example.invalid/r", "http://example.invalid/r", "--upload-pack=command"):
            with self.subTest(url=url), self.assertRaises(BridgeError):
                endpoint(url, self.a)

    def test_unrelated_files_index_and_code_branch_untouched(self):
        (self.a / "notes.txt").write_text("Unrelated staged notes", encoding="utf-8")
        self.command("git", "-C", self.a, "add", "notes.txt")
        index = (self.a / ".git" / "index").read_bytes()
        head = (self.a / ".git" / "HEAD").read_bytes()
        self.task_a()
        self.ba.sync(push=True)
        self.assertEqual((self.a / ".git" / "index").read_bytes(), index)
        self.assertEqual((self.a / ".git" / "HEAD").read_bytes(), head)
        self.assertEqual((self.a / "notes.txt").read_text(encoding="utf-8"), "Unrelated staged notes")
        listing = self.command("git", "--git-dir", self.remote, "ls-tree", "-r", "--name-only", "agentbridge")
        self.assertNotIn("notes.txt", listing)
        self.assertNotIn(".agentbridge/messages", listing)

    def test_remote_unrelated_blobs_preserved_without_checkout(self):
        self.inject({"unrelated.txt": b"Unrelated remote data", ".gitconfig": b"[core]\nsshCommand = forbidden\n"})
        self.task_a()
        self.ba.sync(push=True)
        content = self.command("git", "--git-dir", self.remote, "show", "agentbridge:unrelated.txt")
        self.assertEqual(content, "Unrelated remote data")
        self.assertFalse((self.a / ".gitconfig").exists())

    def test_push_race_reconciles_once_without_force_push(self):
        barrier = Barrier(2)
        all_args = []
        class RacingBackend(GitBackend):
            raced = False
            def run(inner, args, **kwargs):
                all_args.append(tuple(args))
                if "push" in args and not inner.raced:
                    inner.raced = True
                    barrier.wait(timeout=20)
                return super().run(args, **kwargs)
        for bridge, label in ((self.ba, "agent-a"), (self.bb, "agent-c")):
            bridge.task(source=label, destination="agent-b", title="Independent synthetic task", description="Race test")
            bridge.transport.git = RacingBackend(bridge.transport.cache)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda b: b.sync(push=True), (self.ba, self.bb)))
        self.assertTrue(all(result["pushed"] for result in results))
        self.assertEqual(sorted(result["push_attempts"] for result in results), [1, 2])
        self.assertFalse(any("--force" in args or any(arg.startswith("+") for arg in args) for args in all_args))
        self.ba.sync()
        self.bb.sync()
        self.assertEqual(len(self.ba.status()["tasks"]), 2)
        self.assertEqual(len(self.bb.status()["tasks"]), 2)

    def test_real_git_e2e_in_separate_cli_processes(self):
        directory = self.root / "process-demo"
        result = subprocess.run([sys.executable, str(ROOT / "examples" / "git_demo.py"), "--root", str(directory)],
                                capture_output=True, text=True, encoding="utf-8", timeout=120, shell=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"task_status": "completed"', result.stdout)
        self.assertIn('"separate_cli_processes": true', result.stdout)
