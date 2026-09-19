from __future__ import annotations

import asyncio

import pytest
from quart import Quart
from quart import websocket
from quart.app import ConnectionTracker
from quart.testing.connections import WebsocketDisconnectError


async def test_connection_tracker_drains() -> None:
    tracker = ConnectionTracker()
    assert tracker.state == "ready"
    assert tracker.is_accepting
    assert tracker.active_count == 0

    completed = False

    async def connection() -> None:
        nonlocal completed
        await asyncio.sleep(0.05)
        completed = True

    task = asyncio.ensure_future(connection())
    tracker.add(task)
    assert tracker.active_count == 1

    wait_task = asyncio.ensure_future(tracker.wait_drained())
    await asyncio.sleep(0)
    assert not wait_task.done()

    await task
    tracker.discard(task)
    assert completed
    await asyncio.wait_for(wait_task, timeout=1)
    assert tracker.active_count == 0


async def test_connection_tracker_force_close() -> None:
    tracker = ConnectionTracker()
    started = asyncio.Event()

    async def connection() -> None:
        started.set()
        await asyncio.sleep(30)

    task = asyncio.ensure_future(connection())
    await started.wait()
    tracker.add(task)

    await tracker.force_close()
    assert task.cancelled()
    assert tracker.active_count == 0
    # Drained once everything has been force closed
    await asyncio.wait_for(tracker.wait_drained(), timeout=1)


async def test_health_endpoint_ready() -> None:
    app = Quart(__name__)
    async with app.test_app():
        response = await app.test_client().get("/health")
        assert response.status_code == 200
        data = await response.get_json()
        assert data["status"] == "ready"
        assert data["active_connections"] == 0


async def test_health_endpoint_shutting_down() -> None:
    app = Quart(__name__)
    async with app.test_app():
        app.connection_tracker.begin_shutdown()
        response = await app.test_client().get("/health")
        assert response.status_code == 503
        data = await response.get_json()
        assert data["status"] == "shutting_down"


async def test_health_endpoint_disabled() -> None:
    app = Quart(__name__)
    app.config["PROVIDE_HEALTH_ENDPOINT"] = False
    async with app.test_app():
        response = await app.test_client().get("/health")
        assert response.status_code == 404


async def test_health_endpoint_user_defined() -> None:
    app = Quart(__name__)

    @app.route("/health")
    async def health() -> str:
        return "custom"

    async with app.test_app():
        response = await app.test_client().get("/health")
        assert response.status_code == 200
        assert await response.get_data(as_text=True) == "custom"


async def test_request_rejected_when_shutting_down() -> None:
    app = Quart(__name__)

    @app.route("/")
    async def index() -> str:
        return "ok"

    async with app.test_app():
        app.connection_tracker.begin_shutdown()
        response = await app.test_client().get("/")
        assert response.status_code == 503
        assert await response.get_data(as_text=True) == "Server is shutting down"


async def test_websocket_rejected_when_shutting_down() -> None:
    app = Quart(__name__)

    @app.websocket("/ws")
    async def ws() -> None:
        await websocket.accept()

    async with app.test_app():
        app.connection_tracker.begin_shutdown()
        with pytest.raises(WebsocketDisconnectError) as exc_info:
            async with app.test_client().websocket("/ws") as connection:
                await connection.receive()
        assert exc_info.value.args[0] == 1012  # Service Restart


async def test_shutdown_waits_for_inflight_request() -> None:
    app = Quart(__name__)
    app.config["GRACEFUL_SHUTDOWN_TIMEOUT"] = 5
    started = asyncio.Event()
    release = asyncio.Event()

    @app.route("/slow")
    async def slow() -> str:
        started.set()
        await release.wait()
        return "done"

    test_app = app.test_app()
    await test_app.startup()
    client = test_app.test_client()

    request_task = asyncio.ensure_future(client.get("/slow"))
    await started.wait()
    assert app.connection_tracker.active_count == 1

    shutdown_task = asyncio.ensure_future(test_app.shutdown())
    await asyncio.sleep(0.1)
    # Shutdown is waiting for the in-flight request to complete
    assert not shutdown_task.done()
    assert app.connection_tracker.state == "shutting_down"

    release.set()
    response = await asyncio.wait_for(request_task, timeout=5)
    assert response.status_code == 200
    assert await response.get_data(as_text=True) == "done"

    await asyncio.wait_for(shutdown_task, timeout=5)
    assert app.connection_tracker.state == "shutdown"
    assert app.connection_tracker.active_count == 0


async def test_shutdown_waits_for_websocket() -> None:
    app = Quart(__name__)
    app.config["GRACEFUL_SHUTDOWN_TIMEOUT"] = 5
    started = asyncio.Event()
    release = asyncio.Event()

    @app.websocket("/ws")
    async def ws() -> None:
        await websocket.accept()
        started.set()
        await release.wait()
        await websocket.send("done")

    test_app = app.test_app()
    await test_app.startup()
    client = test_app.test_client()

    async def run_websocket() -> str:
        async with client.websocket("/ws") as connection:
            return await connection.receive()

    websocket_task = asyncio.ensure_future(run_websocket())
    await started.wait()
    assert app.connection_tracker.active_count == 1

    shutdown_task = asyncio.ensure_future(test_app.shutdown())
    await asyncio.sleep(0.1)
    assert not shutdown_task.done()

    release.set()
    assert await asyncio.wait_for(websocket_task, timeout=5) == "done"
    await asyncio.wait_for(shutdown_task, timeout=5)
    assert app.connection_tracker.state == "shutdown"


async def test_shutdown_force_closes_after_timeout() -> None:
    app = Quart(__name__)
    app.config["GRACEFUL_SHUTDOWN_TIMEOUT"] = 0.2
    started = asyncio.Event()

    @app.route("/slow")
    async def slow() -> str:
        started.set()
        await asyncio.sleep(30)
        return "done"

    test_app = app.test_app()
    await test_app.startup()
    client = test_app.test_client()

    request_task = asyncio.ensure_future(client.get("/slow"))
    await started.wait()

    await asyncio.wait_for(test_app.shutdown(), timeout=5)
    assert app.connection_tracker.state == "shutdown"
    assert app.connection_tracker.active_count == 0
    assert request_task.done()
    with pytest.raises(asyncio.CancelledError):
        await request_task


async def test_health_available_during_graceful_shutdown() -> None:
    app = Quart(__name__)
    app.config["GRACEFUL_SHUTDOWN_TIMEOUT"] = 5
    started = asyncio.Event()
    release = asyncio.Event()

    @app.route("/slow")
    async def slow() -> str:
        started.set()
        await release.wait()
        return "done"

    test_app = app.test_app()
    await test_app.startup()
    client = test_app.test_client()

    request_task = asyncio.ensure_future(client.get("/slow"))
    await started.wait()

    shutdown_task = asyncio.ensure_future(test_app.shutdown())
    await asyncio.sleep(0.1)

    # The health endpoint keeps responding whilst draining so that
    # readiness probes observe the shutting_down state.
    response = await client.get("/health")
    assert response.status_code == 503
    assert (await response.get_json())["status"] == "shutting_down"

    # New (non health) requests are rejected during the drain
    response = await client.get("/slow")
    assert response.status_code == 503

    release.set()
    await asyncio.wait_for(request_task, timeout=5)
    await asyncio.wait_for(shutdown_task, timeout=5)
    assert app.connection_tracker.state == "shutdown"


async def test_background_tasks_complete_on_shutdown() -> None:
    app = Quart(__name__)
    completed = False

    async def background() -> None:
        nonlocal completed
        await asyncio.sleep(0.2)
        completed = True

    @app.before_serving
    async def startup() -> None:
        app.add_background_task(background)

    async with app.test_app():
        assert not completed

    assert completed


async def test_run_task_aligns_hypercorn_graceful_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Quart(__name__)
    app.config["GRACEFUL_SHUTDOWN_TIMEOUT"] = 3
    captured = {}

    async def fake_serve(app_, config, shutdown_trigger=None):  # type: ignore[no-untyped-def]
        captured["graceful_timeout"] = config.graceful_timeout

    monkeypatch.setattr("quart.app.serve", fake_serve)
    await app.run_task()
    assert captured["graceful_timeout"] == 3
