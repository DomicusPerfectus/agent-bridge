"""Delivery is a view of the shared log, not another mutable queue."""

from typing import Protocol, Sequence

from ..protocol import Message
from ..protocol.message import agent


class Transport(Protocol):
    def inbox(self, messages: Sequence[Message], destination: str,
              *, include_acknowledged: bool = False) -> list[Message]: ...


class LocalTransport:
    def inbox(self, messages: Sequence[Message], destination: str,
              *, include_acknowledged: bool = False) -> list[Message]:
        agent(destination)
        received = {m.in_reply_to for m in messages
                    if m.kind == "acknowledgment" and m.source == destination}
        return [m for m in messages if m.destination == destination
                and (include_acknowledged or m.message_id not in received)]
