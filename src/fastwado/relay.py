"""Relay connector: WebSocket client that bridges Mirror relay ↔ local API.

Connects to the relay via WebSocket, receives proxied requests, executes them
against the local API, and returns responses with binary chunking per the
Relay Mirror protocol.

Reconnect behaviour:
- Reconnects on ANY disconnection with exponential backoff 1 s → 30 s (+ jitter).
- Proactive reconnect every 50 min (Cloud Run cuts WS at 60 min).
- Loop never exits (except on 4409 "replaced").
"""

import asyncio
import base64
import json
import logging
import random
import struct
import sys
import time

import aiohttp

log = logging.getLogger("relay")

HEADER_FMT = ">I"
CHUNK_SIZE = 256 * 1024
PING_TIMEOUT = 60
PING_INTERVAL = 20
RECONNECT_AFTER = 50 * 60

# Overridable for testing
RECV_TIMEOUT = PING_TIMEOUT + 10  # 50 minutes — proactive refresh


class RelayConnector:
    def __init__(self, relay_base, client, token, upstream):
        self.relay_base = relay_base.rstrip("/")
        self.client = client
        self.token = token
        self.upstream = upstream.rstrip("/")

        base = self.relay_base
        if "/agent/ws" in base:
            self.ws_url = base if base.startswith(("ws://", "wss://")) else None
        else:
            if base.startswith(("https://", "wss://")):
                ws_proto = "wss"
            elif base.startswith(("http://", "ws://")):
                ws_proto = "ws"
            else:
                ws_proto = "wss"
            http_clean = base.split("://", 1)[-1] if "://" in base else base
            self.ws_url = f"{ws_proto}://{http_clean}/agent/ws?client={client}"

        self.poll_url = f"{self.relay_base}/agent/poll?client={client}"
        self.resp_url_tpl = f"{self.relay_base}/agent/response/{{req_id}}?client={client}"

        self._ws = None
        self._session = None
        self._inflight = {}

    # ------------------------------------------------------------------
    async def run(self):
        """Never-exit reconnect loop with exponential backoff."""
        backoff = 1.0
        while True:
            started_at = time.monotonic()
            try:
                await self._run_ws()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Relay loop error — will retry")
            finally:
                self._cancel_inflight()

            backoff = 1.0  # reset on clean disconnect
            if time.monotonic() - started_at < 5:
                # Short-lived connection (likely a refusal / quick error)
                await asyncio.sleep(backoff + random.uniform(0, 0.5))
                backoff = min(backoff * 2, 30)

    # ------------------------------------------------------------------
    async def _run_ws(self):
        headers = {"Authorization": f"Bearer {self.token}"}
        timeout = aiohttp.ClientTimeout(total=None, sock_read=PING_TIMEOUT + 10)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.ws_connect(
                    self.ws_url, headers=headers
                ) as ws:
                    await self._handle_ws(ws, session)
            except aiohttp.ClientError as exc:
                log.error("WS connect failed: %s", exc)
            except (ConnectionError, OSError) as exc:
                log.error("WS network error: %s", exc)

    async def _handle_ws(self, ws, session):
        self._ws = ws
        self._session = session
        log.info("Connected to relay (WS) — client=%s", self.client)

        reconnect_timer = asyncio.create_task(self._proactive_reconnect())

        try:
            while True:
                try:
                    msg = await asyncio.wait_for(
                        ws.receive(), timeout=RECV_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    log.error(
                        "No WS message for %ds — connection dead, will reconnect",
                        PING_TIMEOUT + 10,
                    )
                    return
                except asyncio.CancelledError:
                    raise

                try:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._on_text(msg.data)
                    elif msg.type == aiohttp.WSMsgType.CLOSE:
                        code = msg.data
                        log.info("WS close code=%s", code)
                        if code == 4409:
                            log.warning("Replaced by newer connection — exiting")
                            sys.exit(0)
                        return
                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                        log.warning("WS closed (%s)", msg.type)
                        return
                except Exception:
                    log.exception("Error processing WS message — continuing")
        finally:
            reconnect_timer.cancel()
            self._ws = None
            self._session = None

    async def _proactive_reconnect(self):
        await asyncio.sleep(RECONNECT_AFTER)
        log.info("Proactive reconnect after %ds", RECONNECT_AFTER)
        if self._ws and not self._ws.closed:
            await self._ws.close()

    # ------------------------------------------------------------------
    async def _on_text(self, raw):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Invalid JSON from relay: %.200s", raw)
            return

        typ = data.get("type")
        if typ == "ping":
            await self._ws.send_json({"type": "pong"})
        elif typ == "request":
            req_id = data.get("id", "?")
            task = asyncio.create_task(self._handle_request(data))
            self._inflight[req_id] = task
            task.add_done_callback(lambda t, rid=req_id: self._inflight.pop(rid, None))
        else:
            log.debug("unhandled frame type=%s", typ)

    def _cancel_inflight(self):
        for tid, task in list(self._inflight.items()):
            if not task.done():
                task.cancel()
        self._inflight.clear()

    # ------------------------------------------------------------------
    async def _handle_request(self, req):
        req_id = req.get("id", "?")
        method = req.get("method", "GET")
        path = req.get("path", "/")
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
                headers={"content-type": req.get("headers", {}).get("content-type", "") or "application/json"},
            )
            status = resp.status
            ct = resp.headers.get("Content-Type", "application/octet-stream")
            full_body = await resp.read()

            await self._send_response_start(req_id, status, ct, len(full_body))
            await self._send_chunks(req_id, full_body)

            log.info("[%s] → %s (%d bytes)", req_id, status, len(full_body))
        except Exception as exc:
            log.exception("[%s] upstream error", req_id)
            await self._send_error(req_id, 502, str(exc))

    async def _send_response_start(self, req_id, status, content_type, body_len):
        try:
            if self._ws:
                await self._ws.send_json({
                    "type": "response_start",
                    "id": req_id,
                    "status": status,
                    "headers": {
                        "content-type": content_type,
                        "content-length": str(body_len),
                    },
                })
        except Exception:
            log.exception("[%s] failed to send response_start", req_id)

    async def _send_chunks(self, req_id, full_body):
        if not full_body:
            await self._send_chunk(req_id, 0, b"", True)
            return
        offset = 0
        seq = 0
        while offset < len(full_body):
            chunk = full_body[offset:offset + CHUNK_SIZE]
            final = (offset + CHUNK_SIZE) >= len(full_body)
            await self._send_chunk(req_id, seq, chunk, final)
            offset += CHUNK_SIZE
            seq += 1

    async def _send_chunk(self, req_id, seq, chunk, final):
        try:
            if self._ws:
                header = json.dumps({"id": req_id, "seq": seq, "final": final})
                hb = header.encode()
                frame = struct.pack(HEADER_FMT, len(hb)) + hb + chunk
                await self._ws.send_bytes(frame, compress=False)
        except Exception:
            log.exception("[%s] failed to send chunk seq=%s", req_id, seq)

    async def _send_error(self, req_id, status, message):
        try:
            if self._ws:
                await self._ws.send_json({
                    "type": "error",
                    "id": req_id,
                    "status": status,
                    "message": message,
                })
        except Exception:
            log.exception("[%s] failed to send error frame", req_id)
