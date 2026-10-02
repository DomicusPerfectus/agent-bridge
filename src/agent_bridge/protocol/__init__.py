"""Canonical envelopes and validation; independent of storage and providers."""

from .message import BridgeError, Message, PROTOCOL_VERSION, json_dumps, json_loads

__all__ = ["BridgeError", "Message", "PROTOCOL_VERSION", "json_dumps", "json_loads"]
