from ..protocol import Message
from .base import GenericAdapter


class CodexAdapter(GenericAdapter):
    """A reference prompt/file codec; does not invoke or control Codex."""

    name = "codex"

    def render(self, message: Message) -> str:
        return (super().render(message) +
                "\n## Coding agent workflow\n\n"
                "Read the referenced project and its instructions. For a handoff, first "
                "acknowledge its message_id as the destination agent through the CLI or API. "
                "Perform only the authorized task, then submit a report with the same task_id "
                "and correlation_id, replying to the handoff message_id. Include results, "
                "validation, blockers, risks and the recommended next action. The task creator "
                "reviews and acknowledges a successful report to complete the task. "
                "Artifact strings are references; the bridge does not open or execute them.\n")
