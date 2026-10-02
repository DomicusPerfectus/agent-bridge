from importlib.resources import files
import json
import unittest
from uuid import uuid4

from jsonschema import Draft202012Validator

from agent_bridge.protocol import BridgeError, Message, json_dumps, json_loads
from agent_bridge.protocol.message import MAX_MESSAGE_BYTES
from agent_bridge.protocol.schema import schema
from tests.support import schema_validator


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.message = Message.create(project_id=str(uuid4()), kind="task", source="agent-a",
            destination="agent-b", task_id=str(uuid4()), summary="Sample task", status="pending",
            payload={"title": "Sample task", "description": "Synthetic data", "approval_ids": []})
        self.validator = schema_validator()

    def test_roundtrip_and_schema(self):
        data = self.message.to_dict()
        self.assertEqual(Message.from_dict(json_loads(json_dumps(data))), self.message)
        self.validator.validate(data)
        Draft202012Validator.check_schema(schema())

    def test_distributed_schema_is_current(self):
        packaged = files("agent_bridge.protocol").joinpath("schema.json").read_text(encoding="utf-8")
        self.assertEqual(json.loads(packaged), schema())

    def test_rejects_invalid_envelopes_in_runtime_and_schema(self):
        mutations = [
            {"protocol_version": "99"}, {"message_id": "../outside"},
            {"source": "../agent"}, {"destination": None}, {"task_id": None},
            {"source": "agent-a\n"}, {"message_id": self.message.message_id + "\n"},
            {"kind": "execute-shell"}, {"status": "completed"},
            {"timestamp": "2026-02-30T00:00:00Z"}, {"timestamp": "2026-10-02T00:00:00+02:00"},
            {"summary": "  "}, {"payload": {}}, {"extensions": []}, {"unexpected": "value"},
        ]
        for fields in mutations:
            with self.subTest(fields=fields):
                data = self.message.to_dict()
                data.update(fields)
                with self.assertRaises(BridgeError):
                    Message.from_dict(data)
                self.assertFalse(self.validator.is_valid(data))

    def test_payloads_are_strict_and_duplicates_rejected(self):
        for value in ({"title": "x", "description": 1, "approval_ids": []},
                      {"title": "x", "description": "x", "approval_ids": [str(uuid4()), "invalid"]},
                      {"title": "x", "description": "x", "approval_ids": [], "command": "ignored"}):
            data = self.message.to_dict()
            data["payload"] = value
            with self.subTest(value=value), self.assertRaises(BridgeError):
                Message.from_dict(data)
        for text in ('{"key":1,"key":2}', '{"number":NaN}', '{"number":Infinity}', '{broken}'):
            with self.subTest(text=text), self.assertRaises(BridgeError):
                json_loads(text)

    def test_ids_are_linked_and_canonical(self):
        data = self.message.to_dict()
        data["correlation_id"] = str(uuid4())
        with self.assertRaisesRegex(BridgeError, "task_id as correlation_id"):
            Message.from_dict(data)
        data = self.message.to_dict()
        data["project_id"] = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
        with self.assertRaises(BridgeError):
            Message.from_dict(data)

    def test_limits_and_json_types(self):
        with self.assertRaisesRegex(BridgeError, "1 MiB"):
            json_loads(" " * (MAX_MESSAGE_BYTES + 1))
        data = self.message.to_dict()
        data["extensions"] = {"x": (1, 2)}
        with self.assertRaises(BridgeError):
            Message.from_dict(data)
        data["extensions"] = {"x": float("nan")}
        with self.assertRaises(BridgeError):
            Message.from_dict(data)
        data["extensions"] = {"x": "x" * MAX_MESSAGE_BYTES}
        with self.assertRaisesRegex(BridgeError, "1 MiB"):
            Message.from_dict(data)
        deep = {}
        cursor = deep
        for _ in range(70):
            cursor["nested"] = {}
            cursor = cursor["nested"]
        data["extensions"] = deep
        with self.assertRaisesRegex(BridgeError, "64"):
            Message.from_dict(data)

    def test_decision_status_matches_boolean(self):
        decision = Message.create(project_id=str(uuid4()), kind="decision", source="agent-a",
            destination="human:reviewer", summary="Boundary", status="awaiting_approval",
            payload={"rationale": "Needs review", "requires_approval": True})
        self.validator.validate(decision.to_dict())
        data = decision.to_dict()
        data["status"] = "accepted"
        with self.assertRaises(BridgeError):
            Message.from_dict(data)
        self.assertFalse(self.validator.is_valid(data))

    def test_external_extensions_and_defensive_copies(self):
        data = self.message.to_dict()
        data["extensions"] = {"org.example.hermes": {"queue": "sample"}}
        decoded = Message.from_dict(data)
        data["extensions"]["org.example.hermes"]["queue"] = "changed"
        self.assertEqual(decoded.extensions["org.example.hermes"]["queue"], "sample")
        self.validator.validate(decoded.to_dict())
