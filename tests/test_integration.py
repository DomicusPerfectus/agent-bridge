from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import sys

from tests.support import BridgeTestCase, ROOT, schema_validator


class CLIIntegrationTests(BridgeTestCase):
    def call(self, *arguments, stdin=None, success=True):
        result = subprocess.run([sys.executable, "-m", "agent_bridge", "--root", str(self.root), *arguments],
                                input=stdin, capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
        self.assertEqual(result.returncode, 0 if success else 1, result.stderr)
        if success:
            self.assertEqual(result.stderr, "")
        else:
            self.assertNotIn("Traceback", result.stderr)
        return result

    def json(self, *args, **kwargs):
        return json.loads(self.call(*args, **kwargs).stdout)

    def test_end_to_end_local_cli(self):
        initial = self.json("init", "--project", "Synthetic test project")
        state = self.json("state", "--from", "agent-a", "--summary", "Current milestone",
                          "--data", str(ROOT / "examples" / "project-state.json"))
        task = self.json("task", "--from", "agent-a", "--to", "agent-b", "--title", "Sample validation",
                         "--description", "Use synthetic inputs")
        handoff = self.json("handoff", task["task_id"], "--from", "agent-a", "--to", "agent-b",
                            "--instructions", "Validate and report")
        received = self.json("inbox", "--agent", "agent-b", "--kind", "handoff")
        self.assertEqual(received, [handoff])
        ack_b = self.json("acknowledge", handoff["message_id"], "--by", "agent-b")
        report = self.json("report", task["task_id"], "--from", "agent-b", "--result", "Passed sample validation",
                           "--artifact", "synthetic-result.txt", "--risk", "Synthetic risk",
                           "--next-action", "Review and acknowledge")
        self.assertEqual(self.json("inbox", "--agent", "agent-a", "--kind", "report"), [report])
        ack_a = self.json("acknowledge", report["message_id"], "--by", "agent-a")
        status = self.json("status")
        self.assertEqual(status["tasks"][0]["status"], "completed")
        self.assertEqual(status["message_count"], 6)
        self.assertEqual(initial["project_id"], report["project_id"])
        validator = schema_validator()
        for message in (state, task, handoff, ack_b, report, ack_a):
            validator.validate(message)

    def test_cli_approval_and_export_import(self):
        decision = self.json("decision", "--from", "chatgpt", "--to", "human:reviewer", "--summary", "Allow sample task",
                             "--rationale", "Boundary needs review", "--requires-approval")
        task = self.json("task", "--from", "chatgpt", "--to", "codex", "--title", "Sample",
                         "--description", "Synthetic data", "--approval", decision["message_id"])
        self.call("handoff", task["task_id"], "--from", "chatgpt", "--to", "codex", "--instructions", "Run", success=False)
        approval = self.json("approve", decision["message_id"], "--by", "human:reviewer",
                             "--outcome", "approved", "--rationale", "Reviewed sample")
        handoff = self.json("handoff", task["task_id"], "--from", "chatgpt", "--to", "codex", "--instructions", "Run")
        packet = self.call("export", handoff["message_id"], "--adapter", "codex").stdout
        self.assertIn("Coding agent workflow", packet)
        self.assertIn(handoff["message_id"], packet)
        exported = self.call("show", handoff["message_id"]).stdout
        count = self.json("status")["message_count"]
        self.assertEqual(self.json("import", "-", stdin=exported), handoff)
        self.assertEqual(self.json("status")["message_count"], count)
        schema_validator().validate(approval)

    def test_malformed_input_and_uninitialized_root(self):
        self.call("import", "-", stdin='{"unexpected":"input"}', success=False)
        self.call("acknowledge", "../escape", "--by", "agent-b", success=False)
        self.call("import", "-", stdin='{"x":1,"x":2}', success=False)
        child = self.root / "uninitialized"
        child.mkdir()
        self.call("--root", str(child), "status", success=False)

    def test_concurrent_processes_do_not_lose_tasks(self):
        def create(index):
            return self.json("task", "--from", "agent-a", "--to", "agent-b", "--title", f"Sample {index}",
                             "--description", "Concurrent synthetic write")
        with ThreadPoolExecutor(max_workers=4) as pool:
            messages = list(pool.map(create, range(8)))
        status = self.json("status")
        self.assertEqual(status["message_count"], 8)
        self.assertEqual(len({m["task_id"] for m in messages}), 8)
        self.assertEqual(len(status["tasks"]), 8)

    def test_documented_demo_runs(self):
        result = subprocess.run([sys.executable, str(ROOT / "examples" / "local_demo.py"), "--root", str(self.root)],
                                capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"task_status": "completed"', result.stdout)
