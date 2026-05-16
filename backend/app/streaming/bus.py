"""
In-memory event bus per job.
Events are buffered so frontend reconnections can replay from any sequence number.
"""
import asyncio
from collections import defaultdict

from app.streaming.events import AgentEvent, heartbeat_event
from app.observability.logging import get_logger

log = get_logger(__name__)


class JobEventBus:
    def __init__(self) -> None:
        self._events: dict[str, list[AgentEvent]] = defaultdict(list)
        self._queues: dict[str, list[asyncio.Queue[AgentEvent | None]]] = defaultdict(list)
        self._closed: set[str] = set()  # jobs whose pipeline has finished

    def publish(self, event: AgentEvent) -> None:
        self._events[event.job_id].append(event)
        for q in self._queues[event.job_id]:
            q.put_nowait(event)
        log.debug("event_published", job_id=event.job_id, type=event.type, seq=event.seq)

    def next_seq(self, job_id: str) -> int:
        return len(self._events[job_id])

    async def subscribe(
        self,
        job_id: str,
        last_event_id: int = -1,
        heartbeat_interval: float = 15.0,
    ):
        """
        Async generator yielding AgentEvent objects.
        Replays buffered events after `last_event_id` (exclusive), then delivers live events.
        -1 means no prior events received — replay everything.
        Yields heartbeat AgentEvents every `heartbeat_interval` seconds.
        Auto-cleans event buffer when the job is done and this is the last subscriber.
        """
        q: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
        self._queues[job_id].append(q)
        try:
            # Replay missed events (last_event_id is the seq already received, so skip it)
            for event in self._events[job_id][last_event_id + 1:]:
                yield event

            # Live delivery
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=heartbeat_interval)
                except asyncio.TimeoutError:
                    yield heartbeat_event(job_id, self.next_seq(job_id))
                    continue
                if event is None:  # sentinel: job finished
                    break
                yield event
        finally:
            self._queues[job_id].remove(q)
            # Free buffered events once the job is done and no subscribers remain
            if job_id in self._closed and not self._queues[job_id]:
                self._cleanup(job_id)

    def close_job(self, job_id: str) -> None:
        """Signal all subscribers that the job stream is done."""
        self._closed.add(job_id)
        for q in self._queues[job_id]:
            q.put_nowait(None)
        # If nobody is subscribed right now, clean up immediately
        if not self._queues[job_id]:
            self._cleanup(job_id)

    def _cleanup(self, job_id: str) -> None:
        self._events.pop(job_id, None)
        self._queues.pop(job_id, None)
        self._closed.discard(job_id)
        log.debug("bus_cleanup", job_id=job_id)


# Singleton used across the app
event_bus = JobEventBus()
