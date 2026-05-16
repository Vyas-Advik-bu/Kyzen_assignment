"""
CLI dry-run: prove data flows end-to-end before introducing the FastAPI layer.
Usage: python -m app.agent.cli <company_name> [--ticker TICKER]

Runs the full Resolve → Research → Synthesize pipeline and prints the
validated Portfolio JSON to stdout. No web server, no SSE, no frontend needed.
"""
import argparse
import asyncio
import json
import sys

from app.agent.orchestrator import run_pipeline
from app.agent.schemas import ResearchJob
from app.observability.logging import configure_logging
from app.streaming.bus import JobEventBus
from app.streaming.events import EventType


async def main(company_name: str, verbose: bool) -> None:
    configure_logging("DEBUG" if verbose else "INFO")

    job = ResearchJob(job_id="cli-dry-run", company_name=company_name)
    bus = JobEventBus()

    print(f"\n{'─' * 60}", flush=True)
    print(f"  Researching: {company_name}", flush=True)
    print(f"{'─' * 60}\n", flush=True)

    async def _print_events() -> None:
        async for event in bus.subscribe("cli-dry-run", heartbeat_interval=30.0):
            match event.type:
                case "phase_start":
                    print(f"\n[{event.data['phase'].upper()}] {event.data['description']}",
                          flush=True)
                case "tool_call":
                    args_preview = str(event.data["args"])[:80]
                    print(f"  → CALL {event.data['tool']}({args_preview})", flush=True)
                case "tool_result":
                    status = "✓" if event.data["success"] else "✗"
                    print(f"  {status} {event.data['tool']} ({event.data['duration_ms']}ms)",
                          flush=True)
                case "token":
                    print(event.data["text"], end="", flush=True)
                case "warning":
                    print(f"  ⚠  {event.data['message']}", flush=True)
                case "error":
                    print(f"\n[ERROR] {event.data['message']}", flush=True)
                case "done":
                    print("\n\n[DONE]", flush=True)

    event_task = asyncio.create_task(_print_events())
    await run_pipeline(job, bus)
    await event_task

    from app.jobs.store import job_store  # noqa: F401 — not used in CLI mode
    # Print final portfolio if available
    if job.portfolio:
        print("\n" + "=" * 60)
        print("PORTFOLIO JSON")
        print("=" * 60)
        print(job.portfolio.model_dump_json(indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Company Research Agent CLI")
    parser.add_argument("company", help="Company name to research")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.company, args.verbose))
