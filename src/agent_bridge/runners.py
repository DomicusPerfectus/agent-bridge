"""Explicit, one-shot runners over the existing Bridge store and lifecycle."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .bridge import Bridge
from .protocol import BridgeError, Message
from .protocol.message import agent, identifier
from .protocol.workflow import ProjectView


@dataclass(frozen=True)
class RunnerRegistration:
    destination: str
    runtime_required: str

    def preview(self, registry: Mapping[str, str]) -> dict:
        """Validate a proposed entry without writing a registry or starting a runner."""
        agent(self.destination)
        valid = (
            self.destination not in registry or registry[self.destination] == self.runtime_required
        )
        return {
            "destination": self.destination,
            "registration_valid": valid,
            "runtime_required": self.runtime_required,
            "applied": False,
        }


class ExplicitHandoffRunner:
    """A durable acknowledgment claims one handoff before any external invocation.

    A completed replay returns its exact report. An acknowledgment without a
    report is uncertain and never re-invoked automatically, including after a
    crash. Store transactions serialize competing claims; no polling is provided.
    """

    def __init__(self, bridge: Bridge, destination: str):
        agent(destination)
        self.bridge = bridge
        self.destination = destination

    def run(self, message_id: str, invoke: Callable[[Message], tuple[str, str]]) -> Message:
        identifier(message_id, "message_id")
        store = self.bridge.store
        with store.transaction():
            view = ProjectView(store.config(), store.read())
            handoff = view.find(message_id)
            if handoff.kind != "handoff" or handoff.destination != self.destination:
                raise BridgeError("runner_explicit_addressed_handoff_required")
            for message in view.messages.values():
                if message.kind == "report" and message.in_reply_to == message_id:
                    return message
            if message_id in view.acknowledgments:
                raise BridgeError("runner_handoff_already_claimed_no_retry")
            ack = Message.create(
                project_id=handoff.project_id,
                kind="acknowledgment",
                source=self.destination,
                destination=handoff.source,
                task_id=handoff.task_id,
                in_reply_to=message_id,
                summary="Explicit handoff claimed",
                status="acknowledged",
                payload={},
            )
            view.apply(ack)
            store.append(ack)

        # Nothing may retry this handoff once the acknowledgment has been saved.
        try:
            status, result = invoke(handoff)
        except Exception:  # noqa: BLE001 - never expose external exception data or retry
            status, result = "blocked", "runner_invocation_failed"
        report = Message.create(
            project_id=handoff.project_id,
            kind="report",
            source=self.destination,
            destination=handoff.source,
            task_id=handoff.task_id,
            in_reply_to=message_id,
            summary="Explicit handoff result",
            status=status,
            payload={
                "result": result,
                "artifacts": [],
                "blockers": [] if status == "succeeded" else ["runner_failed_closed"],
                "risks": [],
                "next_action": "Task creator should review; no automatic retry.",
            },
        )
        # Receive validates the exact parent, assignee, approvals and current
        # lifecycle, rather than attaching the result to a newer handoff.
        return self.bridge.receive(report)
