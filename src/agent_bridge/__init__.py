"""Agent Bridge: structured state between agents, with no provider dependency."""

__version__ = "0.2.1"

from .advisory import SelectionAdvisor, SelectionResult, TwoPassSelector
from .bridge import Bridge
from .protocol import BridgeError, Message

__all__ = [
    "Bridge",
    "BridgeError",
    "Message",
    "SelectionAdvisor",
    "SelectionResult",
    "TwoPassSelector",
    "__version__",
]
