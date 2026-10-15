"""Tests for TelemetryDispatcher live streaming: replay ring, non-blocking emit,
and the background Redis publisher.

These pin the guarantees the dispatcher makes to LangGraph nodes:
  * emit() never awaits network I/O, so a slow broker cannot slow agent execution;
  * the last 100 events per task are replayable in-process with zero I/O;
  * subscriber queues still receive events synchronously (no batch-timer lag).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from services.api.forge.engine.dispatcher import (
    DEFAULT_HISTORY_LIMIT,
    TelemetryDispatcher,
)


# ===========================================================================
# 1. PER-TASK REPLAY RING
# ===========================================================================


@pytest.mark.asyncio
async def test_replay_ring_keeps_last_100_events_per_task():
    """Only the most recent 100 events per task_id are retained for replay."""
    dispatcher = TelemetryDispatcher(flush_interval=60.0, max_batch=10000)

    for step in range(150):
        await dispatcher.emit(
            task_id="task-a",
            event_type="step",
            payload={"node": "implementer_node", "step": step},
        )

    recent = dispatcher.get_recent_events("task-a")

    assert len(recent) == DEFAULT_HISTORY_LIMIT == 100
    # Oldest 50 evicted, newest retained in chronological order
    assert recent[0]["step"] == 50
    assert recent[-1]["step"] == 149
    assert dispatcher.history_size("task-a") == 100


@pytest.mark.asyncio
async def test_replay_ring_is_isolated_per_task():
    """Each task_id keeps its own history; unknown tasks replay nothing."""
    dispatcher = TelemetryDispatcher(flush_interval=60.0, max_batch=10000)

    await dispatcher.emit(task_id="task-1", event_type="step", payload={"which": "one"})
    await dispatcher.emit(task_id="task-2", event_type="step", payload={"which": "two"})

    assert [e["which"] for e in dispatcher.get_recent_events("task-1")] == ["one"]
    assert [e["which"] for e in dispatcher.get_recent_events("task-2")] == ["two"]
    assert dispatcher.get_recent_events("never-seen") == []


@pytest.mark.asyncio
async def test_replay_limit_argument_bounds_returned_events():
    """`limit` returns only the N most-recent events."""
    dispatcher = TelemetryDispatcher(flush_interval=60.0, max_batch=10000)

    for step in range(10):
        await dispatcher.emit(task_id="task-c", event_type="step", payload={"step": step})

    assert [e["step"] for e in dispatcher.get_recent_events("task-c", limit=3)] == [7, 8, 9]
    assert dispatcher.get_recent_events("task-c", limit=0) == []


@pytest.mark.asyncio
async def test_history_lru_evicts_oldest_tracked_task():
    """`max_tracked_tasks` bounds total retained history across tasks."""
    dispatcher = TelemetryDispatcher(
        flush_interval=60.0, max_batch=10000, max_tracked_tasks=2
    )

    for task in ("t1", "t2", "t3"):
        await dispatcher.emit(task_id=task, event_type="step", payload={"t": task})

    assert dispatcher.history_size("t1") == 0  # evicted LRU
    assert dispatcher.history_size("t2") == 1
    assert dispatcher.history_size("t3") == 1


@pytest.mark.asyncio
async def test_clear_history_removes_one_or_all_tasks():
    """clear_history() targets a single task or every task."""
    dispatcher = TelemetryDispatcher(flush_interval=60.0, max_batch=10000)

    await dispatcher.emit(task_id="t1", event_type="step", payload={"a": 1})
    await dispatcher.emit(task_id="t2", event_type="step", payload={"a": 2})

    dispatcher.clear_history("t1")
    assert dispatcher.history_size("t1") == 0
    assert dispatcher.history_size("t2") == 1

    dispatcher.clear_history()
    assert dispatcher.history_size("t2") == 0


# ===========================================================================
# 2. SUBSCRIBE / REPLAY DELIVERY
# ===========================================================================


@pytest.mark.asyncio
async def test_subscribe_replay_seeds_new_client_with_history():
    """A newly connected client opting into replay receives prior events immediately."""
    dispatcher = TelemetryDispatcher(flush_interval=60.0, max_batch=10000)

    for step in range(3):
        await dispatcher.emit(task_id="task-d", event_type="step", payload={"step": step})

    queue = dispatcher.subscribe("task-d", replay=True)

    assert queue.qsize() == 3
    replayed = [queue.get_nowait()["step"] for _ in range(3)]
    assert replayed == [0, 1, 2]

    dispatcher.unsubscribe(queue, "task-d")


@pytest.mark.asyncio
async def test_subscribe_defaults_to_no_replay():
    """Replay is opt-in so callers that already replay from Redis don't see duplicates."""
    dispatcher = TelemetryDispatcher(flush_interval=60.0, max_batch=10000)
    await dispatcher.emit(task_id="task-e", event_type="step", payload={"step": 1})

    queue = dispatcher.subscribe("task-e")

    assert queue.empty()
    dispatcher.unsubscribe(queue, "task-e")


@pytest.mark.asyncio
async def test_emit_delivers_live_to_subscribers_without_batch_delay():
    """Live delivery is immediate even with a long flush interval."""
    dispatcher = TelemetryDispatcher(flush_interval=60.0, max_batch=10000)
    queue = dispatcher.subscribe("task-f")

    await dispatcher.emit(
        task_id="task-f",
        event_type="node_exit",
        payload={"node": "verifier_node", "status": "verified"},
    )

    event = queue.get_nowait()
    assert event["event_type"] == "node_exit"
    assert event["node"] == "verifier_node"
    assert event["status"] == "verified"
    assert event["event_id"]
    assert dispatcher.get_recent_events("task-f")[0]["event_id"] == event["event_id"]

    dispatcher.unsubscribe(queue, "task-f")


@pytest.mark.asyncio
async def test_emit_accepts_message_and_metadata_keywords():
    """`message` and `metadata` fold into the payload and flatten onto the event."""
    dispatcher = TelemetryDispatcher(flush_interval=60.0, max_batch=10000)
    queue = dispatcher.subscribe("task-g")

    await dispatcher.emit(
        "task-g",
        "node_entry",
        payload={"node": "verifier_node"},
        message="Verifying patched worktree",
        metadata={"retry_count": 2},
    )

    event = queue.get_nowait()
    assert event["message"] == "Verifying patched worktree"
    assert event["node"] == "verifier_node"
    assert event["retry_count"] == 2
    assert event["payload"]["retry_count"] == 2

    dispatcher.unsubscribe(queue, "task-g")


# ===========================================================================
# 3. NON-BLOCKING EMIT & BACKGROUND PUBLISHER
# ===========================================================================


@pytest.mark.asyncio
async def test_emit_never_awaits_redis_on_the_hot_path():
    """emit() must not block on Redis publication, however slow the broker is."""
    dispatcher = TelemetryDispatcher(flush_interval=60.0, max_batch=10000)

    async def slow_publish(batch):
        await asyncio.sleep(5.0)
        return True

    dispatcher._publish_events_to_redis = slow_publish  # type: ignore[method-assign]

    await dispatcher.start()
    try:
        started = time.perf_counter()
        for step in range(20):
            await dispatcher.emit(
                task_id="task-h", event_type="step", payload={"step": step}
            )
        elapsed = time.perf_counter() - started
    finally:
        await dispatcher.stop(timeout=1.0)

    assert elapsed < 1.0, f"emit() blocked on Redis publication for {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_publish_queue_drops_oldest_when_saturated():
    """A saturated publish queue drops the oldest events rather than blocking emit()."""
    dispatcher = TelemetryDispatcher(
        flush_interval=60.0, max_batch=10000, publish_queue_capacity=2
    )

    for step in range(5):
        await dispatcher.emit(task_id="task-i", event_type="step", payload={"step": step})

    assert dispatcher._publish_queue.qsize() == 2
    assert dispatcher.stats["publish_dropped_count"] == 3


@pytest.mark.asyncio
async def test_start_and_stop_manage_the_publisher_task():
    """The publisher runs only between start() and stop()."""
    dispatcher = TelemetryDispatcher(flush_interval=60.0, max_batch=10000)

    await dispatcher.start()
    assert dispatcher.is_running
    assert dispatcher._publish_task is not None
    assert not dispatcher._publish_task.done()

    await dispatcher.stop(timeout=1.0)
    assert not dispatcher.is_running
    assert dispatcher._publish_task is None


@pytest.mark.asyncio
async def test_stats_expose_streaming_metrics():
    """Operational stats surface replay and publish health."""
    dispatcher = TelemetryDispatcher(flush_interval=60.0, max_batch=10000)

    await dispatcher.emit(task_id="task-j", event_type="step", payload={"step": 1})
    stats = dispatcher.stats

    for key in (
        "history_limit",
        "history_tracked_tasks",
        "history_buffered_events",
        "publish_queue_size",
        "publish_queue_capacity",
        "published_count",
        "publish_dropped_count",
        "global_subscribers",
    ):
        assert key in stats, f"missing stats key: {key}"

    assert stats["history_tracked_tasks"] == 1
    assert stats["history_buffered_events"] == 1
    assert stats["history_limit"] == DEFAULT_HISTORY_LIMIT
