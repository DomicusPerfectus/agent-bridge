from pathlib import Path
from unittest.mock import patch
import warnings

from agent_bridge import Bridge, BridgeError
from agent_bridge.transport.git_backend import GitBackend, GitError
from tests.support import BridgeTestCase


class RuntimeProtectionTests(BridgeTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        template = self.root / "template"
        template.mkdir()
        self.git = GitBackend(self.repo, bare=False)
        self.git.run(["init", "-b", "main", "--template=" + str(template)])
        self.exclude = Path(self.git.run(["rev-parse", "--path-format=absolute", "--git-path", "info/exclude"])
                            .decode().strip())
        self.exclude.parent.mkdir(exist_ok=True)

    def test_exclusion_preserves_bytes_config_index_and_tracked_gitignore(self):
        existing = b"# Existing comment\r\nlocal-notes/"  # No final newline.
        self.exclude.write_bytes(existing)
        tracked_ignore = self.repo / ".gitignore"
        tracked_ignore.write_text("other-cache/\n", encoding="utf-8")
        self.git.run(["add", ".gitignore"])
        index = self.git.run(["ls-files", "--stage"])
        config = (self.repo / ".git/config").read_bytes()
        head = (self.repo / ".git/HEAD").read_bytes()
        Bridge.initialize(self.repo)
        protected = self.exclude.read_bytes()
        self.assertTrue(protected.startswith(existing + b"\n"))
        self.assertIn(b"/.agentbridge/\n", protected)
        self.assertEqual(index, self.git.run(["ls-files", "--stage"]))
        self.assertEqual(config, (self.repo / ".git/config").read_bytes())
        self.assertEqual(head, (self.repo / ".git/HEAD").read_bytes())
        self.assertEqual(tracked_ignore.read_text(encoding="utf-8"), "other-cache/\n")
        Bridge.initialize(self.repo)
        self.assertEqual(self.exclude.read_bytes(), protected)
        self.git.run(["add", "-A"])
        self.assertEqual(index, self.git.run(["ls-files", "--stage"]))

    def test_nested_special_path_is_escaped_and_scoped(self):
        nested = self.repo / "[draft] 1!"
        nested.mkdir()
        Bridge.initialize(nested)
        self.git.run(["check-ignore", "--quiet", str(nested / ".agentbridge/config.json")])
        with self.assertRaises(GitError) as result:
            self.git.run(["check-ignore", "--quiet", str(self.repo / ".agentbridge/config.json")])
        self.assertEqual(result.exception.code, 1)
        self.assertIn(b"/\\[draft\\]\\ 1\\!/.agentbridge/", self.exclude.read_bytes())

    def test_linked_worktree_uses_git_resolved_common_exclude(self):
        (self.repo / "sample.txt").write_text("Generic fixture\n", encoding="utf-8")
        self.git.run(["add", "sample.txt"])
        author = {"GIT_AUTHOR_NAME": "Fixture", "GIT_COMMITTER_NAME": "Fixture",
                  "GIT_AUTHOR_EMAIL": "fixture@example.invalid", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}
        self.git.run(["commit", "-m", "Generic fixture"], env_extra=author)
        linked = self.root / "linked"
        self.git.run(["worktree", "add", "-b", "fixture-linked", str(linked)])
        peer = GitBackend(linked, bare=False)
        before = peer.run(["rev-parse", "HEAD"])
        Bridge.initialize(linked)
        common = Path(peer.run(["rev-parse", "--path-format=absolute", "--git-path", "info/exclude"])
                      .decode().strip())
        self.assertEqual(common.resolve(), self.exclude.resolve())
        peer.run(["add", "-A"])
        self.assertEqual(peer.run(["rev-parse", "HEAD"]), before)
        self.assertEqual(peer.run(["diff", "--cached", "--name-only"]), b"")
        self.assertEqual(peer.run(["status", "--porcelain"]), b"")

    def test_already_ignored_runtime_needs_no_metadata_change(self):
        (self.repo / ".gitignore").write_text(".agentbridge/\n", encoding="utf-8")
        self.exclude.write_bytes(b"# Preserve exactly\n")
        Bridge.initialize(self.repo)
        self.assertEqual(self.exclude.read_bytes(), b"# Preserve exactly\n")

    def test_tracked_runtime_fails_before_creating_configuration(self):
        runtime = self.repo / ".agentbridge"
        runtime.mkdir()
        (runtime / "legacy.txt").write_text("Generic fixture", encoding="utf-8")
        self.git.run(["add", ".agentbridge/legacy.txt"])
        before = self.git.run(["ls-files", "--stage"])
        with self.assertRaisesRegex(BridgeError, "WARNING.*already tracked"):
            Bridge.initialize(self.repo)
        self.assertFalse((runtime / "config.json").exists())
        self.assertEqual(before, self.git.run(["ls-files", "--stage"]))

    def test_higher_priority_unignore_fails_closed(self):
        (self.repo / ".gitignore").write_text("!/.agentbridge/\n", encoding="utf-8")
        with self.assertRaisesRegex(BridgeError, "WARNING.*higher-priority"):
            Bridge.initialize(self.repo)
        self.assertFalse((self.repo / ".agentbridge").exists())
        self.assertFalse(self.exclude.with_name("exclude.agentbridge.lock").exists())

    def test_exclusion_write_failure_warns_and_releases_lock(self):
        self.exclude.write_bytes(b"# Keep this\n")
        original = Path.open
        def deny_write(path, mode="r", *args, **kwargs):
            if path == self.exclude and mode == "ab":
                raise PermissionError("Fixture read-only exclusion")
            return original(path, mode, *args, **kwargs)
        with patch.object(Path, "open", new=deny_write):
            with self.assertRaisesRegex(BridgeError, "WARNING.*read-only exclusion"):
                Bridge.initialize(self.repo)
        self.assertEqual(self.exclude.read_bytes(), b"# Keep this\n")
        self.assertFalse(self.exclude.with_name("exclude.agentbridge.lock").exists())
        self.assertFalse((self.repo / ".agentbridge").exists())

    def test_git_unavailable_warns_but_local_mode_continues(self):
        with patch("agent_bridge.storage.git_exclude.shutil.which", return_value=None):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                bridge = Bridge.initialize(self.repo)
        self.assertEqual(bridge.status()["message_count"], 0)
        self.assertEqual(len(caught), 1)
        self.assertIn("exclusion could not be verified", str(caught[0].message))
