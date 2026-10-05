import asyncio

import pytest
from aiohttp import ClientSession, web

from src.rr_gateway import Endpoint, RoundRobin, create_app, endpoints_from_slices

pytestmark = pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")


async def start(app, **kwargs):
    runner = web.AppRunner(app, handler_cancellation=True, shutdown_timeout=.2, **kwargs)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


def test_pool_excludes_unready_and_terminating_pods():
    payload = dict(items=[dict(ports=[dict(name="http", port=8081)], endpoints=[
        dict(addresses=["127.0.0.1"], targetRef=dict(kind="Pod", uid="a", name="a"), conditions=dict(ready=True)),
        dict(addresses=["127.0.0.2"], targetRef=dict(kind="Pod", uid="b", name="b"), conditions=dict(ready=True, terminating=True))])])
    assert [e.uid for e in endpoints_from_slices(payload)] == ["a"]
    pool = RoundRobin()
    pool.update([Endpoint("a", "http://a", "a"), Endpoint("b", "http://b", "b")])
    assert [pool.select()[0].uid for _ in range(5)] == ["a", "b", "a", "b", "a"]
    pool.update([])
    with pytest.raises(web.HTTPServiceUnavailable):
        pool.select()


def test_real_http_round_robin_keepalive_streaming_and_cancel():
    async def scenario():
        calls, logs, cancelled = [], [], asyncio.Event()
        release = asyncio.Event()
        transports = []
        @web.middleware
        async def connections(request, handler):
            transports.append(request.transport)
            return await handler(request)
        async def health(_):
            return web.Response(text="ok")
        async def engine(request):
            payload = await request.json()
            calls.append(payload["request_id"])
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b'data: {"choices":[{"text":"first"}]}\n\n')
            try:
                if payload.get("max_tokens") == 999:
                    await asyncio.sleep(10)
                else:
                    await release.wait()
                await response.write(b'data: [DONE]\n\n')
                return response
            except asyncio.CancelledError:
                cancelled.set()
                raise
        engine_app = web.Application()
        engine_app.router.add_get("/health", health)
        engine_app.router.add_post("/v1/completions", engine)
        backend, url = await start(engine_app)
        gateway_app = create_app([Endpoint("a", url, "a"), Endpoint("b", url, "b")], log=logs.append, discovery_interval=.02)
        gateway_app.middlewares.append(connections)
        gateway, target = await start(gateway_app)
        try:
            for _ in range(100):
                if len(gateway_app["pool"].endpoints) == 2:
                    break
                await asyncio.sleep(.01)
            async with ClientSession() as client:
                seen = []
                for i in range(4):
                    release.clear()
                    async with client.post(target + "/v1/completions", json=dict(request_id=f"r{i}", stream=True)) as response:
                        seen.append(response.headers["X-Backend-Pod-UID"])
                        line = await asyncio.wait_for(response.content.readline(), 2)
                        assert b"first" in line
                        release.set()
                        assert b"[DONE]" in await response.read()
                assert seen == ["a", "b", "a", "b"]
                assert len({id(t) for t in transports}) == 1
                response = await client.post(target + "/v1/completions", json=dict(request_id="cancel", stream=True, max_tokens=999))
                await response.content.readline()
                response.close()
                await asyncio.wait_for(cancelled.wait(), 2)
                await asyncio.sleep(.03)
            assert calls == ["r0", "r1", "r2", "r3", "cancel"]
            assert any(r.get("status") == "client_cancelled" for r in logs)
            assert all(r["upstream_attempts"] == 1 for r in logs if r.get("event") == "request")
        finally:
            await gateway.cleanup()
            await backend.cleanup()
    asyncio.run(scenario())


def test_broken_stream_is_not_completed_or_retried():
    async def scenario():
        calls, logs = [], []
        async def broken(request):
            calls.append(1)
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b'data: {"choices":[]}\n\n')
            return response
        app = web.Application()
        async def health(_):
            return web.Response()
        app.router.add_get("/health", health)
        app.router.add_post("/v1/completions", broken)
        backend, url = await start(app)
        gateway_app = create_app([Endpoint("a", url, "a")], log=logs.append, discovery_interval=.01)
        gateway, target = await start(gateway_app)
        try:
            for _ in range(100):
                if gateway_app["pool"].endpoints:
                    break
                await asyncio.sleep(.01)
            async with ClientSession() as client:
                with pytest.raises(Exception):
                    async with client.post(target + "/v1/completions", json=dict(request_id="x", stream=True)) as response:
                        await response.read()
            assert len(calls) == 1
            assert [r["status"] for r in logs if r.get("event") == "request"] == ["interrupted"]
        finally:
            await gateway.cleanup()
            await backend.cleanup()
    asyncio.run(scenario())
