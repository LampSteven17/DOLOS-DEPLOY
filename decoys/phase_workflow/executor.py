"""Timezone-aware daily executor for immutable PHASE workflow plans."""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Optional, Protocol

from phase_workflow.loader import PlanEntry, WorkflowPlan
from phase_workflow.registry import ResolvedTask, WorkflowRegistry, WorkflowResult


UTC = timezone.utc
TERMINAL_STATUSES = frozenset({"completed", "failed", "missed"})
TERMINAL_REASONS = frozenset({
    "startup_past_due",
    "dst_nonexistent_time",
    "window_closed_while_waiting",
    "workflow_failed",
})


class Clock(Protocol):
    def now(self) -> datetime:
        ...


class RealClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class Handle(Protocol):
    def done(self) -> bool:
        ...

    def result(self) -> WorkflowResult:
        ...

    def add_done_callback(self, callback: Callable[[object], None]) -> None:
        ...


@dataclass
class Occurrence:
    local_day: date
    window_index: int
    sequence_index: int
    window_end_minute: int
    entry: PlanEntry
    task: ResolvedTask
    scheduled_local: datetime
    scheduled_utc: Optional[datetime]
    state: str = "scheduled"
    actual_start: Optional[datetime] = None
    handle: Optional[Handle] = None


class DailyExecutor:
    """Non-random FIFO scheduler. ``tick`` is deterministic and fake-clock safe."""

    def __init__(
        self,
        plan: WorkflowPlan,
        registry: WorkflowRegistry,
        terminal_sink: Callable[[dict], None],
        *,
        clock: Optional[Clock] = None,
        starter: Optional[Callable[[ResolvedTask, str], Handle]] = None,
    ):
        self.plan = plan
        self.registry = registry
        self._sink = terminal_sink
        self.clock = clock or RealClock()
        self.startup_utc = self._utc(self.clock.now())
        self._pool = None
        if starter is None:
            self._pool = ThreadPoolExecutor(
                max_workers=plan.max_parallel,
                thread_name_prefix="phase-workflow",
            )
            starter = self._start_future
        self._starter = starter
        self._wake = threading.Event()
        self._active: list[Occurrence] = []
        self._waiting: list[Occurrence] = []
        self._day = self.startup_utc.astimezone(plan.timezone).date()
        self._occurrences: list[Occurrence] = []
        self._load_day(self._day, initial=True)

    @property
    def occurrences(self) -> tuple[Occurrence, ...]:
        return tuple(self._occurrences)

    @property
    def active_count(self) -> int:
        return len(self._active)

    def tick(self) -> None:
        now = self._utc(self.clock.now())
        local_day = now.astimezone(self.plan.timezone).date()
        if local_day != self._day:
            self._close_unstarted(now)
            self._day = local_day
            self._waiting = []
            self._load_day(local_day, initial=False)

        self._finish_completed(now)
        for occurrence in self._occurrences:
            if occurrence.state != "scheduled":
                continue
            if occurrence.scheduled_utc is not None and occurrence.scheduled_utc <= now:
                occurrence.state = "waiting"
                self._waiting.append(occurrence)

        still_waiting = []
        for occurrence in self._waiting:
            if self._window_is_open(occurrence, now):
                still_waiting.append(occurrence)
            else:
                self._terminal(
                    occurrence,
                    status="missed",
                    reason="window_closed_while_waiting",
                    actual_end=None,
                )
        self._waiting = still_waiting

        while self._waiting and len(self._active) < self.plan.max_parallel:
            occurrence = self._waiting.pop(0)
            occurrence.state = "active"
            occurrence.actual_start = now
            try:
                occurrence.handle = self._starter(
                    occurrence.task, occurrence.local_day.isoformat()
                )
                occurrence.handle.add_done_callback(lambda _handle: self._wake.set())
            except Exception:
                self._terminal(
                    occurrence,
                    status="failed",
                    reason="workflow_failed",
                    actual_end=self._utc(self.clock.now()),
                )
                continue
            self._active.append(occurrence)

    def run_forever(self, stop_event: Optional[threading.Event] = None) -> None:
        stop_event = stop_event or threading.Event()
        while not stop_event.is_set():
            self.tick()
            delay = self._seconds_until_next_clock_event()
            self._wake.wait(timeout=delay)
            self._wake.clear()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True)
        self.tick()

    def _load_day(self, local_day: date, *, initial: bool) -> None:
        occurrences = []
        for window_index, window in enumerate(self.plan.windows):
            for sequence_index, entry in enumerate(window.sequence):
                local_minute = window.start_minute + entry.offset_minutes
                naive = datetime.combine(local_day, time()) + timedelta(
                    minutes=local_minute
                )
                scheduled_utc = self._resolve_first_utc(naive)
                occurrence = Occurrence(
                    local_day=local_day,
                    window_index=window_index,
                    sequence_index=sequence_index,
                    window_end_minute=window.end_minute,
                    entry=entry,
                    task=self.registry.resolve(entry),
                    scheduled_local=naive,
                    scheduled_utc=scheduled_utc,
                )
                occurrences.append(occurrence)
        occurrences.sort(key=lambda item: (
            item.scheduled_utc or datetime.max.replace(tzinfo=UTC),
            item.window_index,
            item.sequence_index,
        ))
        self._occurrences = occurrences
        for occurrence in occurrences:
            if occurrence.scheduled_utc is None:
                self._terminal(
                    occurrence,
                    status="missed",
                    reason="dst_nonexistent_time",
                    actual_end=None,
                )
            elif initial and occurrence.scheduled_utc < self.startup_utc:
                self._terminal(
                    occurrence,
                    status="missed",
                    reason="startup_past_due",
                    actual_end=None,
                )

    def _finish_completed(self, now: datetime) -> None:
        remaining = []
        for occurrence in self._active:
            handle = occurrence.handle
            if handle is None or not handle.done():
                remaining.append(occurrence)
                continue
            try:
                result = handle.result()
            except Exception:
                result = WorkflowResult(completed=False)
            if result.completed:
                self._terminal(
                    occurrence,
                    status="completed",
                    reason=None,
                    actual_end=now,
                    artifact=result.artifact,
                )
            else:
                self._terminal(
                    occurrence,
                    status="failed",
                    reason="workflow_failed",
                    actual_end=now,
                    artifact=result.artifact,
                )
        self._active = remaining

    def _close_unstarted(self, now: datetime) -> None:
        for occurrence in self._occurrences:
            if occurrence.state not in {"scheduled", "waiting"}:
                continue
            self._terminal(
                occurrence,
                status="missed",
                reason="window_closed_while_waiting",
                actual_end=None,
            )

    def _terminal(
        self,
        occurrence: Occurrence,
        *,
        status: str,
        reason: Optional[str],
        actual_end: Optional[datetime],
        artifact: Optional[str] = None,
    ) -> None:
        if status not in TERMINAL_STATUSES:
            raise RuntimeError(f"invalid terminal status: {status}")
        if status in {"failed", "missed"} and reason not in TERMINAL_REASONS:
            raise RuntimeError(f"invalid terminal reason: {reason}")
        if status == "completed" and reason is not None:
            raise RuntimeError("completed workflow cannot have a reason")
        occurrence.state = status
        event = {
            "window_index": occurrence.window_index,
            "sequence_index": occurrence.sequence_index,
            "workflow": occurrence.entry.workflow,
            "target_profile": self.plan.target_profile,
            "brain_profile": occurrence.entry.brain_profile,
            "scheduled_local": occurrence.scheduled_local.isoformat(),
            "scheduled_utc": self._iso(occurrence.scheduled_utc),
            "actual_start": self._iso(occurrence.actual_start),
            "actual_end": self._iso(actual_end),
            "status": status,
        }
        if occurrence.entry.instruction is not None:
            event["resolved_instruction"] = occurrence.entry.instruction
        if artifact is not None:
            event["artifact"] = artifact
        if reason is not None:
            event["reason"] = reason
        self._sink(event)

    def _window_is_open(self, occurrence: Occurrence, now: datetime) -> bool:
        local = now.astimezone(self.plan.timezone)
        minute = local.hour * 60 + local.minute
        return local.date() == occurrence.local_day and minute < occurrence.window_end_minute

    def _resolve_first_utc(self, naive: datetime) -> Optional[datetime]:
        first = naive.replace(tzinfo=self.plan.timezone, fold=0)
        utc = first.astimezone(UTC)
        roundtrip = utc.astimezone(self.plan.timezone).replace(tzinfo=None)
        if roundtrip != naive:
            return None
        return utc

    def _seconds_until_next_clock_event(self) -> Optional[float]:
        now = self._utc(self.clock.now())
        candidates = [
            occurrence.scheduled_utc
            for occurrence in self._occurrences
            if occurrence.state == "scheduled" and occurrence.scheduled_utc is not None
        ]
        local_now = now.astimezone(self.plan.timezone)
        next_midnight = datetime.combine(
            local_now.date() + timedelta(days=1), time(), self.plan.timezone
        ).astimezone(UTC)
        candidates.append(next_midnight)
        for occurrence in self._waiting:
            close_naive = datetime.combine(occurrence.local_day, time()) + timedelta(
                minutes=occurrence.window_end_minute
            )
            close_utc = self._resolve_first_utc(close_naive)
            if close_utc is not None:
                candidates.append(close_utc)
        future = [candidate for candidate in candidates if candidate > now]
        if not future:
            return None
        return max(0.0, (min(future) - now).total_seconds())

    def _start_future(self, task: ResolvedTask, local_day: str) -> Future:
        assert self._pool is not None
        return self._pool.submit(self.registry.execute, task, local_day)

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            raise RuntimeError("workflow executor clock must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _iso(value: Optional[datetime]) -> Optional[str]:
        if value is None:
            return None
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
