"""Offline packaging/startup check from a committed local clone, inside the repo.

Run with a development Python containing pip and setuptools. No global install,
network call, push, or system configuration change is performed.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run(arguments, cwd, env):
    result = subprocess.run([str(a) for a in arguments], cwd=cwd, env=env,
                            capture_output=True, text=True, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"Command failed: {arguments[0]}\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def verify():
    workspace = ROOT / ".validation"
    workspace.mkdir(exist_ok=True)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INDEX"] = "1"
    env["PYTHONUTF8"] = "1"
    revision = run(["git", "rev-parse", "HEAD"], ROOT, env)
    tracked_changes = run(["git", "status", "--porcelain", "--untracked-files=normal"], ROOT, env)
    if tracked_changes:
        raise RuntimeError("Commit the reviewable source first so the clone validates all changes")
    with tempfile.TemporaryDirectory(prefix="fresh-clone-", dir=workspace) as temporary:
        area = Path(temporary)
        clone = area / "clone"
        temporary_build = area / "tmp"
        temporary_build.mkdir()
        env["TEMP"] = str(temporary_build)
        env["TMP"] = str(temporary_build)
        run(["git", "clone", "--local", "--no-hardlinks", ROOT, clone], ROOT, env)
        if (clone / ".agentbridge").exists():
            raise RuntimeError("Private bridge runtime state leaked into the clone")
        wheels = area / "wheels"
        wheels.mkdir()
        run([sys.executable, "-m", "pip", "wheel", "--no-index", "--no-deps",
             "--no-build-isolation", "--wheel-dir", wheels, "."], clone, env)
        wheel, = wheels.glob("agent_bridge-*.whl")
        venv = clone / ".venv"
        run([sys.executable, "-m", "venv", "--without-pip", venv], clone, env)
        scripts = venv / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        cli = scripts / ("agentbridge.exe" if os.name == "nt" else "agentbridge")
        run([sys.executable, "-m", "pip", "--python", python, "install",
             "--no-index", "--no-deps", wheel], clone, env)
        version = run([cli, "--version"], clone, env)
        # Isolated Python cannot accidentally import the original editable source.
        check = (
            "import json, sys, agent_bridge; from pathlib import Path; "
            "from importlib.resources import files; "
            "from importlib.metadata import distribution; "
            "assert Path(agent_bridge.__file__).is_relative_to(Path(sys.prefix)); "
            "d=distribution('agent-bridge'); assert d.version==agent_bridge.__version__; "
            "assert not d.requires or all('extra ==' in r for r in d.requires); "
            "assert any(str(f).endswith('/LICENSE') for f in d.files); "
            "assert any(str(f).endswith('/NOTICE') for f in d.files); "
            "s=json.loads(files('agent_bridge.protocol').joinpath('schema.json').read_text()); "
            "assert s['title']=='Agent Bridge v0.1 message'; "
            "print('wheel metadata, license, schema and isolated import: PASS')"
        )
        print(run([python, "-I", "-c", check], clone, env))
        initialized = json.loads(run([cli, "init", "--project", "Fresh clone sample"], clone, env))
        if initialized["message_count"] != 0:
            raise RuntimeError("Fresh project is not empty")
        demo_root = clone / ".validation" / "demo"
        output = run([python, "examples/local_demo.py", "--root", demo_root], clone, env)
        print(output)
        status = json.loads(run([cli, "--root", demo_root, "status"], clone, env))
        if len(status["tasks"]) != 1 or status["tasks"][0]["status"] != "completed":
            raise RuntimeError("Fresh clone demo did not complete")
        print(run([python, "examples/git_demo.py", "--root", clone / ".validation" / "git-demo"], clone, env))
        if run(["git", "status", "--porcelain"], clone, env):
            raise RuntimeError("Documented demo created non-ignored files")
        result = {"status": "PASS", "revision": revision, "version": version,
                  "platform": sys.platform, "python": sys.version.split()[0],
                  "fresh_clone": True, "wheel_install": True, "isolated_runtime": True,
                  "packaged_schema": True, "demo_task_status": "completed",
                  "packaged_license": True, "packaged_notice": True,
                  "git_e2e": True,
                  "network_required": False, "runtime_git_status": "clean"}
    (workspace / "fresh-clone-result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    verify()
