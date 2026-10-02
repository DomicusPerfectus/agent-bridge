from unittest.mock import patch

from tests.git_support import GitTestCase


class GitStatusTests(GitTestCase):
    def status(self, bridge):
        with patch.object(bridge.transport.git, "run", wraps=bridge.transport.git.run) as calls:
            result = bridge.transport.status(bridge)
        self.assertFalse(any(call.args[0][0] in ("fetch", "ls-remote", "push") for call in calls.call_args_list))
        self.assertNotIn("unpublished_local_messages", result)
        self.assertNotIn("cached_message_count", result)
        return result

    def test_no_push_cache_then_push_and_accepted_remote_are_distinct(self):
        self.task_a()
        before = self.status(self.ba)
        self.assertEqual(before["local_event_count"], 1)
        self.assertEqual(before["local_events_not_cached"], 1)
        self.assertEqual(before["cached_packet_count"], 0)
        self.assertEqual(before["local_events_pending_remote_delivery"], 1)
        self.assertFalse(before["remote_snapshot_known"])
        self.ba.transport.publish(self.ba, push=False)
        cached = self.status(self.ba)
        self.assertEqual(cached["local_events_not_cached"], 0)
        self.assertEqual(cached["cached_packet_count"], 1)
        self.assertEqual(cached["accepted_remote_packet_count"], 0)
        self.assertEqual(cached["cached_packets_pending_remote_delivery"], 1)
        self.assertEqual(cached["local_events_pending_remote_delivery"], 1)
        self.ba.sync(push=True)
        pushed = self.status(self.ba)
        self.assertTrue(pushed["remote_snapshot_known"])
        self.assertEqual(pushed["accepted_remote_packet_count"], 1)
        self.assertEqual(pushed["cached_commit"], pushed["accepted_remote_commit"])
        self.assertEqual(pushed["cached_packets_pending_remote_delivery"], 0)
        self.assertEqual(pushed["local_events_pending_remote_delivery"], 0)
        self.ba.sync(push=False)
        self.assertEqual(self.status(self.ba), pushed)

    def test_fetch_only_accepts_remote_without_claiming_cache_or_delivery_pending(self):
        self.task_a()
        self.ba.sync(push=True)
        self.bb.transport.fetch(self.bb)
        fetched = self.status(self.bb)
        self.assertEqual(fetched["local_event_count"], 1)
        self.assertEqual(fetched["local_events_not_cached"], 1)
        self.assertEqual(fetched["cached_packet_count"], 0)
        self.assertEqual(fetched["accepted_remote_packet_count"], 1)
        self.assertEqual(fetched["cached_packets_pending_remote_delivery"], 0)
        self.assertEqual(fetched["local_events_pending_remote_delivery"], 0)
        self.bb.sync(push=False)
        self.assertEqual(self.status(self.bb)["local_events_not_cached"], 0)
