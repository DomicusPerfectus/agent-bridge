from .base import AgentAdapter, GenericAdapter
from .chatgpt import ChatGPTAdapter
from .codex import CodexAdapter

ADAPTERS = {adapter.name: adapter for adapter in (GenericAdapter(), CodexAdapter(), ChatGPTAdapter())}

__all__ = ["AgentAdapter", "GenericAdapter", "CodexAdapter", "ChatGPTAdapter", "ADAPTERS"]
