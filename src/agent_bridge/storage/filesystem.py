"""A small append-only log with one lock and atomic file publication."""

from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from uuid import uuid4

from ..protocol import BridgeError, Message, PROTOCOL_VERSION, json_dumps, json_loads
from ..protocol.message import MAX_MESSAGE_BYTES, identifier, nonempty, timestamp, utc_now

EVENT_FILE = re.compile(r"^(\d{12})_([0-9a-f-]{36})\.json$")


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    if os.name != "nt":
        return False
    try:
        metadata = os.lstat(path)  # Inspect the link itself, including dangling junctions.
    except FileNotFoundError:
        return False  # Future storage paths are valid during initialization.
    # These Windows metadata fields/constants exist since Python 3.8. Reject
    # link tags specifically, not unrelated reparse types such as cloud files.
    return (metadata.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT != 0
            and metadata.st_reparse_tag in (stat.IO_REPARSE_TAG_MOUNT_POINT,
                                           stat.IO_REPARSE_TAG_SYMLINK))


def _safe(path: Path) -> None:
    if _is_link_or_junction(path):
        raise BridgeError(f"Symlink/junction is not allowed in bridge storage: {path.name}")


def read_json_file(path: Path):
    _safe(path)
    if path.stat().st_size > MAX_MESSAGE_BYTES:
        raise BridgeError(f"File exceeds the 1 MiB limit: {path.name}")
    try:
        return json_loads(path.read_text(encoding="utf-8-sig"))
    except UnicodeError as exc:
        raise BridgeError(f"Invalid UTF-8: {path.name}") from exc


def _atomic_write(path: Path, data: dict) -> None:
    _safe(path)
    text = json_dumps(data)
    if len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise BridgeError("File exceeds the 1 MiB limit")
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class FileSystemStore:
    def __init__(self, root: Path | str, *, lock_timeout: float = 5.0):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise BridgeError("Project root must be an existing directory")
        self.data = self.root / ".agentbridge"
        self.log = self.data / "messages"
        self.lock_timeout = lock_timeout
        self._in_transaction = False
        self._guard()

    def _guard(self) -> None:
        _safe(self.data)
        _safe(self.log)
        _safe(self.data / "config.json")
        _safe(self.data / ".lock")

    @classmethod
    def initialize(cls, root: Path | str, name: str, project_id: str | None = None) -> "FileSystemStore":
        nonempty(name, "project_name")
        if project_id is not None:
            identifier(project_id, "project_id")
        store = cls(root)
        from .git_exclude import protect_runtime
        protect_runtime(store.root)
        store.data.mkdir(exist_ok=True, mode=0o700)
        with store.transaction():
            path = store.data / "config.json"
            if path.exists():
                config = store.config()  # Init never overwrites an existing identity.
                if project_id is not None and config["project_id"] != project_id:
                    raise BridgeError("Existing bridge has a different project_id")
                if not store.log.is_dir():
                    raise BridgeError("Initialized bridge is missing its messages directory")
            else:
                if store.log.exists() and any(store.log.iterdir()):
                    raise BridgeError("Refusing to initialize over an orphaned message log")
                store.log.mkdir(exist_ok=True, mode=0o700)
                _atomic_write(path, {
                    "protocol_version": PROTOCOL_VERSION,
                    "project_id": project_id or str(uuid4()), "project_name": name,
                    "created_at": utc_now(), "storage": "filesystem", "transport": "local",
                })
        return store

    @contextmanager
    def transaction(self):
        self._guard()
        if not self.data.is_dir():
            raise BridgeError("Bridge is not initialized; run agentbridge init")
        if self._in_transaction:
            raise BridgeError("Nested storage transactions are not supported")
        lock = self.data / ".lock"
        deadline = time.monotonic() + self.lock_timeout
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                break
            except FileExistsError as exc:
                if time.monotonic() >= deadline:
                    raise BridgeError("Bridge is locked; wait for the writer. For a stale lock, see SECURITY.md") from exc
                time.sleep(0.05)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                stream.write(str(os.getpid()))
            self._in_transaction = True
            yield
        finally:
            self._in_transaction = False
            lock.unlink()

    def _require_transaction(self) -> None:
        if not self._in_transaction:
            raise BridgeError("Storage reads and writes require transaction()")
        self._guard()

    def config(self) -> dict:
        self._require_transaction()
        path = self.data / "config.json"
        if not path.is_file():
            raise BridgeError("Bridge is not initialized; run agentbridge init")
        data = read_json_file(path)
        expected = {"protocol_version", "project_id", "project_name", "created_at", "storage", "transport"}
        if not isinstance(data, dict) or set(data) != expected:
            raise BridgeError("Invalid bridge configuration fields")
        identifier(data["project_id"], "project_id")
        nonempty(data["project_name"], "project_name")
        if data["protocol_version"] != PROTOCOL_VERSION:
            raise BridgeError("Unsupported configuration protocol version")
        if data["storage"] != "filesystem" or data["transport"] != "local":
            raise BridgeError("v0.1 supports filesystem storage and local transport")
        timestamp(data["created_at"], "created_at")
        return data

    def read(self) -> list[Message]:
        self._require_transaction()
        if not self.log.is_dir():
            raise BridgeError("Missing messages directory")
        result = []
        for path in sorted(self.log.iterdir()):
            if path.name.startswith(".pending-"):
                continue  # A crashed unpublished write is never visible.
            match = EVENT_FILE.fullmatch(path.name)
            if not match or not path.is_file() or int(match[1]) != len(result) + 1:
                raise BridgeError(f"Invalid log filename or sequence: {path.name}")
            message = Message.from_dict(read_json_file(path))
            if message.message_id != match[2]:
                raise BridgeError(f"Message ID differs from filename: {path.name}")
            result.append(message)
        return result

    def append(self, message: Message) -> None:
        self._require_transaction()
        message.validate()
        messages = self.read()
        if any(item.message_id == message.message_id for item in messages):
            raise BridgeError("Duplicate message ID in storage")
        path = self.log / f"{len(messages) + 1:012d}_{message.message_id}.json"
        _atomic_write(path, message.to_dict())
