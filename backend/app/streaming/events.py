from enum import StrEnum
from typing import Any
from pydantic import BaseModel


class EventType(StrEnum):
    PHASE_START = "phase_start"
    PLAN = "plan"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TOKEN = "token"
    WARNING = "warning"
    PORTFOLIO_SECTION = "portfolio_section"
    DONE = "done"
    ERROR = "error"
    HEARTBEAT = "heartbeat"


class AgentEvent(BaseModel):
    type: EventType
    job_id: str
    seq: int
    data: dict[str, Any] = {}

    def to_sse(self) -> str:
        return f"id:{self.seq}\nevent:{self.type}\ndata:{self.model_dump_json()}\n\n"


def phase_start(job_id: str, seq: int, phase: str, description: str) -> AgentEvent:
    return AgentEvent(type=EventType.PHASE_START, job_id=job_id, seq=seq,
                      data={"phase": phase, "description": description})


def tool_call_event(job_id: str, seq: int, tool: str, args: dict[str, Any]) -> AgentEvent:
    return AgentEvent(type=EventType.TOOL_CALL, job_id=job_id, seq=seq,
                      data={"tool": tool, "args": args})


def tool_result_event(job_id: str, seq: int, tool: str, result: Any,
                      success: bool, duration_ms: int) -> AgentEvent:
    return AgentEvent(type=EventType.TOOL_RESULT, job_id=job_id, seq=seq,
                      data={"tool": tool, "result": result, "success": success,
                            "duration_ms": duration_ms})


def token_event(job_id: str, seq: int, text: str) -> AgentEvent:
    return AgentEvent(type=EventType.TOKEN, job_id=job_id, seq=seq, data={"text": text})


def warning_event(job_id: str, seq: int, message: str) -> AgentEvent:
    return AgentEvent(type=EventType.WARNING, job_id=job_id, seq=seq, data={"message": message})


def portfolio_section_event(job_id: str, seq: int, section: str, content: Any) -> AgentEvent:
    return AgentEvent(type=EventType.PORTFOLIO_SECTION, job_id=job_id, seq=seq,
                      data={"section": section, "content": content})


def done_event(job_id: str, seq: int) -> AgentEvent:
    return AgentEvent(type=EventType.DONE, job_id=job_id, seq=seq)


def error_event(job_id: str, seq: int, message: str, phase: str | None = None) -> AgentEvent:
    return AgentEvent(type=EventType.ERROR, job_id=job_id, seq=seq,
                      data={"message": message, "phase": phase})


def heartbeat_event(job_id: str, seq: int) -> AgentEvent:
    return AgentEvent(type=EventType.HEARTBEAT, job_id=job_id, seq=seq)
