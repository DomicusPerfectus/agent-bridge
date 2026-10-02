"""Fetch/sync and display Codex packets; acknowledgment and execution stay explicit."""

import argparse
from pathlib import Path

from agent_bridge import Bridge
from agent_bridge.adapters import CodexAdapter

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--agent", default="codex:worker")
    args = parser.parse_args()
    bridge = Bridge.open(args.root)
    bridge.sync(push=False)
    adapter = CodexAdapter()
    for handoff in bridge.inbox(args.agent):
        if handoff.kind == "handoff":
            print(adapter.render(handoff))
