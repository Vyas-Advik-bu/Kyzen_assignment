"""
The raw tool-calling research loop for Phase 1.
Runs against Ollama, emitting AgentEvents to the bus as it works.
Bounded by MAX_ITERATIONS to prevent runaway on unreliable local models.
"""
import json
import time
from typing import Any

from app.agent.prompts import RESEARCH_SYSTEM, RESEARCH_USER
from app.agent.schemas import ResolvedCompany
from app.llm.ollama_client import OllamaClient, ToolCall
from app.streaming.bus import JobEventBus
from app.streaming.events import (
    tool_call_event,
    tool_result_event,
    token_event,
    warning_event,
)
from app.tools.registry import ToolRegistry
from app.observability.logging import get_logger

log = get_logger(__name__)

MAX_ITERATIONS = 12
DONE_SIGNAL = "RESEARCH_COMPLETE"

# Per-tool call caps — prevent fetch-all traps and semantic oscillation loops.
MAX_CALLS_PER_TOOL: dict[str, int] = {
    "web_search": 6,
    "fetch_page": 3,
    "get_company_profile": 2,
    "get_company_financials": 2,
}


async def research_loop(
    company: ResolvedCompany,
    llm: OllamaClient,
    registry: ToolRegistry,
    bus: JobEventBus,
    job_id: str,
) -> list[dict[str, Any]]:
    """
    Run the multi-step tool-calling research loop.
    Returns a list of evidence dicts accumulated from tool results.
    """
    evidence: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": RESEARCH_SYSTEM.format(company_name=company.name),
        },
        {
            "role": "user",
            "content": RESEARCH_USER.format(
                company_name=company.name,
                ticker=company.ticker or "N/A",
                company_type=company.type,
            ),
        },
    ]

    def _seq() -> int:
        return bus.next_seq(job_id)

    # Exact-duplicate dedup (same tool + same args string)
    seen_calls: set[tuple[str, str]] = set()
    # Per-tool call counter — caps semantic oscillation and fetch-all traps
    tool_call_counts: dict[str, int] = {}
    # Global call counter — ensures tool_call ids are unique across all iterations
    global_call_counter = 0
    # Consecutive text-only iterations — model may reason before calling tools,
    # but if it does it twice in a row without a tool call we stop.
    consecutive_text_only = 0

    for iteration in range(MAX_ITERATIONS):
        log.debug("loop_iteration", job_id=job_id, iteration=iteration)

        # Collect streamed output from the model
        accumulated_text = ""
        pending_tool_calls: list[ToolCall] = []

        async for item in llm.stream_chat(
            messages, tools=registry.schemas(), temperature=0.1
        ):
            if isinstance(item, ToolCall):
                pending_tool_calls.append(item)
            else:
                accumulated_text += item
                bus.publish(token_event(job_id, _seq(), item))

        # Check for stop signal in model text
        if DONE_SIGNAL in accumulated_text:
            log.info("research_complete_signal", job_id=job_id, iteration=iteration)
            break

        if not pending_tool_calls:
            # No tool calls and no stop signal.
            # The model may have output reasoning text before it's ready to call tools.
            # Add the response to conversation history and give it one more chance.
            if accumulated_text.strip():
                messages.append({"role": "assistant", "content": accumulated_text})
                consecutive_text_only += 1
                if consecutive_text_only < 2:
                    log.warning("no_tool_calls_retrying", job_id=job_id,
                                iteration=iteration, text=accumulated_text[:100])
                    continue
            log.warning("no_tool_calls_no_signal", job_id=job_id,
                        iteration=iteration, text=accumulated_text[:200])
            break

        consecutive_text_only = 0  # reset on any successful tool call

        # Assign globally unique IDs so repeated iterations don't collide
        for tc in pending_tool_calls:
            tc.id = f"call_{global_call_counter}"
            global_call_counter += 1

        # Add assistant turn to messages
        messages.append({"role": "assistant", "content": accumulated_text,
                          "tool_calls": [
                              {"id": tc.id, "type": "function",
                               "function": {"name": tc.name, "arguments": tc.arguments}}
                              for tc in pending_tool_calls
                          ]})

        # Execute tool calls — skip exact duplicates to break stubborn retry loops
        for tc in pending_tool_calls:
            # Strip non-serializable injected objects (e.g. llm) before hashing
            hashable_args = {k: v for k, v in tc.arguments.items() if k != "llm"}
            call_key = (tc.name, json.dumps(hashable_args, sort_keys=True, default=str))

            # Check per-tool cap first
            tool_count = tool_call_counts.get(tc.name, 0)
            cap = MAX_CALLS_PER_TOOL.get(tc.name, MAX_ITERATIONS)
            if tool_count >= cap:
                cap_msg = f"Per-tool cap reached for {tc.name!r} ({cap} calls max)"
                bus.publish(warning_event(job_id, _seq(), cap_msg))
                messages.append({
                    "role": "tool",
                    "content": json.dumps({"error": cap_msg}),
                    "tool_call_id": tc.id,
                })
                continue
            tool_call_counts[tc.name] = tool_count + 1

            if call_key in seen_calls:
                dup_msg = f"Skipping duplicate call to {tc.name!r} with same args"
                bus.publish(warning_event(job_id, _seq(), dup_msg))
                messages.append({
                    "role": "tool",
                    "content": json.dumps({"error": dup_msg}),
                    "tool_call_id": tc.id,
                })
                continue
            seen_calls.add(call_key)

            # Publish event with clean args (no llm object — it can't be JSON-serialized)
            bus.publish(tool_call_event(job_id, _seq(), tc.name, hashable_args))
            t0 = time.monotonic()
            try:
                # Use hashable_args (no llm) — the registry lambda already captures llm
                result = await registry.call(tc.name, hashable_args)
                duration_ms = int((time.monotonic() - t0) * 1000)
                bus.publish(tool_result_event(job_id, _seq(), tc.name, result, True, duration_ms))
                evidence.append({"tool": tc.name, "args": hashable_args, "result": result})
                messages.append({
                    "role": "tool",
                    "content": json.dumps(result, default=str),
                    "tool_call_id": tc.id,
                })
            except Exception as exc:
                duration_ms = int((time.monotonic() - t0) * 1000)
                error_payload = {"error": str(exc)}
                bus.publish(tool_result_event(job_id, _seq(), tc.name, error_payload,
                                               False, duration_ms))
                bus.publish(warning_event(job_id, _seq(), f"Tool {tc.name!r} failed: {exc}"))
                messages.append({
                    "role": "tool",
                    "content": json.dumps(error_payload),
                    "tool_call_id": tc.id,
                })
    else:
        bus.publish(warning_event(job_id, _seq(),
                                  f"Research loop hit max iterations ({MAX_ITERATIONS})"))

    return evidence
