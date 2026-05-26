"""BaseAgent — shared foundation for all agents in the pipeline.

Every agent in the system inherits from this class. It owns:
  - The Anthropic async client
  - The tool registry and tool-use loop
  - Structured logging of every prompt and response
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

import anthropic
from loguru import logger

from src.config import get_settings

ProgressCallback = Callable[[str], None]

# A tool handler is an async callable that receives the tool input dict
# and returns any JSON-serialisable value.
ToolHandler = Callable[[dict[str, Any]], Awaitable[Any]]


class ToolSpec:
    """Pairs an Anthropic tool definition with its Python handler."""

    def __init__(
        self,
        definition: dict[str, Any],
        handler: ToolHandler,
    ) -> None:
        self.definition = definition
        self.handler = handler

    @property
    def name(self) -> str:
        return self.definition["name"]


class BaseAgent:
    """Wraps the Anthropic async client with a tool-use loop and structured logging.

    Typical usage — simple call (no tools):
        agent = BaseAgent(name="my-agent", system_prompt="You are ...")
        result = await agent.run("What is 2+2?")

    With tool-use loop:
        agent = (
            BaseAgent(name="orchestrator", system_prompt="...")
            .with_tools([
                ToolSpec(definition={...}, handler=my_async_fn),
            ])
        )
        result = await agent.run("Do something that needs tools")
    """

    DEFAULT_MODEL = "claude-opus-4-7"
    DEFAULT_MAX_TOKENS = 4096

    def __init__(
        self,
        name: str,
        system_prompt: str,
        model: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        settings = get_settings()
        self.name = name
        self.system_prompt = system_prompt
        self.model = model or settings.default_model or self.DEFAULT_MODEL
        self.max_tokens = max_tokens

        self._client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            timeout=settings.claude_timeout_seconds,
        )
        self._tool_specs: dict[str, ToolSpec] = {}
        self._on_thinking: ProgressCallback | None = None
        self._force_tool_use: bool = False

    # ------------------------------------------------------------------
    # Fluent builder
    # ------------------------------------------------------------------

    def with_progress(self, callback: ProgressCallback) -> "BaseAgent":
        """Register a progress callback fired before each Claude API call."""
        self._on_thinking = callback
        return self

    def with_forced_tool_use(self) -> "BaseAgent":
        """Force Claude to call a tool on every turn (tool_choice='any').

        Use on agents where plain-text responses mean the pipeline stalls —
        e.g. the orchestrator, which must always call a pipeline tool.
        """
        self._force_tool_use = True
        return self

    def with_tools(self, specs: list[ToolSpec]) -> "BaseAgent":
        """Register tools on this agent and return self for chaining.

        Registered tools are passed to Claude on every `run()` call and their
        handlers are dispatched automatically inside the tool-use loop.
        """
        for spec in specs:
            self._tool_specs[spec.name] = spec
            logger.debug(f"[{self.name}] registered tool '{spec.name}'")
        return self

    # ------------------------------------------------------------------
    # Core run method
    # ------------------------------------------------------------------

    async def run(
        self,
        user_input: str,
        extra_messages: list[dict[str, Any]] | None = None,
    ) -> str:
        """Send user_input to Claude and return the final text response.

        If tools are registered, drives the full tool-use loop until Claude
        returns stop_reason='end_turn'. extra_messages can be used to prepend
        prior conversation turns (for multi-turn flows in the Orchestrator).
        """
        messages: list[dict[str, Any]] = list(extra_messages or [])
        messages.append({"role": "user", "content": user_input})

        tool_defs = [spec.definition for spec in self._tool_specs.values()]

        self._log_prompt(user_input)

        iteration = 0
        max_iterations = 10  # guard against runaway loops

        while iteration < max_iterations:
            iteration += 1

            if self._on_thinking:
                msg = (
                    "thinking…"
                    if iteration == 1
                    else "reviewing results, planning next step…"
                )
                self._on_thinking(msg)

            kwargs: dict[str, Any] = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "system": self.system_prompt,
                "messages": messages,
            }
            if tool_defs:
                kwargs["tools"] = tool_defs
                # Force a tool call on the first turn only — prevents Haiku/smaller
                # models from returning plain text and stalling the pipeline.
                if self._force_tool_use and iteration == 1:
                    kwargs["tool_choice"] = {"type": "any"}

            response = await self._client.messages.create(**kwargs)
            self._log_response(response)

            if response.stop_reason == "end_turn":
                return self._extract_text(response)

            if response.stop_reason == "tool_use":
                # Append Claude's response (may contain text + tool_use blocks)
                messages.append({"role": "assistant", "content": response.content})

                # Execute each tool call and collect results
                tool_results = await self._execute_tool_calls(response.content)
                messages.append({"role": "user", "content": tool_results})
                continue

            # Unexpected stop reason — return whatever text is there
            logger.warning(
                f"[{self.name}] unexpected stop_reason='{response.stop_reason}'"
            )
            return self._extract_text(response)

        logger.error(f"[{self.name}] tool-use loop hit max_iterations={max_iterations}")
        return self._extract_text(response)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def _execute_tool_calls(
        self, content: list[Any]
    ) -> list[dict[str, Any]]:
        """Execute every tool_use block in the response content concurrently.

        Returns a list of tool_result content blocks ready to send back to Claude.
        """
        import asyncio

        async def _call_one(block: Any) -> dict[str, Any]:
            tool_name: str = block.name
            tool_input: dict[str, Any] = block.input
            tool_use_id: str = block.id

            logger.info(f"[{self.name}] → tool '{tool_name}' input={tool_input}")

            spec = self._tool_specs.get(tool_name)
            if spec is None:
                error_msg = f"Unknown tool '{tool_name}'"
                logger.error(f"[{self.name}] {error_msg}")
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "is_error": True,
                    "content": error_msg,
                }

            try:
                result = await spec.handler(tool_input)
                result_str = (
                    result if isinstance(result, str) else json.dumps(result, default=str)
                )
                logger.info(
                    f"[{self.name}] ← tool '{tool_name}' "
                    f"result_len={len(result_str)}"
                )
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": result_str,
                }
            except Exception as exc:
                logger.exception(f"[{self.name}] tool '{tool_name}' raised: {exc}")
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "is_error": True,
                    "content": str(exc),
                }

        tool_use_blocks = [b for b in content if getattr(b, "type", None) == "tool_use"]
        results = await asyncio.gather(*[_call_one(b) for b in tool_use_blocks])
        return list(results)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_text(response: anthropic.types.Message) -> str:
        """Pull all text blocks from a response into a single string."""
        parts = [
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text"
        ]
        return "\n".join(parts)

    def _log_prompt(self, user_input: str) -> None:
        preview = user_input[:120].replace("\n", " ")
        logger.info(f"[{self.name}] → prompt: {preview!r}")

    def _log_response(self, response: anthropic.types.Message) -> None:
        text_preview = self._extract_text(response)[:120].replace("\n", " ")
        logger.info(
            f"[{self.name}] ← stop={response.stop_reason} "
            f"in={response.usage.input_tokens} out={response.usage.output_tokens} "
            f"text={text_preview!r}"
        )
