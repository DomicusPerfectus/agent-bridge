"""Portable Git artifacts and deterministic causal ordering, not a working tree."""

import heapq
import re

from ..protocol import BridgeError, Message, json_dumps, json_loads
from ..protocol.message import MAX_MESSAGE_BYTES, identifier
from .git_backend import GitBackend, OID

PREFIX = ".agentbridge-remote/projects/"
UUID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
PACKET_PATH = re.compile(rf"{re.escape(PREFIX)}({UUID_PATTERN})/(messages|links)/({UUID_PATTERN})\.json")
MAX_PACKETS = 2048
MAX_TOTAL_BYTES = 33_554_432


def paths(project_id: str, message_id: str) -> tuple[str, str]:
    identifier(project_id, "project_id")
    identifier(message_id, "message_id")
    base = f"{PREFIX}{project_id}"
    return f"{base}/messages/{message_id}.json", f"{base}/links/{message_id}.json"


def validate_link(data, message_id: str) -> str | None:
    if not isinstance(data, dict) or set(data) != {"format", "message_id", "after"} or data["format"] != "agentbridge-git/1":
        raise BridgeError("Invalid Git dependency sidecar")
    if data["message_id"] != message_id:
        raise BridgeError("Git sidecar ID differs from its path")
    if data["after"] is not None:
        identifier(data["after"], "after")
    return data["after"]


def read_packets(git: GitBackend, commit: str | None, project_id: str) -> dict[str, tuple[Message, str | None]]:
    if commit is None:
        return {}
    listing = git.run(["ls-tree", "-rlz", commit, "--", ".agentbridge-remote"])
    selected = []
    seen_paths = set()
    total = 0
    for row in listing.split(b"\0"):
        if not row:
            continue
        try:
            header, raw_path = row.split(b"\t", 1)
            mode, kind, oid, raw_size = header.split()
            path = raw_path.decode("ascii")
            match = PACKET_PATH.fullmatch(path)
            size = int(raw_size)
        except (UnicodeError, ValueError) as exc:
            raise BridgeError("Invalid Git transport tree entry") from exc
        if not match or mode != b"100644" or kind != b"blob" or not OID.fullmatch(oid.decode("ascii")):
            raise BridgeError("Git transport contains an unsafe filename, symlink or unsupported entry")
        if path in seen_paths:
            raise BridgeError("Duplicate Git transport artifact path")
        seen_paths.add(path)
        if size < 0 or size > MAX_MESSAGE_BYTES or (match[2] == "links" and size > 4096):
            raise BridgeError("Git transport artifact exceeds its size limit")
        if match[1] != project_id:
            continue  # Other valid project namespaces are never ingested or exported.
        total += size
        selected.append((oid.decode("ascii"), size, match[2], match[3]))
    if len(selected) > MAX_PACKETS * 2 or total > MAX_TOTAL_BYTES:
        raise BridgeError("Git project exceeds the v0.2 packet/byte limit")
    if not selected:
        return {}
    batch = git.run(["cat-file", "--batch"], data=("\n".join(item[0] for item in selected) + "\n").encode("ascii"),
                    limit=MAX_TOTAL_BYTES + MAX_PACKETS * 256)
    offset = 0
    messages = {}
    links = {}
    for oid, size, kind, message_id in selected:
        end = batch.find(b"\n", offset)
        if end == -1 or batch[offset:end] != f"{oid} blob {size}".encode("ascii"):
            raise BridgeError("Invalid Git object response")
        start = end + 1
        raw = batch[start:start + size]
        offset = start + size + 1
        if batch[start + size:offset] != b"\n":
            raise BridgeError("Incomplete Git object response")
        try:
            data = json_loads(raw.decode("utf-8-sig"))
        except UnicodeError as exc:
            raise BridgeError("Git artifact is not UTF-8 JSON") from exc
        if kind == "messages":
            message = Message.from_dict(data)
            if message.project_id != project_id or message.message_id != message_id:
                raise BridgeError("Git envelope project/message ID differs from its path")
            messages[message_id] = message
        else:
            links[message_id] = validate_link(data, message_id)
    if offset != len(batch) or messages.keys() != links.keys():
        raise BridgeError("Every Git envelope requires exactly one dependency sidecar")
    return {key: (message, links[key]) for key, message in messages.items()}


def combine(*collections) -> dict:
    result = {}
    for packets in collections:
        for key, packet in packets.items():
            if key in result and (result[key][0].to_dict() != packet[0].to_dict() or result[key][1] != packet[1]):
                raise BridgeError(f"Conflicting Git packet or dependency sidecar: {key}")
            result[key] = packet
    return result


def ordered(packets: dict, known: set[str]) -> list[Message]:
    """Topological sort using local predecessor, parent and approval links only."""
    dependencies = {}
    children = {}
    for key, (message, after) in packets.items():
        deps = {value for value in (after, message.in_reply_to) if value is not None}
        if message.kind == "task":
            deps.update(message.payload["approval_ids"])
        for dep in deps:
            if dep not in packets and dep not in known:
                raise BridgeError(f"Missing Git message dependency: {dep}")
        dependencies[key] = {dep for dep in deps if dep in packets}
        for dep in dependencies[key]:
            children.setdefault(dep, []).append(key)
    ready = [key for key, deps in dependencies.items() if not deps]
    heapq.heapify(ready)
    result = []
    while ready:
        key = heapq.heappop(ready)
        result.append(packets[key][0])
        for child in children.get(key, []):
            dependencies[child].remove(key)
            if not dependencies[child]:
                heapq.heappush(ready, child)
    if len(result) != len(packets):
        raise BridgeError("Git packet dependencies contain a cycle")
    return result


def packet_blobs(message: Message, after: str | None) -> tuple[bytes, bytes]:
    return (json_dumps(message.to_dict()).encode("utf-8"),
            json_dumps({"format": "agentbridge-git/1", "message_id": message.message_id, "after": after}).encode("utf-8"))
