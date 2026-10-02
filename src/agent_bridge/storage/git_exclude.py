"""Protect runtime data with a local, Git-resolved exclusion; never edit an index."""

import os
from pathlib import Path
import shutil
import time
import warnings

from ..protocol import BridgeError


def protect_runtime(root: Path) -> None:
    # Avoid making Git a requirement for local-only projects.
    if not any((parent / ".git").exists() for parent in (root, *root.parents)):
        return
    if not shutil.which("git"):
        warnings.warn("Git is unavailable: .agentbridge/ exclusion could not be verified. "
                      "Keep runtime data out of staging and rerun init when Git is available.",
                      RuntimeWarning, stacklevel=2)
        return
    # Delayed imports avoid a storage/backend import cycle.
    from .filesystem import _safe
    from ..transport.git_backend import GitBackend, GitError

    git = GitBackend(root, bare=False)
    runtime = root / ".agentbridge"
    lock = None
    acquired = False
    try:
        top = Path(git.run(["rev-parse", "--show-toplevel"]).decode("utf-8").strip()).resolve()
        relative = runtime.relative_to(top).as_posix()
        if any(ord(char) < 32 for char in relative):
            raise BridgeError("Runtime path contains unsupported control characters")
        tracked = git.run(["ls-files", "-z", "--", ":(top,literal)" + relative + "/"],
                          env_extra={"GIT_OPTIONAL_LOCKS": "0"})
        if tracked:
            raise BridgeError("Runtime files are already tracked; review and untrack them explicitly")

        def ignored():
            try:
                git.run(["check-ignore", "--quiet", "--", runtime.as_posix() + "/"],
                        env_extra={"GIT_OPTIONAL_LOCKS": "0"})
                return True
            except GitError as exc:
                if exc.code == 1:
                    return False
                raise

        if ignored():
            return
        # Git resolves info/exclude to the common directory for linked worktrees.
        exclude = Path(git.run(["rev-parse", "--path-format=absolute", "--git-path", "info/exclude"])
                       .decode("utf-8").strip())
        if not exclude.is_absolute():
            raise BridgeError("Git returned a relative exclusion path")
        _safe(exclude.parent)
        _safe(exclude)
        exclude.parent.mkdir(exist_ok=True)
        lock = exclude.with_name(exclude.name + ".agentbridge.lock")
        _safe(lock)
        deadline = time.monotonic() + 5.0
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                acquired = True
                with os.fdopen(fd, "w", encoding="ascii") as stream:
                    stream.write(str(os.getpid()))
                break
            except FileExistsError as exc:
                if time.monotonic() >= deadline:
                    raise BridgeError("Local exclusion is locked; confirm the writer has stopped before recovery") from exc
                time.sleep(0.05)
        if not ignored():
            # Preserve existing bytes, including comments and line endings.
            _safe(exclude)
            existing = exclude.read_bytes() if exclude.exists() else b""
            escaped = "".join("\\" + char if char in "\\[]*? !#" else char for char in relative)
            addition = ((b"\n" if existing and not existing.endswith(b"\n") else b"")
                        + b"# Agent Bridge local runtime (agentbridge init)\n"
                        + ("/" + escaped + "/\n").encode("utf-8"))
            with exclude.open("ab") as stream:
                stream.write(addition)
                stream.flush()
                os.fsync(stream.fileno())
        if not ignored():
            raise BridgeError("A higher-priority ignore rule overrides the local runtime exclusion")
    except (BridgeError, OSError, ValueError) as exc:
        raise BridgeError(f"WARNING: runtime protection could not be established; initialization stopped: {exc}") from exc
    finally:
        if acquired:
            lock.unlink()
