from pathlib import Path
from datetime import datetime
import tempfile
import unittest

from jsonschema import Draft202012Validator, FormatChecker

from agent_bridge import Bridge
from agent_bridge.protocol.schema import schema

ROOT = Path(__file__).resolve().parents[1]


def schema_validator():
    checker = FormatChecker()
    # jsonschema's optional RFC3339 dependency need not be installed offline.
    # The schema's UTC pattern and this stdlib calendar check cover our format.
    @checker.checks("date-time", raises=ValueError)
    def date_time(value):
        if isinstance(value, str):
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    return Draft202012Validator(schema(), format_checker=checker)


class BridgeTestCase(unittest.TestCase):
    def setUp(self):
        workspace = ROOT / ".validation" / "tests"
        workspace.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=workspace)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bridge = Bridge.initialize(self.root, "Synthetic test project")

    def task(self, **kwargs):
        return self.bridge.task(source="agent-a", destination="agent-b", title="Sample task",
                                description="Validate a synthetic sample", **kwargs)

    def dispatched(self):
        task = self.task()
        handoff = self.bridge.handoff(task.task_id, source="agent-a", destination="agent-b",
                                      instructions="Validate and report")
        return task, handoff

    def working(self):
        task, handoff = self.dispatched()
        self.bridge.acknowledge(handoff.message_id, actor="agent-b")
        return task, handoff
