from .base import AgentAdapter, GenericAdapter
from .chatgpt import ChatGPTAdapter
from .codex import CodexAdapter
from .hermes import HermesAdapter

ADAPTERS = {
    adapter.name: adapter
    for adapter in (GenericAdapter(), CodexAdapter(), ChatGPTAdapter(), HermesAdapter())
}

__all__ = [
    "ADAPTERS",
    "AgentAdapter",
    "ChatGPTAdapter",
    "CodexAdapter",
    "GenericAdapter",
    "HermesAdapter",
]
