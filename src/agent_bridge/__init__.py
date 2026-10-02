"""Agent Bridge: structured state between agents, with no provider dependency."""

__version__ = "0.2.0"

from .bridge import Bridge
from .protocol import BridgeError, Message

__all__ = ["Bridge", "BridgeError", "Message", "__version__"]
