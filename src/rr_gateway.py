"""Single-worker request-level RR lab proxy with SSE backpressure and no retries."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web


@dataclass(frozen=True)
class Endpoint:
    uid: str
    url: str
    name: str


class RoundRobin:
    def __init__(self):
        self.endpoints = []
        self.index = 0
        self.epoch = 0
        self.sequence = 0

    def update(self, endpoints):
        endpoints = sorted(endpoints, key=lambda e: e.uid)
        if endpoints != self.endpoints:
            self.endpoints = endpoints
            self.index = 0
            self.epoch += 1

    def select(self):
        if not self.endpoints:
            raise web.HTTPServiceUnavailable(text="no Ready replica endpoints")
        # No await between selection and increment: atomic in this one event loop.
        endpoint = self.endpoints[self.index % len(self.endpoints)]
        self.index = (self.index + 1) % len(self.endpoints)
        self.sequence += 1
        return endpoint, self.epoch, self.sequence


def endpoints_from_slices(payload, port=8081):
    endpoints = {}
    for item in payload.get("items", []):
        ports = [p["port"] for p in item.get("ports", []) if p.get("name") == "http"]
        if not ports or ports[0] != port:
            continue
        for row in item.get("endpoints", []):
            conditions, target = row.get("conditions", {}), row.get("targetRef", {})
            if conditions.get("ready") is not True or conditions.get("terminating") is True:
                continue
            if target.get("kind") != "Pod" or not target.get("uid"):
                continue
            address = row["addresses"][0]
            address = f"[{address}]" if ":" in address else address
            endpoints[target["uid"]] = Endpoint(target["uid"], f"http://{address}:{port}", target["name"])
    return list(endpoints.values())


def emit(record):
    print(json.dumps(record, allow_nan=False), flush=True)


async def discovery(app):
    """EndpointSlice readiness plus generation health; stale discovery fails closed."""
    import ssl
    from pathlib import Path
    service = os.environ.get("DISCOVERY_SERVICE")
    static = app["static_endpoints"]
    ca = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    context = ssl.create_default_context(cafile=ca) if service else None
    namespace = os.environ.get("POD_NAMESPACE", "serving-lab")
    while True:
        try:
            candidates = static
            if service:
                token = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text().strip()
                url = f"https://kubernetes.default.svc/apis/discovery.k8s.io/v1/namespaces/{namespace}/endpointslices"
                async with app["session"].get(url, params={"labelSelector": f"kubernetes.io/service-name={service}"},
                        headers={"Authorization": "Bearer " + token}, ssl=context, timeout=ClientTimeout(total=5)) as response:
                    response.raise_for_status()
                    candidates = endpoints_from_slices(await response.json())
            async def healthy(endpoint):
                try:
                    async with app["session"].get(endpoint.url + "/health", timeout=ClientTimeout(total=2)) as response:
                        return endpoint if response.status == 200 else None
                except Exception:
                    return None
            ready = [e for e in await asyncio.gather(*(healthy(e) for e in candidates)) if e]
            app["pool"].update(ready)
            app["emit"](dict(event="endpoint_snapshot", timestamp=time.time(), epoch=app["pool"].epoch,
                             endpoints=[dict(uid=e.uid, name=e.name, url=e.url) for e in ready]))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            app["pool"].update([])
            app["emit"](dict(event="discovery_error", error_type=type(error).__name__, timestamp=time.time()))
        await asyncio.sleep(app["discovery_interval"])


async def proxy(request):
    app = request.app
    if app["state"]["draining"]:
        raise web.HTTPServiceUnavailable(text="draining")
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType()
    try:
        payload = await request.json()
    except (ValueError, UnicodeError):
        raise web.HTTPBadRequest(text="invalid JSON") from None
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text="expected JSON object")
    rid = request.headers.get("X-Request-ID") or payload.get("request_id") or uuid.uuid4().hex
    if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", rid):
        raise web.HTTPBadRequest(text="invalid request ID")
    if payload.get("request_id", rid) != rid:
        raise web.HTTPBadRequest(text="header/body request ID mismatch")
    payload["request_id"] = rid
    endpoint, epoch, sequence = app["pool"].select()
    start = time.monotonic()
    record = dict(event="request", request_id=rid, gateway_id=app["identity"], epoch=epoch,
                  sequence=sequence, upstream=endpoint.name, pod_uid=endpoint.uid,
                  ready_uids=[e.uid for e in app["pool"].endpoints], upstream_attempts=1,
                  admitted_at=time.time(), path=request.path, status="incomplete")
    response = None
    try:
        # An explicit client timeout covers the entire stream. Redirects/retries are disabled.
        async with app["session"].post(endpoint.url + request.path, json=payload,
                headers={"X-Request-ID": rid}, allow_redirects=False) as upstream:
            record["http_status"] = upstream.status
            record["upstream_headers_ms"] = (time.monotonic() - start) * 1000
            response = web.StreamResponse(status=upstream.status, headers={
                "Content-Type": upstream.headers.get("Content-Type", "application/octet-stream"),
                "X-Request-ID": rid, "X-Backend-Pod-UID": endpoint.uid,
                "X-Backend-Name": endpoint.name, "Cache-Control": "no-cache"})
            await response.prepare(request)
            total, tail, done = 0, b"", False
            async for chunk in upstream.content.iter_any():
                total += len(chunk)
                if total > app["max_response_bytes"]:
                    raise ValueError("response exceeds configured bound")
                record.setdefault("first_body_ms", (time.monotonic() - start) * 1000)
                combined = tail + chunk
                done |= b"data: [DONE]" in combined
                tail = combined[-32:]
                # write() applies downstream backpressure, without aggregating SSE chunks.
                await response.write(chunk)
            if payload.get("stream") and upstream.status < 400 and not done:
                raise ConnectionError("upstream stream ended without DONE")
            await response.write_eof()
            record["status"] = "completed" if upstream.status < 400 else "upstream_error"
            return response
    except asyncio.CancelledError:
        # AppRunner(handler_cancellation=True) delivers client disconnect here even while
        # upstream has produced no new bytes. Exiting the context closes upstream.
        record["status"] = "client_cancelled"
        raise
    except Exception as error:
        record.update(status="interrupted", error_type=type(error).__name__)
        if response is not None and response.prepared:
            if request.transport:
                request.transport.close()
            return response
        raise web.HTTPBadGateway(text="upstream unavailable; not retried") from None
    finally:
        record.update(completed_at=time.time(), duration_ms=(time.monotonic() - start) * 1000)
        app["emit"](record)


async def health(request):
    ready = not request.app["state"]["draining"] and bool(request.app["pool"].endpoints)
    return web.json_response(dict(ready=ready, endpoints=len(request.app["pool"].endpoints)), status=200 if ready else 503)


async def context(app):
    app["session"] = ClientSession(timeout=ClientTimeout(total=app["timeout"], connect=5),
                                   connector=TCPConnector(limit=512), auto_decompress=False)
    task = asyncio.create_task(discovery(app))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await app["session"].close()


async def shutdown(app):
    app["state"]["draining"] = True
    app["emit"](dict(event="draining", timestamp=time.time()))


def create_app(endpoints=(), *, log=emit, timeout=120, discovery_interval=1):
    app = web.Application(client_max_size=2 * 1024 * 1024)
    app.update(pool=RoundRobin(), static_endpoints=list(endpoints), emit=log,
               timeout=timeout, discovery_interval=discovery_interval, state={"draining": False},
               identity=os.environ.get("POD_UID", uuid.uuid4().hex), max_response_bytes=16 * 1024 * 1024)
    app.cleanup_ctx.append(context)
    app.on_shutdown.append(shutdown)
    app.router.add_get("/health", health)
    app.router.add_post("/v1/completions", proxy)
    app.router.add_post("/v1/chat/completions", proxy)
    return app


def main():
    endpoints = [Endpoint(**row) for row in json.loads(os.environ.get("UPSTREAMS_JSON", "[]"))]
    if os.environ.get("LOCAL_ENGINE"):
        endpoints = [Endpoint(os.environ["POD_UID"], "http://127.0.0.1:8000", os.environ["POD_NAME"])]
    app = create_app(endpoints, timeout=float(os.environ.get("REQUEST_TIMEOUT", "120")))
    web.run_app(app, host=os.environ.get("LISTEN_HOST", "127.0.0.1"),
                port=int(os.environ.get("PORT", "8080")), handler_cancellation=True,
                shutdown_timeout=130, access_log=None)


if __name__ == "__main__":
    main()
