"""Build the distributed JSON Schema from the envelope's primitive constraints."""

from .message import AGENT_PATTERN, TIMESTAMP_PATTERN, PROTOCOL_VERSION, STATUSES


def schema() -> dict:
    uuid = {"type": "string", "pattern": r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![\s\S])"}
    text = {"type": "string", "pattern": r"\S"}
    strings = {"type": "array", "items": text}
    nullable_uuid = {"anyOf": [uuid, {"type": "null"}]}
    context = {"blockers": strings, "risks": strings, "next_action": {"type": "string"}}
    payloads = {
        "state": {"state": {"type": "object"}, **context},
        "decision": {"rationale": text, "requires_approval": {"type": "boolean"}},
        "approval": {"rationale": text},
        "task": {"title": text, "description": text,
                 "approval_ids": {"type": "array", "items": uuid, "uniqueItems": True}},
        "handoff": {"instructions": text, "context": {"type": "object"}, **context},
        "report": {"result": text, "artifacts": strings, **context},
        "acknowledgment": {},
    }
    properties = {
        "protocol_version": {"const": PROTOCOL_VERSION},
        "message_id": uuid, "project_id": uuid, "kind": {"enum": list(STATUSES)},
        "source": {"type": "string", "pattern": AGENT_PATTERN},
        "destination": {"anyOf": [{"type": "string", "pattern": AGENT_PATTERN}, {"type": "null"}]},
        "correlation_id": uuid, "task_id": nullable_uuid, "in_reply_to": nullable_uuid,
        "timestamp": {"type": "string", "pattern": TIMESTAMP_PATTERN, "format": "date-time"},
        "status": {"type": "string"}, "summary": text,
        "payload": {"type": "object"}, "extensions": {"type": "object"},
    }
    branches = []
    for kind, fields in payloads.items():
        constrained = {
            "status": {"enum": list(STATUSES[kind])},
            "payload": {"type": "object", "properties": fields,
                        "required": list(fields), "additionalProperties": False},
        }
        if kind != "state":
            constrained["destination"] = {"type": "string", "pattern": AGENT_PATTERN}
        if kind in ("task", "handoff", "report"):
            constrained["task_id"] = uuid
        elif kind in ("state", "decision", "approval"):
            constrained["task_id"] = {"type": "null"}
        constrained["in_reply_to"] = uuid if kind in ("handoff", "report", "approval", "acknowledgment") else {"type": "null"}
        branches.append({"if": {"properties": {"kind": {"const": kind}}},
                         "then": {"properties": constrained}})
    for requires, status in ((True, "awaiting_approval"), (False, "accepted")):
        branches.append({
            "if": {"properties": {"kind": {"const": "decision"},
                                  "payload": {"properties": {"requires_approval": {"const": requires}}}}},
            "then": {"properties": {"status": {"const": status}}},
        })
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:agent-bridge:protocol:0.1",
        "title": "Agent Bridge v0.1 message", "type": "object",
        "properties": properties, "required": list(properties),
        "additionalProperties": False, "allOf": branches,
    }
