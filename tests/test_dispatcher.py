"""Comprehensive test suite for the asynchronous TelemetryDispatcher:
1. Active background flushing loop lifecycle (start/stop)
2. Batch threshold triggering (MAX_BATCH)
3. Time interval triggering (FLUSH_INTERVAL)
4. Thread-safe concurrent buffering with asyncio.Lock (test_concurrent_flushes)
5. Buffer capacity and overflow pressure policy (test_overflow_pressure)
6. Concrete sink emission with retry resilience on network/sink drops
7. File sink JSONL output
8. Context manager support (async with)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from services.api.forge.telemetry.dispatcher import TelemetryDispatcher


@pytest.mark.asyncio
async def test_dispatcher_start_stop_lifecycle():
    """Verify start() launches background task and stop() drains remaining buffer."""
    emitted_batches: List[List[Dict[str, Any]]] = []

    async def mock_sink(batch: List[Dict[str, Any]]) -> None:
        emitted_batches.append(batch)

    dispatcher = TelemetryDispatcher(
        max_batch=10,
        flush_interval=5.0,
        sink=mock_sink,
    )

    assert not dispatcher.is_running
    assert dispatcher.buffer_size == 0

    await dispatcher.start()
    assert dispatcher.is_running
    assert dispatcher._flush_task is not None
    assert not dispatcher._flush_task.done()

    # Enqueue a few items
    await dispatcher.record({"event": "node_start", "node": "planner"})
    await dispatcher.record({"event": "node_end", "node": "planner"})

    # Stop dispatcher - must drain remaining buffer
    await dispatcher.stop()
    assert not dispatcher.is_running
    assert dispatcher.buffer_size == 0

    # Ensure remaining 2 events were emitted
    all_events = [item for batch in emitted_batches for item in batch]
    assert len(all_events) == 2
    assert all_events[0]["node"] == "planner"


@pytest.mark.asyncio
async def test_batch_size_trigger():
    """Verify that buffer reaching max_batch triggers immediate flush without waiting for interval."""
    emitted_batches: List[List[Dict[str, Any]]] = []
    flush_signaled = asyncio.Event()

    async def mock_sink(batch: List[Dict[str, Any]]) -> None:
        emitted_batches.append(batch)
        flush_signaled.set()

    # Set long flush interval (60s), so flush ONLY happens via batch size trigger
    dispatcher = TelemetryDispatcher(
        max_batch=5,
        flush_interval=60.0,
        sink=mock_sink,
    )

    await dispatcher.start()
    try:
        # Enqueue 4 items - below threshold, should not flush yet
        for i in range(4):
            await dispatcher.record({"index": i})

        await asyncio.sleep(0.05)
        assert len(emitted_batches) == 0
        assert dispatcher.buffer_size == 4

        # Enqueue 5th item - reaches max_batch (5), should immediately trigger flush
        await dispatcher.record({"index": 4})

        # Wait for flush to be triggered
        await asyncio.wait_for(flush_signaled.wait(), timeout=2.0)

        assert len(emitted_batches) == 1
        assert len(emitted_batches[0]) == 5
        assert dispatcher.buffer_size == 0
    finally:
        await dispatcher.stop()


@pytest.mark.asyncio
async def test_interval_trigger():
    """Verify that events below max_batch are flushed automatically on time interval."""
    emitted_batches: List[List[Dict[str, Any]]] = []
    interval_flushed = asyncio.Event()

    async def mock_sink(batch: List[Dict[str, Any]]) -> None:
        emitted_batches.append(batch)
        interval_flushed.set()

    # Fast flush interval of 0.15 seconds
    dispatcher = TelemetryDispatcher(
        max_batch=50,
        flush_interval=0.15,
        sink=mock_sink,
    )

    await dispatcher.start()
    try:
        # Enqueue only 2 items (well below 50)
        await dispatcher.record({"metric": "cpu", "value": 42})
        await dispatcher.record({"metric": "mem", "value": 85})

        assert len(emitted_batches) == 0

        # Wait for interval timer to trigger flush
        await asyncio.wait_for(interval_flushed.wait(), timeout=1.5)

        assert len(emitted_batches) == 1
        assert len(emitted_batches[0]) == 2
        assert dispatcher.buffer_size == 0
    finally:
        await dispatcher.stop()


@pytest.mark.asyncio
async def test_concurrent_flushes():
    """Verify thread-safe concurrency: 50 concurrent tasks recording simultaneously."""
    emitted_batches: List[List[Dict[str, Any]]] = []
    lock = asyncio.Lock()

    async def thread_safe_sink(batch: List[Dict[str, Any]]) -> None:
        async with lock:
            emitted_batches.append(batch)

    dispatcher = TelemetryDispatcher(
        max_batch=20,
        flush_interval=0.2,
        buffer_capacity=2000,
        sink=thread_safe_sink,
    )

    await dispatcher.start()
    try:
        total_producers = 40
        items_per_producer = 15
        expected_total = total_producers * items_per_producer

        async def producer(p_id: int):
            for i in range(items_per_producer):
                await dispatcher.record({"producer": p_id, "item": i})

        # Run 40 concurrent producers writing at the same time
        await asyncio.gather(*(producer(p) for p in range(total_producers)))

        # Stop will drain everything remaining
        await dispatcher.stop()

        all_emitted = [item for batch in emitted_batches for item in batch]
        assert len(all_emitted) == expected_total, f"Expected {expected_total}, got {len(all_emitted)}"
        assert dispatcher.stats["total_recorded"] == expected_total
        assert dispatcher.stats["total_emitted"] == expected_total
        assert dispatcher.stats["dropped_count"] == 0
    finally:
        if dispatcher.is_running:
            await dispatcher.stop()


@pytest.mark.asyncio
async def test_overflow_pressure():
    """Verify buffer capacity limit drops oldest items under overflow pressure without crashing."""
    dispatcher = TelemetryDispatcher(
        max_batch=100,
        flush_interval=60.0,
        buffer_capacity=10,  # Small capacity to test overflow
    )

    # Don't start flush loop, directly test buffer capacity behavior
    for i in range(25):
        await dispatcher.record({"seq": i})

    assert dispatcher.buffer_size == 10
    assert dispatcher.stats["dropped_count"] == 15
    assert dispatcher.stats["total_recorded"] == 25

    # Oldest 15 items were dropped, so remaining items should have seq 15..24
    async with dispatcher._lock:
        remaining_seqs = [item["seq"] for item in dispatcher._buffer]
    assert remaining_seqs == list(range(15, 25))


@pytest.mark.asyncio
async def test_sink_emission_retry_resilience():
    """Verify network/sink exceptions are retried with backoff and don't crash the dispatcher."""
    attempts = 0

    async def flaky_sink(batch: List[Dict[str, Any]]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("Transient network drop to collector")
        # Succeeds on 3rd attempt

    dispatcher = TelemetryDispatcher(
        max_batch=10,
        flush_interval=5.0,
        retry_backoff=0.01,
        max_retries=3,
        sink=flaky_sink,
    )

    await dispatcher.record({"alert": "high_latency"})
    flushed = await dispatcher.flush()

    assert flushed == 1
    assert attempts == 3
    assert dispatcher.stats["failed_emissions_count"] == 0
    assert dispatcher.stats["total_emitted"] == 1


@pytest.mark.asyncio
async def test_sink_permanent_failure_fallback():
    """Verify permanently failing sink falls back gracefully without raising unhandled exception."""

    async def broken_sink(batch: List[Dict[str, Any]]) -> None:
        raise RuntimeError("Fatal collector crash")

    dispatcher = TelemetryDispatcher(
        max_batch=5,
        flush_interval=5.0,
        retry_backoff=0.01,
        max_retries=2,
        sink=broken_sink,
    )

    await dispatcher.record({"status": "failed"})
    flushed = await dispatcher.flush()

    assert flushed == 1
    assert dispatcher.stats["failed_emissions_count"] == 1


@pytest.mark.asyncio
async def test_file_sink_jsonl_output(tmp_path: Path):
    """Verify local file sink appends valid JSONL telemetry entries."""
    log_file = tmp_path / "telemetry.jsonl"
    dispatcher = TelemetryDispatcher(
        max_batch=10,
        log_file_path=log_file,
    )

    await dispatcher.record({"event": "span_start", "trace_id": "abc123"})
    await dispatcher.record({"event": "span_end", "trace_id": "abc123", "duration_ms": 14.5})

    await dispatcher.flush(force_all=True)

    assert log_file.exists()
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

    parsed_0 = json.loads(lines[0])
    parsed_1 = json.loads(lines[1])
    assert parsed_0["event"] == "span_start"
    assert parsed_1["duration_ms"] == 14.5


@pytest.mark.asyncio
async def test_async_context_manager():
    """Verify async with TelemetryDispatcher properly manages start and stop."""
    flushed_items: List[Dict[str, Any]] = []

    async def test_sink(batch: List[Dict[str, Any]]) -> None:
        flushed_items.extend(batch)

    async with TelemetryDispatcher(max_batch=50, flush_interval=0.5, sink=test_sink) as disp:
        assert disp.is_running
        await disp.record({"metric": "rps", "val": 250})

    # Exited context manager: dispatcher must be stopped and buffer drained
    assert not disp.is_running
    assert len(flushed_items) == 1
    assert flushed_items[0]["metric"] == "rps"


@pytest.mark.asyncio
async def test_record_nowait_helper():
    """Verify record_nowait schedules recording without blocking caller."""
    dispatcher = TelemetryDispatcher()
    success = dispatcher.record_nowait({"sync_event": "from_logger"})
    assert success is True
    # Allow event loop iteration to process scheduled task
    await asyncio.sleep(0.01)
    assert dispatcher.buffer_size == 1
