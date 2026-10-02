"""Real, offline Git fixtures. Only synthetic data and repository-local scratch."""

import json
from pathlib import Path
import subprocess

from agent_bridge import Bridge
from agent_bridge.protocol import json_dumps
from agent_bridge.transport.git_backend import GitBackend
from agent_bridge.transport.git_packets import packet_blobs, paths
from tests.support import BridgeTestCase


class GitTestCase(BridgeTestCase):
    def setUp(self):
        super().setUp()
        self.remote = self.root / "remote.git"
        self.a, self.b = self.root / "a", self.root / "b"
        self.command("git", "init", "--bare", self.remote)
        for clone in (self.a, self.b):
            self.command("git", "clone", self.remote, clone)
        self.ba = Bridge.initialize(self.a, "Synthetic shared project")
        self.project_id = self.ba.status()["project_id"]
        self.bb = Bridge.initialize(self.b, "Synthetic shared project", project_id=self.project_id)
        self.ba.configure_git(self.a)
        self.bb.configure_git(self.b)

    def command(self, *args):
        result = subprocess.run([str(arg) for arg in args], cwd=self.root, capture_output=True,
                                text=True, encoding="utf-8", timeout=60, shell=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def task_a(self, **kwargs):
        return self.ba.task(source="agent-a", destination="agent-b", title="Synthetic task",
                            description="No external data", **kwargs)

    def packet(self, message, after=None):
        return dict(zip(paths(message.project_id, message.message_id), packet_blobs(message, after)))

    def inject(self, artifacts: dict, *, remove=(), reset=False):
        git = GitBackend(self.remote)
        old = git.oid("refs/heads/agentbridge")
        index = self.root / "fixture-index"
        if index.exists():
            index.unlink()
        env = {"GIT_INDEX_FILE": str(index)}
        git.run(["read-tree", old] if old and not reset else ["read-tree", "--empty"], env_extra=env)
        for path in remove:
            git.run(["update-index", "--index-info"], data=f"0 {'0' * 40}\t{path}\n".encode("ascii"), env_extra=env)
        for path, value in artifacts.items():
            mode, data = value if isinstance(value, tuple) else ("100644", value)
            oid = git.run(["hash-object", "-w", "--stdin"], data=data).decode("ascii").strip()
            git.run(["update-index", "--add", "--cacheinfo", f"{mode},{oid},{path}"], env_extra=env)
        tree = git.run(["write-tree"], env_extra=env).decode("ascii").strip()
        author = {"GIT_AUTHOR_NAME": "Synthetic fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                  "GIT_COMMITTER_NAME": "Synthetic fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}
        args = ["commit-tree", tree] + (["-p", old] if old and not reset else [])
        commit = git.run(args, data=b"Synthetic fixture\n", env_extra=author).decode("ascii").strip()
        git.update("refs/heads/agentbridge", commit, old)
        return commit
