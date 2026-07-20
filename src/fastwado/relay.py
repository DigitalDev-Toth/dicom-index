"""Relay connector: WebSocket client that bridges Mirror relay ↔ local API.

Connects to the relay via WebSocket (or long-polling fallback), receives
proxied requests, executes them against the local API, and returns responses
with binary chunking per the Relay Mirror protocol.
"""

import asyncio
import base64
import json
import logging
import os
import random
import struct
import sys
from datetime import datetime, timezone

log = logging.getLogger("relay")

HEADER_FMT = ">I"  # uint32 BE
CHUNK_SIZE = 256 * 1024  # 256 KiB
PING_TIMEOUT = 60
PING_INTERVAL = 20


class RelayConnector:
    def __init__(self, relay_base, client, token, upstream):
        self.relay_base = relay_base.rstrip("/")
        self.client = client
        self.token = token
        self.upstream = upstream.rstrip("/")

        # Transport auto-detection: try WS first, fall back to LP
        self.ws_url = f"{self.relay_base}/agent/ws?client={client}"
        self.poll_url = f"{self.relay_base}/agent/poll?client={client}"
        self.resp_url_tpl = f"{self.relay_base}/agent/response/{{req_id}}?client={client}"

        self._ws = None
        self._session = None
        self._last_pong = datetime.now(timezone.utc)
        self._inflight = {}  # id → asyncio.Task

    # ------------------------------------------------------------------
    async def run(self):
        backoff = 1.0
        while True:
            try:
                await self._run_ws()
                backoff = 1.0
            except Exception:
                log.exception("WS connection lost")
            await asyncio.sleep(backoff + random.uniform(0, 1) * 0.5)
            backoff = min(backoff * 2, 30)

    async def _run_ws(self):
        import aiohttp

        headers = {"Authorization": f"Bearer {self.token}"}
        timeout = aiohttp.ClientTimeout(total=None, sock_read=PING_TIMEOUT + 10)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            self._session = session
            async with session.ws_connect(self.ws_url, headers=headers) as ws:
                self._ws = ws
                self._last_pong = datetime.now(timezone.utc)
                log.info("Connected to relay (WS) for client=%s", self.client)

                # Kick off health-check / pong watcher
                watcher = asyncio.create_task(self._watchdog())

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._on_text(msg.data)
                    elif msg.type == aiohttp.WSMsgType.CLOSE:
                        code = msg.data
                        log.info("WS close code=%s", code)
                        if code == 4409:
                            log.warning("Replaced by newer connection — exiting")
                            sys.exit(0)
                        break
                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                        break

                watcher.cancel()

    async def _watchdog(self):
        while True:
            await asyncio.sleep(PING_INTERVAL)
            ago = (datetime.now(timezone.utc) - self._last_pong).total_seconds()
            if ago > PING_TIMEOUT:
                log.error("No pong for %.0fs — closing", ago)
                if self._ws:
                    await self._ws.close()
                return

    # ------------------------------------------------------------------
    async def _on_text(self, raw):
        data = json.loads(raw)
        typ = data.get("type")
        if typ == "ping":
            self._last_pong = datetime.now(timezone.utc)
            if self._ws:
                await self._ws.send_json({"type": "pong"})
        elif typ == "request":
            req_id = data["id"]
            task = asyncio.create_task(self._handle_request(data))
            self._inflight[req_id] = task
            task.add_done_callback(lambda _: self._inflight.pop(req_id, None))
        else:
            log.debug("unhandled frame type=%s", typ)

    # ------------------------------------------------------------------
    async def _handle_request(self, req):
        req_id = req["id"]
        method = req["method"]
        path = req["path"]
        query = req.get("query", "")
        body_b64 = req.get("body_b64")

        url = f"{self.upstream}{path}"
        if query:
            url += f"?{query}"

        log.info("[%s] %s %s", req_id, method, url)

        body = base64.b64decode(body_b64) if body_b64 else None

        try:
            resp = await self._session.request(
                method, url, data=body,
                headers={"content-type": req.get("headers", {}).get("content-type", "") or "application/json"}
            )
            status = resp.status
            ct = resp.headers.get("Content-Type", "application/octet-stream")
            cl = resp.headers.get("Content-Length", "")

            # Read entire body before chunking — aiohttp's StreamReader
            # returns partial reads, so we can't rely on len(chunk) < N
            # to detect EOF.
            full_body = await resp.read()

            if self._ws:
                await self._ws.send_json({
                    "type": "response_start",
                    "id": req_id,
                    "status": status,
                    "headers": {"content-type": ct, "content-length": str(len(full_body))},
                })

            # Chunked binary frames
            if not full_body:
                # Empty body — one final empty chunk
                if self._ws:
                    header = json.dumps({"id": req_id, "seq": 0, "final": True})
                    hb = header.encode()
                    frame = struct.pack(HEADER_FMT, len(hb)) + hb + b""
                    await self._ws.send_bytes(frame, compress=False)
            else:
                offset = 0
                seq = 0
                while offset < len(full_body):
                    chunk = full_body[offset:offset + CHUNK_SIZE]
                    final = (offset + CHUNK_SIZE) >= len(full_body)
                    if self._ws:
                        header = json.dumps({"id": req_id, "seq": seq, "final": final})
                        hb = header.encode()
                        frame = struct.pack(HEADER_FMT, len(hb)) + hb + chunk
                        await self._ws.send_bytes(frame, compress=False)
                    offset += CHUNK_SIZE
                    seq += 1

            log.info("[%s] → %s", req_id, status)
        except Exception as exc:
            log.exception("[%s] upstream error", req_id)
            if self._ws:
                await self._ws.send_json({
                    "type": "error", "id": req_id,
                    "status": 502, "message": str(exc),
                })
