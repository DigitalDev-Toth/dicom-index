"""
Mock relay server + connector test.

Start a fake relay on localhost, run the connector against it, send a
proxied /health request, assert the chunked response comes back intact.
"""

import asyncio
import base64
import json
import random
import struct
import sys

import aiohttp
from aiohttp import web
import pytest

HOST = "127.0.0.1"
RELAY_PORT = random.randint(19000, 19999)
API_PORT = random.randint(20000, 29999)
RELAY_BASE = f"http://{HOST}:{RELAY_PORT}"
UPSTREAM = f"http://{HOST}:{API_PORT}"
CLIENT = "test-relay-client"
TOKEN = "test-token-12345"

HEADER_FMT = ">I"


# ════════════════════════════════════════════════════════════════════════
# local API (echo /health)
async def health_handler(request):
    return web.json_response({"status": "ok"})


# ════════════════════════════════════════════════════════════════════════
# mock relay
class MockRelay:
    def __init__(self):
        self.connector_ws = None
        self.relay_ws = None
        self.pending = {}  # request_id → future

    async def ws_handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connector_ws = ws
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data["type"] == "pong":
                    pass
                elif data["type"] == "response_start":
                    rid = data["id"]
                    self.pending[rid]["resp_meta"] = data
                elif data["type"] == "error":
                    rid = data["id"]
                    fut = self.pending.get(rid)
                    if fut:
                        fut.set_result(("error", data))
            elif msg.type == aiohttp.WSMsgType.BINARY:
                raw = msg.data
                hlen = struct.unpack(HEADER_FMT, raw[:4])[0]
                header = json.loads(raw[4:4 + hlen])
                rid = header["id"]
                payload = raw[4 + hlen:]
                entry = self.pending.setdefault(rid, {})
                chunks = entry.setdefault("chunks", [])
                chunks.append(payload)
                if header.get("final"):
                    fut = entry.get("fut")
                    if fut:
                        fut.set_result(("ok", entry.get("resp_meta"), chunks))
        return ws

    async def send_request(self, method, path, query, body_b64=None):
        req_id = hex(random.getrandbits(128))[2:]
        request_frame = {
            "type": "request",
            "id": req_id,
            "method": method,
            "path": path,
            "query": query,
            "headers": {"content-type": ""},
            "body_b64": body_b64,
        }
        fut = asyncio.get_event_loop().create_future()
        self.pending[req_id] = {"fut": fut}
        await self.connector_ws.send_json(request_frame)
        return await fut

    async def wait_conn(self, timeout=10):
        for _ in range(timeout * 10):
            if self.connector_ws and not self.connector_ws.closed:
                return True
            await asyncio.sleep(0.1)
        return False


async def run_mock_relay():
    app = web.Application()
    relay = MockRelay()
    app.router.add_get("/agent/ws", relay.ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, RELAY_PORT)
    await site.start()
    return relay, runner


@pytest.mark.asyncio
async def test_relay_health():
    """Full round-trip: mock-relay → connector → local-api → back."""
    # Start local API
    api_app = web.Application()
    api_app.router.add_get("/health", health_handler)
    api_runner = web.AppRunner(api_app)
    await api_runner.setup()
    api_site = web.TCPSite(api_runner, HOST, API_PORT)
    await api_site.start()

    # Start mock relay
    relay, relay_runner = await run_mock_relay()

    # Start connector in background
    from fastwado.relay import RelayConnector

    connector = RelayConnector(
        relay_base=RELAY_BASE,
        client=CLIENT,
        token=TOKEN,
        upstream=UPSTREAM,
    )
    conn_task = asyncio.create_task(connector.run())

    # Wait for WS to be established
    await asyncio.sleep(0.5)

    # Send a request through the relay
    result = await relay.send_request("GET", "/health", "")
    status, meta, chunks = result

    assert status == "ok"
    assert meta["status"] == 200
    assert "application/json" in meta["headers"]["content-type"]

    body = b"".join(chunks)
    data = json.loads(body)
    assert data == {"status": "ok"}

    # Cleanup
    conn_task.cancel()
    await api_runner.cleanup()
    await relay_runner.cleanup()


@pytest.mark.asyncio
async def test_relay_large_response():
    """Verify chunking of responses larger than 256 KiB."""
    HOST2 = "127.0.0.1"
    RP2 = RELAY_PORT + 100
    AP2 = API_PORT + 100

    # Local API that returns a large JSON payload
    async def large_handler(request):
        data = {"data": "x" * 300_000}  # ~300 KB
        return web.json_response(data)

    api_app = web.Application()
    api_app.router.add_get("/large", large_handler)
    api_runner = web.AppRunner(api_app)
    await api_runner.setup()
    await web.TCPSite(api_runner, HOST2, AP2).start()

    # Mock relay
    ra = web.Application()
    relay = MockRelay()
    ra.router.add_get("/agent/ws", relay.ws_handler)
    rr = web.AppRunner(ra)
    await rr.setup()
    await web.TCPSite(rr, HOST2, RP2).start()

    # Start connector
    from fastwado.relay import RelayConnector
    connector = RelayConnector(
        relay_base=f"http://{HOST2}:{RP2}",
        client="test-large",
        token="t",
        upstream=f"http://{HOST2}:{AP2}",
    )
    conn_task = asyncio.create_task(connector.run())
    await asyncio.sleep(0.5)

    # Send request
    result = await relay.send_request("GET", "/large", "")
    status, meta, chunks = result
    assert status == "ok"
    assert meta["status"] == 200

    body = b"".join(chunks)
    assert len(body) > 250_000, f"Body too short: {len(body)} bytes"
    data = json.loads(body)
    assert len(data["data"]) == 300_000

    conn_task.cancel()
    await api_runner.cleanup()
    await rr.cleanup()


@pytest.mark.asyncio
async def test_relay_reconnect_after_kill():
    """Simulate relay death without graceful close — connector must
    detect and reconnect."""
    import logging
    import fastwado.relay as relay_mod

    HOST2 = "127.0.0.1"
    RP2 = RELAY_PORT + 200
    AP2 = API_PORT + 200

    # Short timeout for test
    relay_mod.RECV_TIMEOUT = 2

    async def _start_relay():
        app = web.Application()
        rly = MockRelay()
        app.router.add_get("/agent/ws", rly.ws_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, HOST2, RP2).start()
        return rly, runner

    async def health_handler(request):
        return web.json_response({"status": "ok"})

    api_app = web.Application()
    api_app.router.add_get("/health", health_handler)
    api_runner = web.AppRunner(api_app)
    await api_runner.setup()
    await web.TCPSite(api_runner, HOST2, AP2).start()

    # Start first relay
    relay, relay_runner = await _start_relay()

    from fastwado.relay import RelayConnector
    connector = RelayConnector(
        relay_base=f"http://{HOST2}:{RP2}",
        client="test-kill",
        token="t",
        upstream=f"http://{HOST2}:{AP2}",
    )
    conn_task = asyncio.create_task(connector.run())
    await relay.wait_conn(5)
    assert relay.connector_ws, "never connected to first relay"

    # First request works
    r = await relay.send_request("GET", "/health", "")
    status, meta, chunks = r
    assert status == "ok"
    assert meta["status"] == 200

    # Kill the relay without graceful close
    await relay_runner.cleanup()
    # wait for connector to detect dead connection and reconnect
    await asyncio.sleep(3)

    # Start second relay
    relay2, relay2_runner = await _start_relay()

    # Wait for reconnect
    ok = await relay2.wait_conn(10)
    assert ok, "connector did not reconnect after relay restart"

    # Second request works through the new relay
    status2, meta2, chunks2 = await relay2.send_request("GET", "/health", "")
    assert status2 == "ok"
    assert meta2["status"] == 200

    # Cleanup
    conn_task.cancel()
    relay_mod.RECV_TIMEOUT = relay_mod.PING_TIMEOUT + 10  # restore
    await api_runner.cleanup()
    await relay2_runner.cleanup()
