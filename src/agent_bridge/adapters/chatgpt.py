from ..protocol import Message
from .base import GenericAdapter


class ChatGPTAdapter(GenericAdapter):
    """A ChatGPT-compatible packet for manual use or a future app/MCP/API."""

    name = "chatgpt"

    def render(self, message: Message) -> str:
        return (super().render(message) +
                "\n## ChatGPT integration boundary\n\n"
                "Use this structured packet as project context. Return a canonical Agent "
                "Bridge JSON envelope when proposing a task, decision or report. Retain the "
                "project_id; reports retain task_id and correlation_id and reply to the "
                "handoff message_id. An authorized integration can submit the JSON with "
                "Bridge.receive(), or a user can import a JSON file with agentbridge import. "
                "A human:<name> approval record must come from the addressed human. "
                "This adapter has no access to ChatGPT conversation history.\n")
