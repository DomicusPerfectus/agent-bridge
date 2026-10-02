"""Constrained Git plumbing. Never checks out, merges, or runs repository hooks."""

import errno
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
from threading import Event, Lock, Thread
import time
from urllib.parse import urlsplit

from ..protocol import BridgeError
from ..storage.filesystem import _safe

if os.name == "nt":
    from ._git_windows import WindowsJob

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
        process = job = None
        threads = []
        cancelled = Event()
        worker_errors = []
        stopping = Lock()
        stopped = False

        def stop():
            nonlocal stopped
            with stopping:
                if stopped:
                    return
                if job is not None:
                    job.terminate()  # Owns descendants even after Git exits.
                elif process is not None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                stopped = True

        def worker(stream, action):
            try:
                with stream:
                    action()
            except OSError as exc:
                # Git may close stdin early. Cancellation errors are expected
                # only after cancellation; other worker failures stay visible.
                expected = stream is process.stdin and exc.errno in (errno.EPIPE, errno.EINVAL)
                expected |= cancelled.is_set() and (exc.errno == errno.EBADF or getattr(exc, "winerror", None) == 995)
                if not expected:
                    worker_errors.append(exc)
            except BaseException as exc:
                worker_errors.append(exc)

        def drain(stream, maximum, keep=False):
            size = 0
            while not cancelled.is_set():
                chunk = stream.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > maximum:
                    overflow.append(True)
                    stop()
                    break
                if keep:
                    output.extend(chunk)

        def feed():
            remaining = memoryview(data)
            while remaining and not cancelled.is_set():
                written = process.stdin.write(remaining)
                if not written:
                    raise OSError("Git stdin write made no progress")
                remaining = remaining[written:]

        def cleanup():
            """One bounded owner of process, tree, workers and all pipe closes."""
            deadline = time.monotonic() + 6
            problems = []

            def attempt(action):
                try:
                    action()
                except OSError as exc:
                    problems.append(exc)

            def join(until):
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(timeout=max(0, until - time.monotonic()))

            try:
                attempt(stop)
                if process is not None:
                    if process.poll() is None:
                        # Also covers failure while assigning a suspended child.
                        attempt(process.kill)
                    try:
                        process.wait(timeout=min(2, max(0, deadline - time.monotonic())))
                    except subprocess.TimeoutExpired as exc:
                        problems.append(exc)
                    join(min(deadline, time.monotonic() + 2))
                    if any(thread.is_alive() for thread in threads):
                        cancelled.set()
                        # Cancellation is asynchronous. Repeat only while a worker
                        # is alive; the worker closes its own stream without racing
                        # a BufferedReader/Writer lock in the caller thread.
                        while any(thread.is_alive() for thread in threads) and time.monotonic() < deadline:
                            if job is not None:
                                for thread in threads:
                                    if thread.is_alive():
                                        attempt(lambda thread=thread: job.cancel_io(thread))
                            join(min(deadline, time.monotonic() + 0.02))
                    if any(thread.is_alive() for thread in threads):
                        problems.append(RuntimeError("Git pipe workers did not terminate"))
                    else:
                        for stream in (process.stdin, process.stdout, process.stderr):
                            if stream is not None:
                                attempt(stream.close)
                    if process.poll() is None:
                        problems.append(RuntimeError("Git process was not reaped"))
                    problems.extend(worker_errors)
                if job is not None:
                    attempt(lambda: job.wait(deadline))
                    paused = Event()
                    try:
                        while job.active() and time.monotonic() < deadline:
                            paused.wait(timeout=min(0.01, max(0, deadline - time.monotonic())))
                        if job.active():
                            problems.append(RuntimeError("Git descendants did not terminate"))
                    except OSError as exc:
                        problems.append(exc)
            finally:
                if job is not None:
                    attempt(job.close)  # Kill-on-close is also the failure fallback.
            if problems:
                raise BridgeError(f"Git {operation} process/pipe cleanup exceeded its bound or failed")

        timed_out = False
        interruption = None
        try:
            try:
                job = WindowsJob() if os.name == "nt" else None
                process = subprocess.Popen(command, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, shell=False,
                                           bufsize=0, start_new_session=os.name != "nt",
                                           creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                                                          | 0x00000004) if os.name == "nt" else 0)  # CREATE_SUSPENDED
                if job is not None:
                    job.attach_and_resume(process)
            except OSError as exc:
                raise BridgeError("Unable to start and contain the Git executable") from exc
            threads = [Thread(target=worker, args=(process.stdout, lambda: drain(process.stdout, limit, True)), daemon=True),
                       Thread(target=worker, args=(process.stderr, lambda: drain(process.stderr, 65536)), daemon=True)]
            if data is not None:
                threads.append(Thread(target=worker, args=(process.stdin, feed), daemon=True))
            for thread in threads:
                thread.start()
            process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        except BaseException as exc:
            interruption = exc
            raise
        finally:
            try:
                cleanup()
            except BridgeError as exc:
                if interruption is None:
                    raise
                interruption.add_note(str(exc))
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
