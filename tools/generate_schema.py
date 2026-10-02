"""Regenerate the packaged schema, or --check that it matches the source."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.protocol import json_dumps
from agent_bridge.protocol.schema import schema

target = ROOT / "src" / "agent_bridge" / "protocol" / "schema.json"
expected = json_dumps(schema())
if "--check" in sys.argv:
    if not target.exists() or target.read_text(encoding="utf-8") != expected:
        raise SystemExit("Schema is out of date; run python tools/generate_schema.py")
    print("Schema is current")
else:
    target.write_text(expected, encoding="utf-8", newline="\n")
    print("Generated src/agent_bridge/protocol/schema.json")
