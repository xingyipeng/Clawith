"""Unified agent LLM + tool execution runtime.

Replaces 5 duplicate LLM tool-calling loops (websocket, task_executor,
heartbeat, scheduler, A2A) with a single, configurable runtime.

Design principles:
- Pure async Python — no FastAPI/WebSocket/DB/vendor SDK dependencies
- Model-agnostic — only depends on LLMClient ABC (complete/stream)
- Testable — execute_tool_fn is injected, not imported
- Configurable — RuntimeConfig controls all behavior per caller
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from app.services.llm_client import (
    ChunkCallback,
    LLMClient,
    LLMError,
    LLMMessage,
    LLMResponse,
    ThinkingCallback,
)

logger = logging.getLogger(__name__)

# ─── Types ────────────────────────────────────────────────────────────────────

ToolCallCallback = Callable[[dict], Coroutine[Any, Any, None]]
ToolResultFilter = Callable[[str, str, dict], Coroutine[Any, Any, str | list]]
ExecuteToolFn = Callable[
    [str, dict, uuid.UUID | None, uuid.UUID | None, str],
    Coroutine[Any, Any, str],
]
RetryCheck = Callable[[Exception], bool]

# ─── Constants ────────────────────────────────────────────────────────────────

LOOP_TIMEOUT_BY_MODE: dict[str, float] = {
    "chat": 180,
    "task": 600,
    "trigger": 300,
    "coordinator": 600,
    "a2a": 120,
    "heartbeat": 60,
    "schedule": 600,
}

# Tools that MUST have non-empty arguments (LLM sometimes emits empty args)
TOOLS_REQUIRING_ARGS: frozenset[str] = frozenset({
    "write_file", "read_file", "delete_file", "read_document",
    "send_message_to_agent", "send_feishu_message", "send_email",
    "web_search", "jina_search", "jina_read",
})

# A2A tools contain full LLM call chains — need longer timeout
_A2A_TOOLS: frozenset[str] = frozenset({
    "send_message_to_agent", "send_file_to_agent",
})


# ─── Config & Result ─────────────────────────────────────────────────────────

@dataclass
class RuntimeConfig:
    """Configuration for a single AgentRuntime execution."""

    agent_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    session_id: str = ""
    execution_mode: str = "chat"

    # Loop control
    max_tool_rounds: int = 50
    loop_timeout_seconds: float | None = None  # None = auto from execution_mode
    tool_exec_timeout_seconds: float = 60.0

    # Streaming callbacks (optional)
    use_streaming: bool = False
    on_chunk: ChunkCallback | None = None
    on_thinking: ThinkingCallback | None = None
    on_tool_call: ToolCallCallback | None = None

    # Retry
    max_retries: int = 1  # 1 = no retry
    retryable_check: RetryCheck | None = None

    # Token tracking
    track_tokens: bool = True

    # Rate limiting {tool_name: max_calls_per_execution}
    tool_rate_limits: dict[str, int] = field(default_factory=dict)

    # Vision / post-processing
    supports_vision: bool = False
    tool_result_filter: ToolResultFilter | None = None

    # Round warnings (Aware engine)
    warn_at_80_percent: bool = False


@dataclass
class RuntimeResult:
    """Result of an AgentRuntime.run() execution."""

    content: str = ""
    rounds_used: int = 0
    total_tokens: int = 0
    timed_out: bool = False
    max_rounds_hit: bool = False
    error: str | None = None


# ─── Default retry check ─────────────────────────────────────────────────────

def default_retryable_check(exc: Exception) -> bool:
    """Standard retryable error check (extracted from A2A implementation)."""
    _MARKERS = (
        "http 408", "http 429", "http 500", "http 502", "http 503", "http 504",
        "timeout", "timed out", "connection failed", "temporarily unavailable",
        "rate limit",
    )
    if isinstance(exc, LLMError):
        lowered = (str(exc) or "").lower()
        return any(m in lowered for m in _MARKERS)
    # httpx errors
    exc_name = type(exc).__name__.lower()
    return any(k in exc_name for k in ("timeout", "transport", "connect"))


# ─── Runtime ──────────────────────────────────────────────────────────────────

class AgentRuntime:
    """Unified LLM + tool execution loop.

    Usage:
        runtime = AgentRuntime(client, tools, execute_tool, config,
                               temperature=0.7, max_tokens=4096)
        result = await runtime.run(messages)
    """

    def __init__(
        self,
        client: LLMClient,
        tools: list[dict] | None,
        execute_tool_fn: ExecuteToolFn,
        config: RuntimeConfig,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ):
        self._client = client
        self._tools = tools
        self._execute_tool = execute_tool_fn
        self._cfg = config
        self._temperature = temperature
        self._max_tokens = max_tokens

        # Resolve loop timeout
        self._loop_timeout = (
            config.loop_timeout_seconds
            if config.loop_timeout_seconds is not None
            else LOOP_TIMEOUT_BY_MODE.get(config.execution_mode, 300)
        )

        # Runtime state (reset per run)
        self._accumulated_tokens = 0
        self._tool_call_counts: dict[str, int] = {}
        self._reasoning_content = ""

    async def run(self, messages: list[LLMMessage]) -> RuntimeResult:
        """Execute the LLM tool-calling loop.

        Args:
            messages: Initial message list (system + user + history).
                     Modified in-place during execution.

        Returns:
            RuntimeResult with final content and execution metadata.
        """
        cfg = self._cfg
        loop_start = time.monotonic()
        result = RuntimeResult()

        try:
            for round_i in range(cfg.max_tool_rounds):
                # ── Check overall timeout ──
                elapsed = time.monotonic() - loop_start
                if elapsed > self._loop_timeout:
                    logger.warning(
                        f"[Runtime] Timeout after {elapsed:.0f}s / {round_i} rounds "
                        f"(mode={cfg.execution_mode}, agent={cfg.agent_id})"
                    )
                    result.timed_out = True
                    result.content = (
                        f"⚠️ 执行超时（已运行 {int(elapsed)} 秒，{round_i} 轮工具调用）。"
                        "请尝试将任务拆分为更小的步骤。"
                    )
                    break

                # ── Inject round warnings ──
                self._inject_round_warnings(round_i, messages)

                # ── Call LLM (with retry) ──
                response = await self._call_llm_with_retry(messages)
                result.rounds_used = round_i + 1

                # ── Track tokens ──
                self._track_tokens(response, messages)

                # ── No tool calls → done ──
                if not response.tool_calls:
                    result.content = response.content or ""
                    break

                # ── Append assistant message with tool calls ──
                messages.append(LLMMessage(
                    role="assistant",
                    content=response.content or None,
                    tool_calls=[{
                        "id": tc["id"],
                        "type": "function",
                        "function": tc["function"],
                    } for tc in response.tool_calls],
                    reasoning_content=response.reasoning_content,
                ))
                self._reasoning_content = response.reasoning_content or ""

                # ── Execute each tool call ──
                for tc in response.tool_calls:
                    tool_result = await self._execute_single_tool(tc)
                    messages.append(LLMMessage(
                        role="tool",
                        content=tool_result,
                        tool_call_id=tc.get("id", ""),
                    ))
            else:
                # Exhausted all rounds
                result.max_rounds_hit = True
                if not result.content:
                    result.content = (
                        f"⚠️ 已达到最大工具调用轮次（{cfg.max_tool_rounds}）。"
                        "请尝试简化任务或增加轮次上限。"
                    )

        except LLMError as e:
            logger.error(f"[Runtime] LLM error: {e}")
            result.error = f"[LLM Error] {e}"
            result.content = result.error
        except Exception as e:
            logger.error(f"[Runtime] Unexpected error: {type(e).__name__}: {e}")
            result.error = f"[LLM call error] {type(e).__name__}: {str(e)[:200]}"
            result.content = result.error

        # ── Flush token usage ──
        result.total_tokens = self._accumulated_tokens
        if cfg.track_tokens and cfg.agent_id and self._accumulated_tokens > 0:
            try:
                from app.services.token_tracker import record_token_usage
                await record_token_usage(cfg.agent_id, self._accumulated_tokens)
            except Exception as e:
                logger.warning(f"[Runtime] Token recording failed: {e}")

        return result

    # ── Internal methods ──────────────────────────────────────────────────────

    async def _call_llm_with_retry(self, messages: list[LLMMessage]) -> LLMResponse:
        """Call LLM with optional retry and exponential backoff."""
        cfg = self._cfg
        last_exc: Exception | None = None

        for attempt in range(1, cfg.max_retries + 1):
            try:
                if cfg.use_streaming:
                    return await self._client.stream(
                        messages=messages,
                        tools=self._tools,
                        temperature=self._temperature,
                        max_tokens=self._max_tokens,
                        on_chunk=cfg.on_chunk,
                        on_thinking=cfg.on_thinking,
                    )
                else:
                    return await self._client.complete(
                        messages=messages,
                        tools=self._tools,
                        temperature=self._temperature,
                        max_tokens=self._max_tokens,
                    )
            except Exception as e:
                last_exc = e
                is_retryable = (
                    cfg.retryable_check(e) if cfg.retryable_check
                    else default_retryable_check(e)
                ) if cfg.max_retries > 1 else False

                if is_retryable and attempt < cfg.max_retries:
                    wait = (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                    logger.warning(
                        f"[Runtime] LLM call failed (attempt {attempt}/{cfg.max_retries}), "
                        f"retrying in {wait:.1f}s: {e}"
                    )
                    await asyncio.sleep(wait)
                else:
                    raise

        # Should not reach here, but satisfy type checker
        raise last_exc or RuntimeError("LLM call failed with no exception")

    async def _execute_single_tool(self, tc: dict) -> str | list:
        """Parse, validate, rate-limit, execute, and filter a single tool call."""
        cfg = self._cfg
        fn = tc.get("function", tc)
        tool_name = fn.get("name", "unknown")
        raw_args = fn.get("arguments", "{}")

        # Parse arguments
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except json.JSONDecodeError:
            args = {}

        # Validate: empty args for tools that require them
        if not args and tool_name in TOOLS_REQUIRING_ARGS:
            logger.warning(f"[Runtime] Empty args for {tool_name}, asking LLM to retry")
            return (
                f"Error: {tool_name} was called with empty arguments. "
                "You must provide the required parameters. Please retry."
            )

        # Rate limiting
        blocked = self._check_rate_limit(tool_name)
        if blocked:
            return blocked

        # Notify: running
        if cfg.on_tool_call:
            try:
                await cfg.on_tool_call({
                    "name": tool_name,
                    "args": args,
                    "status": "running",
                    "reasoning_content": self._reasoning_content,
                })
            except Exception:
                pass

        # Execute with timeout
        tool_timeout = (
            self._loop_timeout if tool_name in _A2A_TOOLS
            else cfg.tool_exec_timeout_seconds
        )
        try:
            tool_result = await asyncio.wait_for(
                self._execute_tool(
                    tool_name, args,
                    agent_id=cfg.agent_id,
                    user_id=cfg.user_id or cfg.agent_id,
                    session_id=cfg.session_id,
                ),
                timeout=tool_timeout,
            )
        except TimeoutError:
            tool_result = f"❌ Tool {tool_name} timed out after {tool_timeout:.0f}s"
            logger.warning(f"[Runtime] {tool_result}")

        logger.debug(f"[Runtime] Tool {tool_name} result: {str(tool_result)[:100]}")

        # Apply result filter (vision injection, A2A nudges, etc.)
        if cfg.tool_result_filter:
            try:
                tool_result = await cfg.tool_result_filter(tool_name, str(tool_result), args)
            except Exception as e:
                logger.warning(f"[Runtime] tool_result_filter failed: {e}")

        # Notify: done
        if cfg.on_tool_call:
            try:
                await cfg.on_tool_call({
                    "name": tool_name,
                    "args": args,
                    "status": "done",
                    "result": str(tool_result) if not isinstance(tool_result, list) else "[vision content]",
                    "reasoning_content": self._reasoning_content,
                })
            except Exception:
                pass

        return tool_result

    def _check_rate_limit(self, tool_name: str) -> str | None:
        """Check per-tool rate limit. Returns blocked message or None."""
        limit = self._cfg.tool_rate_limits.get(tool_name)
        if limit is None:
            return None
        current = self._tool_call_counts.get(tool_name, 0)
        if current >= limit:
            return (
                f"[BLOCKED] {tool_name} has reached its limit of {limit} call(s) "
                "in this execution cycle. Please skip this action."
            )
        self._tool_call_counts[tool_name] = current + 1
        return None

    def _inject_round_warnings(self, round_i: int, messages: list[LLMMessage]) -> None:
        """Inject tool-round-limit warning at 80% and last-2 thresholds."""
        if not self._cfg.warn_at_80_percent:
            return
        max_rounds = self._cfg.max_tool_rounds
        threshold_80 = int(max_rounds * 0.8)
        threshold_last = max_rounds - 2

        if round_i == threshold_80:
            messages.append(LLMMessage(
                role="user",
                content=(
                    f"⚠️ 你已使用 {round_i}/{max_rounds} 轮工具调用。"
                    "如果当前任务尚未完成，请尽快保存进度到 focus.md，"
                    "并使用 set_trigger 设置续接触发器，在剩余轮次中做好收尾。"
                ),
            ))
        elif round_i == threshold_last and threshold_last > threshold_80:
            messages.append(LLMMessage(
                role="user",
                content=(
                    f"🚨 仅剩 {max_rounds - round_i} 轮工具调用。"
                    "请立即保存进度到 focus.md 并设置续接触发器。"
                ),
            ))

    def _track_tokens(self, response: LLMResponse, messages: list[LLMMessage]) -> None:
        """Accumulate token usage from response, with fallback estimation."""
        from app.services.token_tracker import extract_usage_tokens, estimate_tokens_from_chars

        real_tokens = extract_usage_tokens(response.usage)
        if real_tokens:
            self._accumulated_tokens += real_tokens
        else:
            # Estimate from character count
            total_chars = sum(
                len(m.content or "") if isinstance(m.content, str) else 0
                for m in messages
            ) + len(response.content or "")
            self._accumulated_tokens += estimate_tokens_from_chars(total_chars)
