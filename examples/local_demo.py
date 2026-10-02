"""Exercise the installed CLI using only synthetic local data."""

import argparse
import json
from pathlib import Path
import subprocess
import sys


def run(root: Path, *arguments) -> dict | list:
    result = subprocess.run([sys.executable, "-m", "agent_bridge", "--root", str(root), *arguments],
                            check=True, capture_output=True, text=True, encoding="utf-8")
    return json.loads(result.stdout)


def demo(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    run(root, "init", "--project", "Synthetic handoff demo")
    task = run(root, "task", "--from", "agent-a", "--to", "agent-b", "--title", "Check a sample",
               "--description", "Return a synthetic validation result; no external system is used.")
    print("Agent A created a task")
    handoff = run(root, "handoff", task["task_id"], "--from", "agent-a", "--to", "agent-b",
                  "--instructions", "Validate the sample and report the outcome.")
    inbox = run(root, "inbox", "--agent", "agent-b", "--kind", "handoff")
    assert any(m["message_id"] == handoff["message_id"] for m in inbox), "Agent B did not receive handoff"
    print("Bridge stored the handoff; Agent B read it")
    run(root, "acknowledge", handoff["message_id"], "--by", "agent-b")
    report = run(root, "report", task["task_id"], "--from", "agent-b", "--result",
                 "Synthetic sample validated successfully.", "--next-action", "Review and acknowledge the report.")
    reports = run(root, "inbox", "--agent", "agent-a", "--kind", "report")
    assert any(m["message_id"] == report["message_id"] for m in reports), "Agent A did not receive report"
    print("Agent B wrote a report; Agent A read it")
    run(root, "acknowledge", report["message_id"], "--by", "agent-a")
    snapshot = run(root, "status")
    completed = next(t for t in snapshot["tasks"] if t["task_id"] == task["task_id"])
    assert completed["status"] == "completed", completed
    print("Agent A acknowledged the report; task completed")
    result = {"status": "PASS", "task_id": task["task_id"], "task_status": completed["status"]}
    print(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, default=Path(".validation/demo"))
    demo(p.parse_args().root.resolve())
