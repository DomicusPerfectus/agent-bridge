"""Offline Git E2E: two project directories/processes and a real bare remote."""

import argparse
import json
from pathlib import Path
import subprocess
import sys


def command(arguments, cwd):
    result = subprocess.run([str(a) for a in arguments], cwd=cwd, capture_output=True,
                            text=True, encoding="utf-8", timeout=120, shell=False)
    if result.returncode:
        raise RuntimeError(f"Demo command failed: {arguments[0]}\n{result.stderr}")
    return result.stdout


def cli(root, *arguments):
    return json.loads(command([sys.executable, "-m", "agent_bridge", "--root", root, *arguments], root))


def demo(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("Choose an empty demo directory; existing projects are never overwritten")
    remote = root / "mailbox.git"
    a, b = root / "clone-a", root / "clone-b"
    command(["git", "init", "--bare", remote], root)
    for clone in (a, b):
        command(["git", "clone", remote, clone], root)
    project = cli(a, "init", "--project", "Synthetic Git demo")
    cli(b, "init", "--project", "Synthetic Git demo", "--project-id", project["project_id"])
    for clone in (a, b):
        cli(clone, "git", "init", "--repo", clone)
    task = cli(a, "task", "--from", "chatgpt:planner", "--to", "codex:worker", "--title", "Check sample",
               "--description", "Return a synthetic validation result")
    handoff = cli(a, "handoff", task["task_id"], "--from", "chatgpt:planner", "--to", "codex:worker",
                  "--instructions", "Validate the synthetic sample and report")
    cli(a, "git", "publish", "--push")
    print("Clone A published task and handoff to the local bare remote")
    cli(b, "git", "sync")
    inbox = cli(b, "inbox", "--agent", "codex:worker", "--kind", "handoff")
    if not any(m["message_id"] == handoff["message_id"] for m in inbox):
        raise RuntimeError("Clone B did not receive the handoff")
    cli(b, "acknowledge", handoff["message_id"], "--by", "codex:worker")
    report = cli(b, "report", task["task_id"], "--from", "codex:worker", "--result", "Synthetic sample passed",
                 "--next-action", "Creator should review and acknowledge")
    cli(b, "git", "publish", "--push")
    print("Clone B fetched, acknowledged and published its structured report")
    cli(a, "git", "fetch")
    reports = cli(a, "inbox", "--agent", "chatgpt:planner", "--kind", "report")
    if not any(m["message_id"] == report["message_id"] for m in reports):
        raise RuntimeError("Clone A did not receive the report")
    cli(a, "acknowledge", report["message_id"], "--by", "chatgpt:planner")
    cli(a, "git", "sync", "--push")
    cli(b, "git", "sync")
    for clone in (a, b):
        status = cli(clone, "status")
        if status["tasks"][0]["status"] != "completed":
            raise RuntimeError("Task is not completed in both clones")
        repeated = cli(clone, "git", "sync")
        if repeated["ingested"] or repeated["published_to_cache"]:
            raise RuntimeError("Repeated sync was not idempotent")
        # Transport publication never stages artifacts or changes the code checkout.
        if command(["git", "status", "--porcelain"], clone).strip():
            # Empty clones have no tracked .gitignore; runtime data is an untracked directory.
            tracked = command(["git", "diff", "--name-only"], clone)
            staged = command(["git", "diff", "--cached", "--name-only"], clone)
            if tracked or staged:
                raise RuntimeError("Transport modified the code checkout/index")
    result = {"status": "PASS", "transport": "git", "protocol_version": "0.1",
              "task_status": "completed", "clones": 2, "separate_cli_processes": True,
              "idempotent_sync": True, "network_required": False}
    print(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(".validation/git-demo"))
    demo(parser.parse_args().root.resolve())
