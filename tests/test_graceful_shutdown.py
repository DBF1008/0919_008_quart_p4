from __future__ import annotations

import asyncio
import time

import pytest
from hypercorn.typing import HTTPScope
from hypercorn.typing import WebsocketScope

from quart import Quart
from quart.app import ConnectionTracker
from quart.app import ShutdownState
from quart.asgi import ASGIHTTPConnection
from quart.asgi import ASGILifespan
from quart.asgi import ASGIWebsocketConnection


@pytest.fixture(name="app")
def _app() -> Quart:
    app = Quart(__name__, static_folder=None)

    @app.route("/slow")
    async def slow() -> str:
        await asyncio.sleep(0.3)
        return "done"

    @app.route("/veryslow")
    async def veryslow() -> str:
        await asyncio.sleep(30)
        return "late"

    @app.websocket("/ws")
    async def ws() -> None:
        pass

    return app


def _http_scope(path: str = "/slow") -> HTTPScope:
    return {
        "type": "http",
        "asgi": {"spec_version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"quart")],
        "client": ("127.0.0.1", 80),
        "server": ("127.0.0.1", 5000),
        "extensions": {},
        "state": {},  # type: ignore[typeddict-item]
    }


def _websocket_scope(path: str = "/ws") -> WebsocketScope:
    return {
        **_http_scope(path),  # type: ignore[typeddict-item]
        "type": "websocket",
        "subprotocols": [],
    }


async def test_health_endpoint_registered(app: Quart) -> None:
    rules = {rule.endpoint: rule.rule for rule in app.url_map.iter_rules()}
    assert rules.get("_quart_health") == "/health"


async def test_health_ready(app: Quart) -> None:
    test_client = app.test_client()
    response = await test_client.get("/health")
    assert response.status_code == 200
    data = await response.get_json()
    assert data == {"status": "ready", "active_connections": 0}


async def test_health_shutting_down(app: Quart) -> None:
    app.connection_tracker.begin_shutdown()
    test_client = app.test_client()
    response = await test_client.get("/health")
    assert response.status_code == 503
    data = await response.get_json()
    assert data["status"] == "shutting_down"
    assert response.headers["Connection"] == "close"


async def test_health_shutdown(app: Quart) -> None:
    app.connection_tracker.begin_shutdown()
    app.connection_tracker.complete_shutdown()
    test_client = app.test_client()
    response = await test_client.get("/health")
    assert response.status_code == 503
    data = await response.get_json()
    assert data["status"] == "shutdown"


async def test_tracker_registers_and_drains() -> None:
    tracker = ConnectionTracker()
    assert tracker.state is ShutdownState.READY
    assert tracker.is_accepting()
    assert tracker.active_count == 0

    finished = asyncio.Event()

    async def worker() -> None:
        await finished.wait()

    task = asyncio.ensure_future(worker())
    await asyncio.sleep(0)
    assert tracker.register(task) is True
    assert tracker.active_count == 1
    assert await tracker.wait_drained(timeout=0.01) is False
    finished.set()
    await asyncio.sleep(0.02)
    assert tracker.active_count == 0
    assert await tracker.wait_drained(timeout=0.01) is True


async def test_tracker_rejects_after_shutdown_begins() -> None:
    tracker = ConnectionTracker()
    tracker.begin_shutdown()
    task = asyncio.ensure_future(asyncio.sleep(0))
    assert tracker.register(task) is False
    assert tracker.active_count == 0
    await task


async def test_tracker_force_close() -> None:
    tracker = ConnectionTracker()
    started = asyncio.Event()

    async def worker() -> None:
        started.set()
        await asyncio.sleep(30)

    task = asyncio.ensure_future(worker())
    tracker.register(task)
    await started.wait()
    tracker.begin_shutdown()
    assert await tracker.wait_drained(timeout=0.01) is False
    await tracker.force_close()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tracker.active_count == 0
    tracker.complete_shutdown()
    assert tracker.state is ShutdownState.SHUTDOWN


async def test_http_connection_rejected_while_draining(app: Quart) -> None:
    app.connection_tracker.begin_shutdown()
    connection = ASGIHTTPConnection(app, _http_scope())
    messages = []

    async def receive() -> dict:
        raise NotImplementedError

    async def send(message: dict) -> None:
        messages.append(message)

    await connection(receive, send)
    start = [m for m in messages if m["type"] == "http.response.start"][0]
    assert start["status"] == 503
    assert (b"connection", b"close") in start["headers"]
    assert app.connection_tracker.active_count == 0


async def test_health_still_served_while_draining(app: Quart) -> None:
    app.connection_tracker.begin_shutdown()
    connection = ASGIHTTPConnection(app, _http_scope("/health"))
    messages = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    await connection(receive, send)
    start = [m for m in messages if m["type"] == "http.response.start"][0]
    assert start["status"] == 503
    assert app.connection_tracker.active_count == 0


async def test_websocket_rejected_while_draining(app: Quart) -> None:
    app.connection_tracker.begin_shutdown()
    connection = ASGIWebsocketConnection(app, _websocket_scope())
    messages = []

    async def receive() -> dict:
        raise NotImplementedError

    async def send(message: dict) -> None:
        messages.append(message)

    await connection(receive, send)
    assert any(
        message["type"] == "websocket.close" and message["code"] == 1001
        for message in messages
    )


async def _run_connection(app: Quart, path: str = "/slow") -> asyncio.Task:
    connection = ASGIHTTPConnection(app, _http_scope(path))
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait({"type": "http.request", "body": b"", "more_body": False})

    async def receive() -> dict:
        return await queue.get()

    async def send(message: dict) -> None:
        pass

    return asyncio.ensure_future(connection(receive, send))


async def test_inflight_completes_within_timeout(app: Quart) -> None:
    connection_task = await _run_connection(app)
    await asyncio.sleep(0.05)
    assert app.connection_tracker.active_count == 1
    started = time.monotonic()
    await app.start_shutdown()
    assert app.shutdown_event.is_set()
    assert app.connection_tracker.state is ShutdownState.SHUTTING_DOWN
    await asyncio.wait_for(connection_task, timeout=1.0)
    elapsed = time.monotonic() - started
    assert 0.2 < elapsed < 1.0
    assert app.connection_tracker.active_count == 0


async def test_straggler_is_force_closed(app: Quart) -> None:
    app.config["GRACEFUL_SHUTDOWN_TIMEOUT"] = 0.1
    connection_task = await _run_connection(app, "/veryslow")
    await asyncio.sleep(0.05)
    assert app.connection_tracker.active_count == 1
    started = time.monotonic()
    await app.shutdown()
    elapsed = time.monotonic() - started
    assert 0.05 < elapsed < 1.5
    assert connection_task.done()
    assert app.connection_tracker.state is ShutdownState.SHUTDOWN


async def test_start_shutdown_is_idempotent(app: Quart) -> None:
    await app.start_shutdown()
    await app.start_shutdown()
    assert app.connection_tracker.state is ShutdownState.SHUTTING_DOWN
    assert app.shutdown_event.is_set()


async def test_background_tasks_awaited_on_shutdown(app: Quart) -> None:
    completed = []

    async def background() -> None:
        await asyncio.sleep(0.1)
        completed.append(None)

    async with app.app_context():
        app.add_background_task(background)
    assert len(app.background_tasks) == 1
    await app.shutdown()
    assert completed == [None]
    assert len(app.background_tasks) == 0


async def test_background_tasks_cancelled_after_timeout(app: Quart) -> None:
    app.config["BACKGROUND_TASK_SHUTDOWN_TIMEOUT"] = 0.1

    async def background() -> None:
        await asyncio.sleep(30)

    async with app.app_context():
        app.add_background_task(background)
    task = next(iter(app.background_tasks))
    started = time.monotonic()
    await app.shutdown()
    assert (time.monotonic() - started) < 1.5
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


async def test_lifespan_waits_for_drain_before_complete(app: Quart) -> None:
    scope = {"type": "lifespan", "asgi": {"spec_version": "2.0"}, "state": {}}
    lifespan = ASGILifespan(app, scope)  # type: ignore[arg-type]
    in_queue: asyncio.Queue = asyncio.Queue()
    out_queue: asyncio.Queue = asyncio.Queue()

    async def receive() -> dict:
        return await in_queue.get()

    async def send(message: dict) -> None:
        await out_queue.put(message)

    task = asyncio.ensure_future(lifespan(receive, send))
    await in_queue.put({"type": "lifespan.startup"})
    assert (await out_queue.get())["type"] == "lifespan.startup.complete"

    connection_task = await _run_connection(app)
    await asyncio.sleep(0.05)
    assert app.connection_tracker.active_count == 1

    await in_queue.put({"type": "lifespan.shutdown"})
    await asyncio.sleep(0.1)
    assert out_queue.empty()

    await asyncio.wait_for(connection_task, timeout=1.0)
    message = await asyncio.wait_for(out_queue.get(), timeout=1.0)
    assert message["type"] == "lifespan.shutdown.complete"
    await task
