"""Deterministic workflow validation and projection of the append-only log."""

from copy import deepcopy

from .message import BridgeError, Message


class ProjectView:
    def __init__(self, config: dict, messages: list[Message]):
        self.config = config
        self.messages: dict[str, Message] = {}
        self.tasks: dict[str, dict] = {}
        self.decisions: dict[str, dict] = {}
        self.acknowledgments: dict[str, Message] = {}
        self.state: Message | None = None
        for message in messages:
            self.apply(message)

    def find(self, message_id: str) -> Message:
        try:
            return self.messages[message_id]
        except KeyError as exc:
            raise BridgeError(f"Unknown message: {message_id}") from exc

    def task(self, task_id: str) -> dict:
        try:
            return self.tasks[task_id]
        except KeyError as exc:
            raise BridgeError(f"Unknown task: {task_id}") from exc

    def apply(self, m: Message) -> None:
        m.validate()
        if m.project_id != self.config["project_id"]:
            raise BridgeError("Message belongs to another project")
        if m.message_id in self.messages:
            raise BridgeError("Duplicate message ID")
        parent = self.find(m.in_reply_to) if m.in_reply_to else None
        if parent and (m.correlation_id != parent.correlation_id or m.task_id != parent.task_id):
            raise BridgeError("Reply must retain its parent's correlation_id and task_id")
        if m.kind == "state":
            self.state = m
        elif m.kind == "decision":
            if m.payload["requires_approval"] and (not m.destination.startswith("human:") or len(m.destination) <= 6):
                raise BridgeError("Approval decisions must be addressed to human:<name>")
            self.decisions[m.message_id] = {
                "message_id": m.message_id, "correlation_id": m.correlation_id,
                "summary": m.summary, "rationale": m.payload["rationale"],
                "requires_approval": m.payload["requires_approval"], "status": m.status,
                "source": m.source, "destination": m.destination,
                "created_at": m.timestamp, "updated_at": m.timestamp,
                "resolved_by": None, "resolution_message_id": None, "resolution_rationale": None,
            }
        elif m.kind == "approval":
            if parent.kind != "decision":
                raise BridgeError("Approval must reply to a decision")
            decision = self.decisions[parent.message_id]
            if decision["status"] != "awaiting_approval":
                raise BridgeError("Decision is not awaiting approval")
            if m.source != parent.destination or m.destination != parent.source:
                raise BridgeError("Only the addressed human may resolve this decision")
            decision.update(status=m.status, resolved_by=m.source, updated_at=m.timestamp,
                            resolution_message_id=m.message_id, resolution_rationale=m.payload["rationale"])
        elif m.kind == "task":
            if m.task_id in self.tasks:
                raise BridgeError("Task ID is already in use")
            for decision_id in m.payload["approval_ids"]:
                if decision_id not in self.decisions:
                    raise BridgeError(f"Unknown approval decision: {decision_id}")
            self.tasks[m.task_id] = {
                "task_id": m.task_id, "message_id": m.message_id,
                "title": m.payload["title"], "description": m.payload["description"],
                "owner": m.source, "assignee": m.destination, "status": "pending",
                "approval_ids": deepcopy(m.payload["approval_ids"]),
                "created_at": m.timestamp, "updated_at": m.timestamp,
                "last_handoff": None, "last_report": None,
            }
        elif m.kind == "handoff":
            task = self.task(m.task_id)
            if m.source != task["owner"]:
                raise BridgeError("Only the task creator may dispatch a handoff")
            if task["status"] not in ("pending", "blocked", "failed"):
                raise BridgeError(f"Cannot hand off a task in status {task['status']}")
            if m.in_reply_to != (task["last_report"] or task["message_id"]):
                raise BridgeError("Handoff must reply to the task or its latest unsuccessful report")
            for decision_id in task["approval_ids"]:
                if self.decisions[decision_id]["status"] not in ("approved", "accepted"):
                    raise BridgeError(f"Handoff blocked by approval decision: {decision_id}")
            task.update(status="handed_off", assignee=m.destination,
                        last_handoff=m.message_id, last_report=None, updated_at=m.timestamp)
        elif m.kind == "report":
            task = self.task(m.task_id)
            if m.source != task["assignee"] or m.destination != task["owner"]:
                raise BridgeError("Report must travel from the assigned agent to the task creator")
            if task["status"] != "in_progress" or m.in_reply_to != task["last_handoff"]:
                raise BridgeError("Report requires an acknowledged current handoff")
            task.update(status="awaiting_review" if m.status == "succeeded" else m.status,
                        last_report=m.message_id, updated_at=m.timestamp)
        elif m.kind == "acknowledgment":
            if parent.kind == "acknowledgment":
                raise BridgeError("Acknowledgments cannot be acknowledged")
            if m.source != parent.destination or m.destination != parent.source:
                raise BridgeError("Only the addressed recipient may acknowledge a message")
            if parent.message_id in self.acknowledgments:
                raise BridgeError("Message is already acknowledged")
            if parent.kind in ("handoff", "report"):
                task = self.task(m.task_id)
                if parent.kind == "handoff":
                    if task["status"] != "handed_off" or task["last_handoff"] != parent.message_id:
                        raise BridgeError("Cannot acknowledge a stale handoff")
                    task.update(status="in_progress", updated_at=m.timestamp)
                else:
                    if task["last_report"] != parent.message_id:
                        raise BridgeError("Cannot acknowledge a stale report")
                    if parent.status == "succeeded":
                        task.update(status="completed", updated_at=m.timestamp)
            self.acknowledgments[parent.message_id] = m
        self.messages[m.message_id] = m

    def snapshot(self) -> dict:
        pending_decisions = [d for d in self.decisions.values() if d["status"] == "awaiting_approval"]
        unfinished = [t for t in self.tasks.values() if t["status"] != "completed"]
        if pending_decisions:
            next_action = "Resolve pending human approval decisions."
        elif unfinished:
            task = unfinished[0]
            report = self.messages.get(task["last_report"])
            rejected_gate = any(self.decisions[d]["status"] == "rejected" for d in task["approval_ids"])
            next_action = ("The task has a rejected approval; propose a new decision and task." if rejected_gate else "") or (report.payload["next_action"] if report else "") or {
                "pending": "Hand off the pending task.",
                "handed_off": "The assigned agent should acknowledge the handoff.",
                "in_progress": "The assigned agent should execute the task and report.",
                "awaiting_review": "The task creator should review and acknowledge the successful report.",
                "blocked": "Resolve blockers and create a new handoff.",
                "failed": "Review the failure and create a new handoff.",
            }[task["status"]]
        elif not self.tasks and self.state:
            next_action = self.state.payload["next_action"] or "Create a task."
        else:
            next_action = "No pending tasks or approvals."
        return deepcopy({
            "protocol_version": self.config["protocol_version"],
            "project_id": self.config["project_id"], "project_name": self.config["project_name"],
            "state": self.state.to_dict() if self.state else None,
            "decisions": list(self.decisions.values()), "tasks": list(self.tasks.values()),
            "message_count": len(self.messages),
            "last_updated": next(reversed(self.messages.values())).timestamp if self.messages else self.config["created_at"],
            "recommended_next_action": next_action,
        })
