"""Git as immutable packet delivery; the local Bridge log remains application state."""

from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import time

from ..protocol import BridgeError, json_dumps
from ..protocol.message import identifier
from ..storage.filesystem import _atomic_write, _safe, read_json_file
from .local import LocalTransport
from .git_backend import GitBackend, GitError, OID, REMOTE, endpoint, validate_branch
from .git_packets import MAX_PACKETS, MAX_TOTAL_BYTES, combine, ordered, packet_blobs, paths, read_packets

LOCAL_REF = "refs/heads/agentbridge-local"
FETCH_REF = "refs/agentbridge/fetched"
ACCEPTED_REF = "refs/agentbridge/accepted"


class GitTransport(LocalTransport):
    """Implements the existing inbox contract and explicit sync/fetch/publication."""

    def __init__(self, root: Path | str, config: dict):
        self.root = Path(root).resolve()
        self.data = self.root / ".agentbridge"
        self.cache = self.data / "git-cache.git"
        self.config = config
        self._guard()
        self.git = GitBackend(self.cache)

    def _guard(self):
        for path in (self.data, self.data / "git.json", self.cache, self.cache / "config",
                     self.cache / "objects", self.cache / "refs", self.data / ".git-sync.lock"):
            _safe(path)

    @classmethod
    def open(cls, root: Path | str):
        root = Path(root).resolve()
        _safe(root / ".agentbridge")
        config = read_json_file(root / ".agentbridge" / "git.json")
        fields = {"format", "project_id", "repo", "git_dir", "remote", "endpoint", "branch", "push", "object_format"}
        if not isinstance(config, dict) or set(config) != fields or config["format"] != "agentbridge-git/1":
            raise BridgeError("Invalid Git transport configuration")
        identifier(config["project_id"], "project_id")
        if not isinstance(config["remote"], str) or not REMOTE.fullmatch(config["remote"]):
            raise BridgeError("Invalid Git remote name")
        validate_branch(config["branch"])
        if type(config["push"]) is not bool or config["object_format"] not in ("sha1", "sha256"):
            raise BridgeError("Invalid Git push policy or object format")
        for field in ("repo", "git_dir"):
            if not isinstance(config[field], str) or not Path(config[field]).is_absolute():
                raise BridgeError("Git repository paths must be absolute")
        if endpoint(config["endpoint"], Path(config["repo"]), require_exists=False) != config["endpoint"]:
            raise BridgeError("Git endpoint must be normalized")
        return cls(root, config)

    @staticmethod
    def _repository(repo: Path | str, remote: str) -> tuple[Path, Path, str, str]:
        if not isinstance(remote, str) or not REMOTE.fullmatch(remote):
            raise BridgeError("Invalid Git remote name")
        repo = Path(repo)
        _safe(repo)
        repo = repo.resolve()
        if not repo.is_dir():
            raise BridgeError("Configured Git repository does not exist")
        _safe(repo / ".git")
        peer = GitBackend(repo, bare=False)
        raw_dir = peer.run(["rev-parse", "--absolute-git-dir"]).decode("utf-8").strip()
        git_dir = Path(raw_dir)
        _safe(git_dir)
        _safe(git_dir / "config")
        git_dir = git_dir.resolve()
        raw = peer.run(["config", "--file", str(git_dir / "config"), "--no-includes",
                        "--get-all", f"remote.{remote}.url"]).decode("utf-8").splitlines()
        if len(raw) != 1:
            raise BridgeError("Git transport requires exactly one explicitly configured remote URL")
        url = endpoint(raw[0], repo)
        object_format = peer.run(["rev-parse", "--show-object-format"]).decode("ascii").strip()
        if object_format not in ("sha1", "sha256"):
            raise BridgeError("Unsupported Git object format")
        return repo, git_dir, url, object_format

    @classmethod
    def configure(cls, bridge, repo: Path | str, *, remote="origin", branch="agentbridge", push=False):
        validate_branch(branch)
        if type(push) is not bool:
            raise BridgeError("push must be a boolean")
        repo, git_dir, url, object_format = cls._repository(repo, remote)
        snapshot = bridge.status()
        config = {"format": "agentbridge-git/1", "project_id": snapshot["project_id"],
                  "repo": str(repo), "git_dir": str(git_dir), "remote": remote,
                  "endpoint": url, "branch": branch, "push": push, "object_format": object_format}
        path = bridge.store.data / "git.json"
        _safe(path)
        if path.exists():
            transport = cls.open(bridge.store.root)
            if transport.config != config:
                raise BridgeError("Existing Git transport differs; review configuration in a separate project directory")
            return transport
        transport = cls(bridge.store.root, config)
        with transport._lock():
            if path.exists():
                raise BridgeError("Git transport was configured concurrently; retry init")
            template = transport.data / "git-empty-template"
            _safe(template)
            template.mkdir(exist_ok=True)
            if any(template.iterdir()):
                raise BridgeError("Git template must be empty")
            if transport.cache.exists():
                raise BridgeError("Orphaned Git cache exists; review it before configuring transport")
            transport.git.run(["init", "--bare", f"--object-format={object_format}",
                               f"--template={template}", str(transport.cache)])
            with bridge.store.transaction():
                if bridge.store.config()["project_id"] != config["project_id"]:
                    raise BridgeError("Project identity changed during Git configuration")
                _atomic_write(path, config)
        return transport

    @contextmanager
    def _lock(self):
        self._guard()
        path = self.data / ".git-sync.lock"
        deadline = time.monotonic() + 5.0
        while True:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                break
            except FileExistsError as exc:
                if time.monotonic() >= deadline:
                    raise BridgeError("Git sync is locked by another operation; retry after it finishes") from exc
                time.sleep(0.05)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                stream.write(str(os.getpid()))
            yield
        finally:
            path.unlink()

    def _check(self, bridge):
        self._guard()
        current = self.open(self.root).config
        if current != self.config or bridge.status()["project_id"] != self.config["project_id"]:
            raise BridgeError("Git transport configuration or project identity changed")
        repo, git_dir, url, object_format = self._repository(self.config["repo"], self.config["remote"])
        if (str(repo), str(git_dir), url, object_format) != (self.config["repo"], self.config["git_dir"], self.config["endpoint"], self.config["object_format"]):
            raise BridgeError("Git repository or remote was substituted; review the pinned configuration")
        if not self.cache.is_dir():
            raise BridgeError("Git cache is missing")
        # Remote trees never become configuration. Also reject poisoned local cache config.
        names = self.git.run(["config", "--file", str(self.cache / "config"), "--no-includes",
                              "--name-only", "--list"]).decode("ascii").splitlines()
        allowed = {"core.repositoryformatversion", "core.filemode", "core.bare",
                   "core.logallrefupdates", "core.symlinks", "core.ignorecase", "extensions.objectformat"}
        if any(name not in allowed for name in names):
            raise BridgeError("Git cache configuration contains unsupported executable or transport settings")

    def status(self, bridge) -> dict:
        with self._lock():
            self._check(bridge)
            commit = self.git.oid(LOCAL_REF)
            packets = read_packets(self.git, commit, self.config["project_id"])
            events = bridge.events()
            return {"transport": "git", "project_id": self.config["project_id"],
                    "repo": self.config["repo"], "remote": self.config["remote"], "branch": self.config["branch"],
                    "push_by_default": self.config["push"], "cached_commit": commit,
                    "accepted_remote_commit": self.git.oid(ACCEPTED_REF),
                    "cached_message_count": len(packets), "local_message_count": len(events),
                    "unpublished_local_messages": sum(m.message_id not in packets for m in events)}

    def _fetch(self) -> str | None:
        ref = "refs/heads/" + self.config["branch"]
        advertised = self.git.run(["ls-remote", "--heads", self.config["endpoint"], ref], limit=4096)
        if not advertised.strip():
            if self.git.oid(ACCEPTED_REF):
                raise BridgeError("BLOCKED: previously observed remote branch was deleted")
            return None
        lines = advertised.decode("ascii").splitlines()
        if len(lines) != 1 or lines[0].split()[1:] != [ref] or not OID.fullmatch(lines[0].split()[0]):
            raise BridgeError("Invalid Git remote branch advertisement")
        self.git.run(["fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head",
                      self.config["endpoint"], f"{ref}:{FETCH_REF}"])
        commit = self.git.oid(FETCH_REF)
        if commit is None:
            raise BridgeError("Fetched Git branch has no commit")
        accepted = self.git.oid(ACCEPTED_REF)
        if accepted and not self.git.ancestor(accepted, commit):
            raise BridgeError("BLOCKED: remote transport history was rewritten")
        return commit

    def _ingest(self, bridge, remote_commit):
        project_id = self.config["project_id"]
        remote = read_packets(self.git, remote_commit, project_id)
        accepted = self.git.oid(ACCEPTED_REF)
        previous = read_packets(self.git, accepted, project_id)
        if not previous.keys() <= remote.keys():
            raise BridgeError("BLOCKED: a published envelope was removed remotely")
        combine(previous, remote)  # Reject changed envelopes or dependency sidecars.
        cached = read_packets(self.git, self.git.oid(LOCAL_REF), project_id)
        packets = combine(remote, cached)
        existing = {m.message_id: m for m in bridge.events()}
        batch = ordered(packets, set(existing))
        bridge.validate_batch(batch)  # No ingestion when any candidate is malformed/invalid.
        added = 0
        for message in batch:
            bridge.receive(message)  # Same validation boundary as local imports.
            added += message.message_id not in existing
        if remote_commit:
            self.git.update(ACCEPTED_REF, remote_commit, accepted)
        return remote, packets, added

    def _publish(self, bridge, remote_commit, remote, known):
        packets = dict(known)
        previous = None
        new = 0
        for message in bridge.events():
            if message.message_id in packets:
                if packets[message.message_id][0].to_dict() != message.to_dict():
                    raise BridgeError("Local and Git envelopes conflict")
            else:
                packets[message.message_id] = (message, previous)
                new += 1
            previous = message.message_id
        if len(packets) > MAX_PACKETS or sum(len(a) + len(b) for m, after in packets.values() for a, b in [packet_blobs(m, after)]) > MAX_TOTAL_BYTES:
            raise BridgeError("Git project exceeds the v0.2 packet/byte limit")
        bridge.validate_batch(ordered(packets, {m.message_id for m in bridge.events()}))
        local_commit = self.git.oid(LOCAL_REF)
        base = remote_commit or local_commit
        base_packets = remote if remote_commit else read_packets(self.git, local_commit, self.config["project_id"])
        with tempfile.TemporaryDirectory(prefix="git-index-", dir=self.data) as directory:
            env = {"GIT_INDEX_FILE": str(Path(directory) / "index")}
            self.git.run(["read-tree", base] if base else ["read-tree", "--empty"], env_extra=env)
            for message_id in sorted(packets.keys() - base_packets.keys()):
                message, after = packets[message_id]
                for path, blob in zip(paths(message.project_id, message_id), packet_blobs(message, after)):
                    oid = self.git.run(["hash-object", "-w", "--stdin"], data=blob).decode("ascii").strip()
                    if not OID.fullmatch(oid):
                        raise BridgeError("Invalid Git blob ID")
                    self.git.run(["update-index", "--add", "--cacheinfo", f"100644,{oid},{path}"], env_extra=env)
            tree = self.git.run(["write-tree"], env_extra=env).decode("ascii").strip()
        if not OID.fullmatch(tree):
            raise BridgeError("Invalid Git tree ID")
        if remote_commit and tree == self.git.oid(remote_commit + "^{tree}"):
            commit = remote_commit
        elif local_commit and tree == self.git.oid(local_commit + "^{tree}") and (not remote_commit or self.git.ancestor(remote_commit, local_commit)):
            commit = local_commit
        else:
            args = ["commit-tree", tree]
            for parent in dict.fromkeys(p for p in (remote_commit, local_commit) if p):
                args += ["-p", parent]
            author = {"GIT_AUTHOR_NAME": "Agent Bridge", "GIT_AUTHOR_EMAIL": "agent-bridge@example.invalid",
                      "GIT_COMMITTER_NAME": "Agent Bridge", "GIT_COMMITTER_EMAIL": "agent-bridge@example.invalid"}
            commit = self.git.run(args, data=b"Agent Bridge transport envelopes\n", env_extra=author).decode("ascii").strip()
        self.git.update(LOCAL_REF, commit, local_commit)
        return commit, new, len(packets)

    def fetch(self, bridge) -> dict:
        with self._lock():
            self._check(bridge)
            commit = self._fetch()
            remote, _, added = self._ingest(bridge, commit)
            return {"status": "PASS", "transport": "git", "operation": "fetch", "remote_commit": commit,
                    "remote_message_count": len(remote), "ingested": added, "pushed": False}

    def sync(self, bridge, *, push: bool | None = None) -> dict:
        push = self.config["push"] if push is None else push
        if type(push) is not bool:
            raise BridgeError("push must be a boolean")
        with self._lock():
            self._check(bridge)
            ingested = 0
            published = 0
            for attempt in range(2):
                try:
                    remote_commit = self._fetch()
                    remote, known, added = self._ingest(bridge, remote_commit)
                except BridgeError as exc:
                    if attempt:
                        raise BridgeError(f"BLOCKED: push reconciliation failed: {exc}") from exc
                    raise
                ingested += added
                commit, new, count = self._publish(bridge, remote_commit, remote, known)
                published += new
                if push:
                    try:
                        self.git.run(["-c", "push.gpgSign=false", "-c", "push.followTags=false",
                                      "push", "--porcelain", "--no-verify", "--recurse-submodules=no",
                                      self.config["endpoint"], f"{commit}:refs/heads/{self.config['branch']}"])
                    except GitError as exc:
                        if attempt == 0:
                            continue  # Fetch/reconcile once. No force push or conflict overwrite.
                        raise BridgeError("BLOCKED: push failed after one reconciliation retry; local envelopes are preserved") from exc
                    accepted = self.git.oid(ACCEPTED_REF)
                    self.git.update(ACCEPTED_REF, commit, accepted)
                return {"status": "PASS", "transport": "git", "operation": "sync", "project_id": self.config["project_id"],
                        "remote_commit": remote_commit, "cached_commit": commit, "ingested": ingested,
                        "published_to_cache": published, "cached_message_count": count,
                        "pushed": push, "push_attempts": attempt + 1 if push else 0}
        raise BridgeError("BLOCKED: sync did not complete")

    def publish(self, bridge, *, push: bool | None = None) -> dict:
        result = self.sync(bridge, push=push)
        result["operation"] = "publish"
        return result
