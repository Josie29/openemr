import logging
from collections.abc import Awaitable, Callable

from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.tools import RunContext, ToolDefinition

from copilot.config import Settings
from copilot.graph.deps import BudgetedTool, GraphDeps

logger = logging.getLogger(__name__)

_Prepare = Callable[[RunContext[GraphDeps], ToolDefinition], Awaitable[ToolDefinition | None]]


def tool_budgets(settings: Settings) -> dict[BudgetedTool, int]:
    """Build the per-tool call budgets a turn runs under."""
    return {
        BudgetedTool.LIST_DOCUMENTS: settings.agent_max_document_lists_per_run,
        BudgetedTool.SEARCH_GUIDELINES: settings.agent_max_searches_per_run,
    }


def _calls_so_far(ctx: RunContext[GraphDeps], tool: BudgetedTool) -> int:
    """Count this run's prior calls to ``tool``, read from the run's own message history."""
    return sum(
        1
        for message in ctx.messages
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, ToolCallPart) and part.tool_name == tool
    )


def budgeted(tool: BudgetedTool) -> _Prepare:
    """Build a ``prepare`` hook that withholds ``tool`` once it has spent its budget for the run.

    Returning ``None`` omits the tool from the schema sent with the next model request, so the
    model cannot call what it cannot see. That is the whole mechanism, and it has to be the model's
    ability to call rather than the tool's willingness to work: ``list_documents`` was already
    memoized — every repeat a ~0.4s cache hit doing nothing — and nine of them still ran a prod turn
    to $0.30 and then failed, because each call costs a model round-trip regardless.

    The count comes from ``ctx.messages`` rather than a counter we keep, so there is no shadow state
    to drift and nothing to remember in a tool body. That scopes it to one agent run; the turn-wide
    ceiling remains ``agent_tool_calls_limit``.

    Args:
        tool: The tool to budget.

    Returns:
        A pydantic-ai ``prepare`` hook.
    """

    async def prepare(
        ctx: RunContext[GraphDeps], tool_def: ToolDefinition
    ) -> ToolDefinition | None:
        budget = ctx.deps.tool_budgets.get(tool)
        if budget is None or _calls_so_far(ctx, tool) < budget:
            return tool_def
        logger.info(
            "tool budget spent; withholding it from the model",
            extra={"tool": tool.value, "correlation_id": ctx.deps.correlation_id, "budget": budget},
        )
        return None

    return prepare
