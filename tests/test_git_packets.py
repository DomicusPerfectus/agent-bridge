"""Focused hostile parser inputs; real Git delivery is tested separately."""

from pathlib import Path
import unittest
from uuid import uuid4

from agent_bridge import BridgeError, Message
from agent_bridge.protocol import json_dumps
from agent_bridge.transport.git_backend import endpoint
from agent_bridge.transport.git_packets import packet_blobs, paths, read_packets


class PacketBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.message = Message.create(project_id=str(uuid4()), kind="state", source="agent-a", summary="Sample",
            status="active", payload={"state": {}, "blockers": [], "risks": [], "next_action": ""})

    def reader(self, rows):
        class BlobSource:
            def run(inner, args, **kwargs):
                if args[0] == "ls-tree":
                    return b"".join(f"100644 blob {oid} {len(blob)}\t{path}".encode("ascii") + b"\0"
                                    for oid, path, blob in rows)
                return b"".join(f"{oid} blob {len(blob)}\n".encode("ascii") + blob + b"\n" for oid, _, blob in rows)
        return BlobSource()

    def rows(self):
        return [(str(index) * 40, path, blob) for index, (path, blob) in enumerate(
            zip(paths(self.message.project_id, self.message.message_id), packet_blobs(self.message, None)), 1)]

    def test_valid_packet_reader(self):
        packets = read_packets(self.reader(self.rows()), "0" * 40, self.message.project_id)
        self.assertEqual(packets[self.message.message_id], (self.message, None))

    def test_duplicate_tree_paths_are_rejected(self):
        rows = self.rows()
        rows.append(("3" * 40, rows[0][1], b"{\"conflicting\":true}"))
        with self.assertRaisesRegex(BridgeError, "Duplicate"):
            read_packets(self.reader(rows), "0" * 40, self.message.project_id)

    def test_traversal_tree_paths_are_rejected(self):
        rows = self.rows()
        rows[0] = (rows[0][0], rows[0][1].replace("/messages/", "/../messages/"), rows[0][2])
        with self.assertRaisesRegex(BridgeError, "unsafe filename"):
            read_packets(self.reader(rows), "0" * 40, self.message.project_id)

    def test_duplicate_json_keys_are_rejected(self):
        rows = self.rows()
        rows[0] = (rows[0][0], rows[0][1], b'{"project_id":"first","project_id":"second"}')
        with self.assertRaisesRegex(BridgeError, "Duplicate JSON"):
            read_packets(self.reader(rows), "0" * 40, self.message.project_id)

    def test_malformed_urls_and_ports_fail_cleanly(self):
        for value in ("https://[broken", "ssh://example.invalid:notaport/r", "https://example.invalid:99999/r"):
            with self.subTest(value=value), self.assertRaisesRegex(BridgeError, "Malformed"):
                endpoint(value, Path.cwd())
