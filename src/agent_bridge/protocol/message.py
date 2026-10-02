"""The v0.1 envelope. JSON is canonical; Markdown is a derived view."""

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import re
from typing import Any
from uuid import UUID, uuid4

PROTOCOL_VERSION = "0.1"
MAX_MESSAGE_BYTES = 1_048_576
AGENT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}(?![\s\S])"
TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z(?![\s\S])"
STATUSES = {
    "state": ("active", "blocked"),
    "decision": ("accepted", "awaiting_approval"),
    "approval": ("approved", "rejected"),
    "task": ("pending",),
    "handoff": ("handed_off",),
    "report": ("succeeded", "failed", "blocked"),
    "acknowledgment": ("acknowledged",),
}


class BridgeError(ValueError):
    """Invalid input or an unsafe/invalid local operation."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def timestamp(value: Any, field: str = "timestamp") -> None:
    if not isinstance(value, str) or not re.fullmatch(TIMESTAMP_PATTERN, value):
        raise BridgeError(f"{field} must be an RFC3339 UTC timestamp ending in Z")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BridgeError(f"Invalid {field}") from exc


def identifier(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise BridgeError(f"{field} must be a UUID string")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise BridgeError(f"{field} must be a canonical lowercase UUID") from exc
    return value


def agent(value: Any, field: str = "agent") -> str:
    if not isinstance(value, str) or not re.fullmatch(AGENT_PATTERN, value):
        raise BridgeError(f"{field} must contain 1-64 letters, digits, dots, colons, underscores or hyphens")
    return value


def nonempty(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise BridgeError(f"{field} must be a nonempty string")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise BridgeError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise BridgeError(f"Non-finite JSON number: {value}")


def json_loads(text: str) -> Any:
    try:
        if len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise BridgeError("JSON exceeds the 1 MiB limit")
        return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except BridgeError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise BridgeError(f"Invalid JSON: {exc}") from exc


def json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n"
    except (ValueError, TypeError, RecursionError) as exc:
        raise BridgeError(f"Not JSON-serializable: {exc}") from exc


def _json_value(value: Any, depth: int = 0) -> None:
    if depth > 64:
        raise BridgeError("JSON nesting exceeds 64 levels")
    if value is None or type(value) in (str, bool, int, float):
        return
    if type(value) is list:
        for item in value:
            _json_value(item, depth + 1)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item, depth + 1)
        return
    raise BridgeError("Payloads and extensions must contain only JSON values")


def _fields(payload: dict, required: set[str]) -> None:
    if set(payload) != required:
        raise BridgeError(f"Payload fields must be exactly: {', '.join(sorted(required))}")


def _strings(value: Any, field: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise BridgeError(f"{field} must be a list of nonempty strings")


@dataclass(frozen=True)
class Message:
    protocol_version: str
    message_id: str
    project_id: str
    kind: str
    source: str
    destination: str | None
    correlation_id: str
    task_id: str | None
    in_reply_to: str | None
    timestamp: str
    status: str
    summary: str
    payload: dict
    extensions: dict

    @classmethod
    def create(cls, *, project_id: str, kind: str, source: str, summary: str,
               payload: dict, status: str, destination: str | None = None,
               correlation_id: str | None = None, task_id: str | None = None,
               in_reply_to: str | None = None, extensions: dict | None = None) -> "Message":
        message = cls(PROTOCOL_VERSION, str(uuid4()), project_id, kind, source,
                      destination, correlation_id or task_id or str(uuid4()), task_id,
                      in_reply_to, utc_now(), status, summary, deepcopy(payload),
                      deepcopy(extensions) if extensions is not None else {})
        message.validate()
        return message

    @classmethod
    def from_dict(cls, data: Any) -> "Message":
        if not isinstance(data, dict) or set(data) != set(cls.__dataclass_fields__):
            raise BridgeError("Message must contain exactly the v0.1 envelope fields")
        _json_value(data)
        message = cls(**deepcopy(data))
        message.validate()
        return message

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self) -> None:
        if self.protocol_version != PROTOCOL_VERSION:
            raise BridgeError(f"Unsupported protocol version: {self.protocol_version}")
        for field in ("message_id", "project_id", "correlation_id"):
            identifier(getattr(self, field), field)
        for field in ("task_id", "in_reply_to"):
            if getattr(self, field) is not None:
                identifier(getattr(self, field), field)
        agent(self.source, "source")
        if self.destination is not None:
            agent(self.destination, "destination")
        if not isinstance(self.kind, str) or self.kind not in STATUSES:
            raise BridgeError("Unknown message kind")
        if self.status not in STATUSES[self.kind]:
            raise BridgeError(f"Invalid status for {self.kind}")
        timestamp(self.timestamp)
        nonempty(self.summary, "summary")
        if type(self.payload) is not dict or type(self.extensions) is not dict:
            raise BridgeError("payload and extensions must be objects")
        _json_value(self.payload)
        _json_value(self.extensions)
        self._validate_payload()
        if self.kind in ("task", "handoff", "report") and self.task_id is None:
            raise BridgeError(f"{self.kind} requires task_id")
        if self.kind in ("handoff", "report", "approval", "acknowledgment") and self.in_reply_to is None:
            raise BridgeError(f"{self.kind} requires in_reply_to")
        if self.kind != "state" and self.destination is None:
            raise BridgeError(f"{self.kind} requires destination")
        if self.kind in ("state", "decision", "approval") and self.task_id is not None:
            raise BridgeError(f"{self.kind} cannot have task_id")
        if self.kind in ("state", "decision", "task") and self.in_reply_to is not None:
            raise BridgeError(f"{self.kind} cannot have in_reply_to")
        if self.task_id is not None and self.correlation_id != self.task_id:
            raise BridgeError("Task messages must use task_id as correlation_id")
        if len(json_dumps(self.to_dict()).encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise BridgeError("Message exceeds the 1 MiB limit")

    def _validate_payload(self) -> None:
        p = self.payload
        fields = {
            "state": {"state", "blockers", "risks", "next_action"},
            "decision": {"rationale", "requires_approval"},
            "approval": {"rationale"},
            "task": {"title", "description", "approval_ids"},
            "handoff": {"instructions", "context", "blockers", "risks", "next_action"},
            "report": {"result", "artifacts", "blockers", "risks", "next_action"},
            "acknowledgment": set(),
        }
        _fields(p, fields[self.kind])
        for field in ("rationale", "title", "description", "instructions", "result"):
            if field in p:
                nonempty(p[field], field)
        for field in ("blockers", "risks", "artifacts"):
            if field in p:
                _strings(p[field], field)
        if "next_action" in p and not isinstance(p["next_action"], str):
            raise BridgeError("next_action must be a string (empty is allowed)")
        for field in ("state", "context"):
            if field in p and not isinstance(p[field], dict):
                raise BridgeError(f"{field} must be an object")
        if self.kind == "decision":
            if type(p["requires_approval"]) is not bool:
                raise BridgeError("requires_approval must be a boolean")
            expected = "awaiting_approval" if p["requires_approval"] else "accepted"
            if self.status != expected:
                raise BridgeError("Decision status must match requires_approval")
        if self.kind == "task":
            _strings(p["approval_ids"], "approval_ids")
            for item in p["approval_ids"]:
                identifier(item, "approval_ids entry")
            if len(set(p["approval_ids"])) != len(p["approval_ids"]):
                raise BridgeError("approval_ids must be unique")
