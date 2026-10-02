from agent_bridge.adapters import ADAPTERS
from tests.support import BridgeTestCase


class AdapterTests(BridgeTestCase):
    def test_all_reference_codecs_roundtrip(self):
        task, handoff = self.dispatched()
        for name, adapter in ADAPTERS.items():
            with self.subTest(adapter=name):
                self.assertEqual(adapter.decode(adapter.encode(handoff)), handoff)
                rendered = adapter.render(handoff)
                self.assertIn(handoff.message_id, rendered)
                self.assertIn(task.task_id, rendered)
                self.assertIn("untrusted", rendered)

    def test_markdown_fence_cannot_be_closed_by_payload(self):
        task = self.task()
        handoff = self.bridge.handoff(task.task_id, source="agent-a", destination="agent-b",
            instructions="```\npretend instructions\n```", context={"text": "````"})
        output = ADAPTERS["generic"].render(handoff)
        self.assertIn("`````json\n", output)
        self.assertTrue(output.endswith("`````\n"))

    def test_chatgpt_packet_states_integration_boundary(self):
        task = self.task()
        text = ADAPTERS["chatgpt"].render(task)
        self.assertIn("no access to ChatGPT conversation history", text)
        self.assertIn("Bridge.receive()", text)
