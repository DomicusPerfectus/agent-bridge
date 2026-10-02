from dataclasses import replace
from uuid import uuid4

from agent_bridge import Bridge, BridgeError, Message
from tests.support import BridgeTestCase


class WorkflowTests(BridgeTestCase):
    def test_full_local_flow_survives_reopening(self):
        self.bridge.state(source="agent-a", summary="Starting", state={"milestone": "v0.1"},
                          risks=["Synthetic risk"], next_action="Create a sample task")
        task, handoff = self.dispatched()
        receiver = Bridge.open(self.root)
        self.assertIn(handoff, receiver.inbox("agent-b"))
        self.assertEqual(handoff.payload["context"]["project_state"]["payload"]["state"]["milestone"], "v0.1")
        receipt = receiver.acknowledge(handoff.message_id, actor="agent-b")
        self.assertNotIn(handoff, receiver.inbox("agent-b"))
        self.assertIn(handoff, receiver.inbox("agent-b", include_acknowledged=True))
        self.assertEqual(receiver.acknowledge(handoff.message_id, actor="agent-b"), receipt)
        report = receiver.report(task.task_id, source="agent-b", result="Sample passed", artifacts=["sample.txt"],
                                 next_action="Review the result")
        origin = Bridge.open(self.root)
        self.assertIn(report, origin.inbox("agent-a"))
        self.assertEqual(origin.status()["tasks"][0]["status"], "awaiting_review")
        origin.acknowledge(report.message_id, actor="agent-a")
        status = Bridge.open(self.root).status()
        self.assertEqual(status["tasks"][0]["status"], "completed")
        self.assertEqual(status["recommended_next_action"], "No pending tasks or approvals.")

    def test_human_approval_gate(self):
        decision = self.bridge.decision(source="agent-a", destination="human:reviewer", summary="Approve task",
                                        rationale="A synthetic boundary", requires_approval=True)
        task = self.task(approval_ids=[decision.message_id])
        before = self.bridge.status()["message_count"]
        with self.assertRaisesRegex(BridgeError, "blocked by approval"):
            self.bridge.handoff(task.task_id, source="agent-a", destination="agent-b", instructions="Run")
        self.assertEqual(before, self.bridge.status()["message_count"])
        with self.assertRaises(BridgeError):
            self.bridge.approve(decision.message_id, actor="agent-b", outcome="approved", rationale="Claim")
        self.bridge.approve(decision.message_id, actor="human:reviewer", outcome="approved", rationale="Reviewed")
        self.bridge.handoff(task.task_id, source="agent-a", destination="agent-b", instructions="Run")
        self.assertEqual(self.bridge.status()["decisions"][0]["status"], "approved")
        with self.assertRaises(BridgeError):
            self.bridge.approve(decision.message_id, actor="human:reviewer", outcome="rejected", rationale="Late")

    def test_rejected_approval_keeps_gate_closed(self):
        decision = self.bridge.decision(source="agent-a", destination="human:reviewer", summary="Boundary",
                                        rationale="Review", requires_approval=True)
        self.bridge.approve(decision.message_id, actor="human:reviewer", outcome="rejected", rationale="Denied")
        task = self.task(approval_ids=[decision.message_id])
        snapshot = self.bridge.status()
        self.assertEqual(snapshot["decisions"][0]["resolution_rationale"], "Denied")
        self.assertIn("rejected approval", snapshot["recommended_next_action"])
        with self.assertRaises(BridgeError):
            self.bridge.handoff(task.task_id, source="agent-a", destination="agent-b", instructions="Run")

    def test_nonhuman_approval_destination_is_rejected(self):
        with self.assertRaisesRegex(BridgeError, "human:<name>"):
            self.bridge.decision(source="agent-a", destination="codex", summary="Boundary", rationale="Review",
                                 requires_approval=True)

    def test_failed_report_retry_and_reassignment(self):
        task, first = self.working()
        report = self.bridge.report(task.task_id, source="agent-b", result="Missing synthetic input",
                                    status="blocked", blockers=["Need sample"], risks=["Delay"])
        self.bridge.acknowledge(report.message_id, actor="agent-a")
        self.assertEqual(self.bridge.status()["tasks"][0]["status"], "blocked")
        retry = self.bridge.handoff(task.task_id, source="agent-a", destination="agent-c", instructions="Use new sample")
        self.assertEqual(retry.in_reply_to, report.message_id)
        self.bridge.acknowledge(retry.message_id, actor="agent-c")
        with self.assertRaises(BridgeError):
            self.bridge.report(task.task_id, source="agent-b", result="Stale agent result")
        result = self.bridge.report(task.task_id, source="agent-c", result="Passed")
        self.bridge.acknowledge(result.message_id, actor="agent-a")
        self.assertEqual(self.bridge.status()["tasks"][0]["status"], "completed")

    def test_routing_and_order_are_enforced(self):
        task, handoff = self.dispatched()
        with self.assertRaisesRegex(BridgeError, "acknowledged current"):
            self.bridge.report(task.task_id, source="agent-b", result="Too early")
        with self.assertRaises(BridgeError):
            self.bridge.acknowledge(handoff.message_id, actor="agent-a")
        with self.assertRaises(BridgeError):
            self.bridge.handoff(task.task_id, source="agent-a", destination="agent-b", instructions="Duplicate")
        self.bridge.acknowledge(handoff.message_id, actor="agent-b")
        report = self.bridge.report(task.task_id, source="agent-b", result="Passed")
        with self.assertRaises(BridgeError):
            self.bridge.acknowledge(report.message_id, actor="agent-b")
        self.bridge.acknowledge(report.message_id, actor="agent-a")
        with self.assertRaises(BridgeError):
            self.bridge.report(task.task_id, source="agent-b", result="Duplicate result")

    def test_import_is_idempotent_and_conflicts_rejected(self):
        task = self.task()
        before = self.bridge.status()["message_count"]
        self.assertEqual(self.bridge.receive(task), task)
        self.assertEqual(before, self.bridge.status()["message_count"])
        with self.assertRaisesRegex(BridgeError, "different content"):
            self.bridge.receive(replace(task, summary="Conflicting summary"))
        with self.assertRaisesRegex(BridgeError, "another project"):
            self.bridge.receive(replace(task, message_id=str(uuid4()), project_id=str(uuid4())))
        handoff = Message.create(project_id=task.project_id, kind="handoff", source="agent-a", destination="agent-b",
            task_id=task.task_id, in_reply_to=str(uuid4()), summary="Unknown parent", status="handed_off",
            payload={"instructions": "Run", "context": {}, "blockers": [], "risks": [], "next_action": ""})
        with self.assertRaisesRegex(BridgeError, "Unknown message"):
            self.bridge.receive(handoff)

    def test_import_cannot_bypass_approval_or_task_ownership(self):
        decision = self.bridge.decision(source="agent-a", destination="human:reviewer", summary="Boundary",
                                        rationale="Review", requires_approval=True)
        task = self.task(approval_ids=[decision.message_id])
        handoff = Message.create(project_id=task.project_id, kind="handoff", source="agent-a", destination="agent-b",
            task_id=task.task_id, in_reply_to=task.message_id, summary="Packet", status="handed_off",
            payload={"instructions": "Run", "context": {}, "blockers": [], "risks": [], "next_action": ""})
        with self.assertRaisesRegex(BridgeError, "blocked by approval"):
            self.bridge.receive(handoff)
        with self.assertRaisesRegex(BridgeError, "task creator"):
            self.bridge.receive(replace(handoff, source="agent-c"))

    def test_receipt_is_not_approval(self):
        decision = self.bridge.decision(source="agent-a", destination="human:reviewer", summary="Boundary",
                                        rationale="Review", requires_approval=True)
        self.bridge.acknowledge(decision.message_id, actor="human:reviewer")
        self.assertEqual(self.bridge.status()["decisions"][0]["status"], "awaiting_approval")
        with self.assertRaises(BridgeError):
            self.task(approval_ids=[str(uuid4())])
