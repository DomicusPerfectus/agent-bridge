"""Application operations, shared by the CLI and future app/MCP adapters."""

from pathlib import Path
from typing import Callable
from uuid import uuid4

from .protocol import BridgeError, Message
from .protocol.message import agent, identifier
from .protocol.workflow import ProjectView
from .storage import FileSystemStore, Store
from .transport import LocalTransport, Transport


class Bridge:
    def __init__(self, store: Store, transport: Transport | None = None):
        self.store = store
        self.transport = transport or LocalTransport()

    @classmethod
    def initialize(cls, root: Path | str, name: str = "Agent Bridge") -> "Bridge":
        return cls(FileSystemStore.initialize(root, name))

    @classmethod
    def open(cls, root: Path | str) -> "Bridge":
        bridge = cls(FileSystemStore(root))
        with bridge.store.transaction():
            bridge.store.config()
        return bridge

    def _view(self) -> ProjectView:
        return ProjectView(self.store.config(), self.store.read())

    def _mutate(self, builder: Callable[[ProjectView], Message]) -> Message:
        with self.store.transaction():
            view = self._view()
            message = builder(view)
            existing = view.messages.get(message.message_id)
            if existing:
                if existing.to_dict() != message.to_dict():
                    raise BridgeError("Message ID already exists with different content")
                return existing
            view.apply(message)
            self.store.append(message)
            return message

    @staticmethod
    def _new(view: ProjectView, **values) -> Message:
        return Message.create(project_id=view.config["project_id"], **values)

    def status(self) -> dict:
        with self.store.transaction():
            return self._view().snapshot()

    def show(self, message_id: str) -> Message:
        identifier(message_id, "message_id")
        with self.store.transaction():
            return self._view().find(message_id)

    def inbox(self, destination: str, *, include_acknowledged: bool = False) -> list[Message]:
        with self.store.transaction():
            return self.transport.inbox(list(self._view().messages.values()), destination,
                                        include_acknowledged=include_acknowledged)

    def receive(self, message: Message) -> Message:
        """Validate and ingest one canonical envelope; identical replay is idempotent."""
        message = Message.from_dict(message.to_dict())
        return self._mutate(lambda view: message)

    def state(self, *, source: str, summary: str, state: dict,
              blockers: list[str] | None = None, risks: list[str] | None = None,
              next_action: str = "", status: str = "active") -> Message:
        return self._mutate(lambda view: self._new(view, kind="state", source=source,
            summary=summary, status=status, payload={"state": state, "blockers": blockers or [],
                                                    "risks": risks or [], "next_action": next_action}))

    def decision(self, *, source: str, destination: str, summary: str, rationale: str,
                 requires_approval: bool = False) -> Message:
        return self._mutate(lambda view: self._new(view, kind="decision", source=source,
            destination=destination, summary=summary,
            status="awaiting_approval" if requires_approval else "accepted",
            payload={"rationale": rationale, "requires_approval": requires_approval}))

    def approve(self, message_id: str, *, actor: str, outcome: str, rationale: str) -> Message:
        identifier(message_id, "message_id")
        def build(view):
            parent = view.find(message_id)
            return self._new(view, kind="approval", source=actor, destination=parent.source,
                correlation_id=parent.correlation_id, in_reply_to=parent.message_id,
                summary=f"Decision {outcome}: {parent.summary}", status=outcome,
                payload={"rationale": rationale})
        return self._mutate(build)

    def task(self, *, source: str, destination: str, title: str, description: str,
             approval_ids: list[str] | None = None) -> Message:
        task_id = str(uuid4())
        return self._mutate(lambda view: self._new(view, kind="task", source=source,
            destination=destination, summary=title, status="pending", task_id=task_id,
            payload={"title": title, "description": description, "approval_ids": approval_ids or []}))

    def handoff(self, task_id: str, *, source: str, destination: str, instructions: str,
                context: dict | None = None, blockers: list[str] | None = None,
                risks: list[str] | None = None, next_action: str = "") -> Message:
        identifier(task_id, "task_id")
        def build(view):
            task = view.task(task_id)
            packet = context if context is not None else {
                "project_state": view.state.to_dict() if view.state else None,
                "decisions": list(view.decisions.values()), "task": task,
            }
            return self._new(view, kind="handoff", source=source, destination=destination,
                task_id=task_id, in_reply_to=task["last_report"] or task["message_id"],
                summary=f"Handoff: {task['title']}", status="handed_off",
                payload={"instructions": instructions, "context": packet,
                         "blockers": blockers or [], "risks": risks or [], "next_action": next_action})
        return self._mutate(build)

    def report(self, task_id: str, *, source: str, result: str, status: str = "succeeded",
               artifacts: list[str] | None = None, blockers: list[str] | None = None,
               risks: list[str] | None = None, next_action: str = "") -> Message:
        identifier(task_id, "task_id")
        def build(view):
            task = view.task(task_id)
            if task["last_handoff"] is None:
                raise BridgeError("Task has no handoff")
            return self._new(view, kind="report", source=source, destination=task["owner"],
                task_id=task_id, in_reply_to=task["last_handoff"], summary=f"Report: {task['title']}",
                status=status, payload={"result": result, "artifacts": artifacts or [],
                    "blockers": blockers or [], "risks": risks or [], "next_action": next_action})
        return self._mutate(build)

    def acknowledge(self, message_id: str, *, actor: str) -> Message:
        identifier(message_id, "message_id")
        agent(actor)
        def build(view):
            parent = view.find(message_id)
            if parent.destination != actor:
                raise BridgeError("Only the addressed recipient may acknowledge a message")
            if message_id in view.acknowledgments:
                return view.acknowledgments[message_id]
            return self._new(view, kind="acknowledgment", source=actor, destination=parent.source,
                task_id=parent.task_id, correlation_id=parent.correlation_id,
                in_reply_to=parent.message_id, summary=f"Acknowledged: {parent.summary}",
                status="acknowledged", payload={})
        return self._mutate(build)
