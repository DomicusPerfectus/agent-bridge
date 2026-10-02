"""Small, scriptable CLI. JSON on stdout; errors on stderr with nonzero exit."""

import argparse
from pathlib import Path
import sys

from . import __version__
from .adapters import ADAPTERS, GenericAdapter
from .adapters.base import markdown_json
from .bridge import Bridge
from .protocol import BridgeError, Message, json_dumps, json_loads
from .protocol.message import MAX_MESSAGE_BYTES
from .storage.filesystem import read_json_file


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentbridge", description="Structured local handoffs between agents")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--root", type=Path, default=Path.cwd(), help="Project directory (default: current directory)")
    p.add_argument("--format", choices=("json", "markdown"), default="json", help="Output format")
    commands = p.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Initialize an ignored local .agentbridge directory")
    init.add_argument("--project", default="Agent Bridge", help="Public or local project label")
    commands.add_parser("status", help="Project state, decisions and projected task statuses")
    state = commands.add_parser("state", help="Record current structured project state")
    state.add_argument("--from", dest="source", required=True)
    state.add_argument("--summary", required=True)
    state.add_argument("--data", type=Path, required=True, help="JSON object file containing structured state")
    state.add_argument("--status", choices=("active", "blocked"), default="active")
    _context_flags(state)
    decision = commands.add_parser("decision", help="Record a decision or request human approval")
    _routing_flags(decision)
    decision.add_argument("--summary", required=True)
    decision.add_argument("--rationale", required=True)
    decision.add_argument("--requires-approval", action="store_true")
    approve = commands.add_parser("approve", help="Record the addressed human's decision")
    approve.add_argument("message_id")
    approve.add_argument("--by", dest="actor", required=True, help="Addressed human:<name> identifier")
    approve.add_argument("--outcome", choices=("approved", "rejected"), required=True)
    approve.add_argument("--rationale", required=True)
    task = commands.add_parser("task", help="Create a pending task")
    _routing_flags(task)
    task.add_argument("--title", required=True)
    task.add_argument("--description", required=True)
    task.add_argument("--approval", dest="approval_ids", action="append", default=[], help="Decision message_id (repeatable)")
    handoff = commands.add_parser("handoff", help="Dispatch a task with a structured context snapshot")
    handoff.add_argument("task_id")
    _routing_flags(handoff)
    handoff.add_argument("--instructions", required=True)
    handoff.add_argument("--context", type=Path, help="Explicit JSON object; default is current state, decisions and task")
    _context_flags(handoff)
    report = commands.add_parser("report", help="Write a result from the assigned agent")
    report.add_argument("task_id")
    report.add_argument("--from", dest="source", required=True)
    report.add_argument("--result", required=True)
    report.add_argument("--status", choices=("succeeded", "failed", "blocked"), default="succeeded")
    report.add_argument("--artifact", dest="artifacts", action="append", default=[])
    _context_flags(report)
    inbox = commands.add_parser("inbox", help="Read messages addressed to an agent")
    inbox.add_argument("--agent", required=True)
    inbox.add_argument("--all", dest="include_acknowledged", action="store_true")
    inbox.add_argument("--kind", choices=("state", "task", "decision", "approval", "handoff", "report", "acknowledgment"))
    ack = commands.add_parser("acknowledge", help="Acknowledge receipt; successful report acknowledgment completes its task")
    ack.add_argument("message_id")
    ack.add_argument("--by", dest="actor", required=True)
    show = commands.add_parser("show", help="Read one canonical stored envelope")
    show.add_argument("message_id")
    export = commands.add_parser("export", help="Render an agent-compatible Markdown packet to stdout")
    export.add_argument("message_id")
    export.add_argument("--adapter", choices=tuple(ADAPTERS), default="generic")
    ingest = commands.add_parser("import", help="Validate and ingest a canonical JSON envelope")
    ingest.add_argument("file", help="JSON file or - for stdin")
    return p


def _routing_flags(p):
    p.add_argument("--from", dest="source", required=True)
    p.add_argument("--to", dest="destination", required=True)


def _context_flags(p):
    p.add_argument("--blocker", dest="blockers", action="append", default=[])
    p.add_argument("--risk", dest="risks", action="append", default=[])
    p.add_argument("--next-action", default="")


def execute(args):
    if args.command == "init":
        return Bridge.initialize(args.root, args.project).status()
    bridge = Bridge.open(args.root)
    if args.command == "status":
        return bridge.status()
    if args.command == "state":
        return bridge.state(source=args.source, summary=args.summary, state=read_json_file(args.data),
                            status=args.status, blockers=args.blockers, risks=args.risks, next_action=args.next_action)
    if args.command == "decision":
        return bridge.decision(source=args.source, destination=args.destination, summary=args.summary,
                               rationale=args.rationale, requires_approval=args.requires_approval)
    if args.command == "approve":
        return bridge.approve(args.message_id, actor=args.actor, outcome=args.outcome, rationale=args.rationale)
    if args.command == "task":
        return bridge.task(source=args.source, destination=args.destination, title=args.title,
                           description=args.description, approval_ids=args.approval_ids)
    if args.command == "handoff":
        context = read_json_file(args.context) if args.context else None
        return bridge.handoff(args.task_id, source=args.source, destination=args.destination,
                              instructions=args.instructions, context=context, blockers=args.blockers,
                              risks=args.risks, next_action=args.next_action)
    if args.command == "report":
        return bridge.report(args.task_id, source=args.source, result=args.result, status=args.status,
                             artifacts=args.artifacts, blockers=args.blockers, risks=args.risks,
                             next_action=args.next_action)
    if args.command == "inbox":
        messages = bridge.inbox(args.agent, include_acknowledged=args.include_acknowledged)
        return [m for m in messages if args.kind is None or m.kind == args.kind]
    if args.command == "acknowledge":
        return bridge.acknowledge(args.message_id, actor=args.actor)
    if args.command == "show":
        return bridge.show(args.message_id)
    if args.command == "export":
        return ADAPTERS[args.adapter].render(bridge.show(args.message_id))
    if args.command == "import":
        if args.file == "-":
            text = sys.stdin.read(MAX_MESSAGE_BYTES + 1)
            message = GenericAdapter().decode(text)
        else:
            message = Message.from_dict(read_json_file(Path(args.file)))
        return bridge.receive(message)
    raise BridgeError("Unknown command")


def _plain(value):
    if isinstance(value, Message):
        return value.to_dict()
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        result = execute(args)
        if args.command == "export":
            output = result
        elif args.format == "markdown":
            output = GenericAdapter().render(result) if isinstance(result, Message) else "# Agent Bridge\n\n" + markdown_json(_plain(result))
        else:
            output = json_dumps(_plain(result))
        sys.stdout.write(output)
        return 0
    except (BridgeError, OSError, UnicodeError) as exc:
        print(f"agentbridge: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
