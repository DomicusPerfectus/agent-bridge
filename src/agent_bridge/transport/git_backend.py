"""Constrained Git plumbing. Never checks out, merges, or runs repository hooks."""

import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
from threading import Lock, Thread
import time
from urllib.parse import urlsplit

from ..protocol import BridgeError
from ..storage.filesystem import _safe

OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
REMOTE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,127}")


class GitError(BridgeError):
    def __init__(self, operation: str, code: int):
        self.operation = operation
        self.code = code
        super().__init__(f"Git {operation} failed (exit {code}); check access, authentication and branch configuration. Raw Git output is withheld to protect credentials.")


def validate_branch(value: str) -> str:
    if (not isinstance(value, str) or not BRANCH.fullmatch(value) or ".." in value
            or "//" in value or value.endswith(("/", "."))
            or any(part.startswith(".") or part.endswith(".lock") for part in value.split("/"))):
        raise BridgeError("Invalid Git transport branch")
    return value


def endpoint(value: str, repo: Path, *, require_exists: bool = True) -> str:
    """Accept HTTPS, SSH/scp or an existing local repository; never URL secrets."""
    if not isinstance(value, str) or not value or value.startswith("-") or any(ord(c) < 32 for c in value):
        raise BridgeError("Invalid Git remote endpoint")
    if re.match(r"^[A-Za-z]:[\\/]", value) or ":" not in value:
        target = Path(value)
        if not target.is_absolute():
            target = repo / target
        _safe(target)
        target = target.resolve()
        if require_exists and not target.is_dir():
            raise BridgeError("Local Git remote must be an existing directory")
        return str(target)
    try:
        parts = urlsplit(value)
        parts.port  # Reject invalid/out-of-range ports as ordinary input errors.
    except ValueError as exc:
        raise BridgeError("Malformed Git endpoint") from exc
    if parts.scheme in ("https", "ssh"):
        if not parts.hostname or parts.password or parts.query or parts.fragment:
            raise BridgeError("Git endpoints cannot contain passwords, queries or fragments")
        if parts.scheme == "https" and parts.username:
            raise BridgeError("Use Git authentication helpers, not HTTPS URL credentials")
        return value
    if "://" in value or "::" in value:
        raise BridgeError("Unsupported Git endpoint protocol")
    if re.fullmatch(r"(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9.-]+:[A-Za-z0-9_./~-]+", value):
        return value  # Conventional SSH scp-style address, including git@host:path.
    raise BridgeError("Supported Git endpoints are HTTPS, SSH and local directories")


class GitBackend:
    def __init__(self, directory: Path, *, bare: bool = True, timeout: float = 30.0):
        _safe(directory)
        self.directory = directory.resolve()
        self.bare = bare
        self.timeout = timeout
        self.executable = shutil.which("git")

    def run(self, args: list[str], *, data: bytes | None = None,
            limit: int = 4_194_304, allowed: tuple[int, ...] = (0,), env_extra: dict | None = None) -> bytes:
        if not self.executable:
            raise BridgeError("Git executable was not found")
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("GIT_") or key in ("GIT_SSH", "GIT_SSH_COMMAND", "GIT_ASKPASS")}
        env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "LC_ALL": "C",
                    "GIT_NO_REPLACE_OBJECTS": "1"})
        if env_extra:
            env.update(env_extra)
        operation = "push" if args[:1] == ["-c"] and "push" in args else args[0]
        command = [self.executable, "--no-pager",
                   "-c", "core.hooksPath=" + str(self.directory / "agentbridge-no-hooks"),
                   "-c", "core.fsmonitor=false", "-c", "commit.gpgSign=false",
                   "-c", "gc.auto=0", "-c", "maintenance.auto=false",
                   "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
                   "-c", "protocol.ssh.allow=always", "-c", "protocol.file.allow=always",
                   "-c", "http.followRedirects=false", "-c", "fetch.recurseSubmodules=false",
                   "-c", "core.attributesFile=", "-c", "core.commitGraph=false"]
        command += ["--git-dir", str(self.directory)] if self.bare else ["-C", str(self.directory)]
        command += args
        output = bytearray()
        overflow: list[bool] = []
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, shell=False,
                                       start_new_session=os.name != "nt",
                                       creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
                                       if os.name == "nt" else 0)
        except OSError as exc:
            raise BridgeError("Unable to start the Git executable") from exc

        stopping = Lock()
        stopped = False

        def stop():
            nonlocal stopped
            with stopping:
                if stopped:
                    return
                stopped = True
                if os.name == "nt":
                    if process.poll() is None:
                        taskkill = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/taskkill.exe"
                        try:
                            subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           timeout=2, shell=False, creationflags=subprocess.CREATE_NO_WINDOW)
                        except (OSError, subprocess.TimeoutExpired):
                            pass
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass

        def drain(stream, maximum, keep=False):
            size = 0
            try:
                while chunk := stream.read(65536):
                    size += len(chunk)
                    if size > maximum:
                        overflow.append(True)
                        stop()
                        break
                    if keep:
                        output.extend(chunk)
            finally:
                stream.close()

        def feed():
            try:
                with process.stdin:
                    process.stdin.write(data)
            except (BrokenPipeError, OSError):
                pass

        readers = [Thread(target=drain, args=(process.stdout, limit, True), daemon=True),
                   Thread(target=drain, args=(process.stderr, 65536), daemon=True)]
        for thread in readers:
            thread.start()
        writer = Thread(target=feed, daemon=True) if data is not None else None
        if writer:
            writer.start()
        timed_out = False
        threads = readers + ([writer] if writer else [])
        try:
            process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            stop()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired as exc:
                raise BridgeError(f"Git {operation} could not be terminated during timeout cleanup") from exc
        finally:
            # Also reap on KeyboardInterrupt or an unexpected wait failure.
            if process.poll() is None:
                stop()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired as exc:
                    raise BridgeError(f"Git {operation} could not be reaped during interruption cleanup") from exc
            deadline = time.monotonic() + 2
            for thread in threads:
                thread.join(timeout=max(0, deadline - time.monotonic()))
            if any(thread.is_alive() for thread in threads):
                stop()
                deadline = time.monotonic() + 2
                for thread in threads:
                    thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            raise BridgeError(f"Git {operation} pipe cleanup exceeded its bound")
        if timed_out:
            raise BridgeError(f"Git {operation} timed out after {self.timeout:g} seconds; process cleanup completed")
        if overflow:
            raise BridgeError(f"Git {operation} exceeded its output bound")
        if process.returncode not in allowed:
            raise GitError(operation, process.returncode)
        return bytes(output)

    def oid(self, ref: str) -> str | None:
        try:
            result = self.run(["rev-parse", "--verify", ref]).decode("ascii").strip()
        except GitError as exc:
            if exc.code in (1, 128):
                return None
            raise
        if not OID.fullmatch(result):
            raise BridgeError("Git returned an invalid object ID")
        return result

    def update(self, ref: str, value: str, old: str | None = None):
        if not OID.fullmatch(value):
            raise BridgeError("Invalid commit ID")
        self.run(["update-ref", ref, value, old or "0" * len(value)])

    def ancestor(self, old: str, new: str) -> bool:
        try:
            self.run(["merge-base", "--is-ancestor", old, new])
            return True
        except GitError as exc:
            if exc.code == 1:
                return False
            raise
