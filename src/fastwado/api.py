"""FastAPI REST server for DICOM study lookups and WADO image delivery."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request, Body
from fastapi.responses import JSONResponse, Response

from fastwado.config import DATABASE_URL
from fastwado.db import (
    connect,
    get_instance_info,
    get_series_instance_paths,
    get_study_full,
    list_studies,
)

log = logging.getLogger(__name__)

# Set via environment or serve CLI
_relay_connector = None


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _relay_connector
    relay_url = os.environ.get("RELAY_URL", "")
    token = os.environ.get("RELAY_TOKEN", "")
    client = os.environ.get("RELAY_CLIENT", "")
    upstream = os.environ.get("RELAY_UPSTREAM", "")
    if relay_url and token and client:
        try:
            from fastwado.relay import RelayConnector
        except ImportError:
            log.error("aiohttp required for relay; pip install aiohttp")
        else:
            _relay_connector = RelayConnector(
                relay_base=relay_url,
                client=client,
                token=token,
                upstream=upstream or "http://localhost:8001",
            )
            task = asyncio.create_task(_relay_connector.run())
            log.info("Relay connector started for client=%s → %s", client, relay_url)
    yield
    if _relay_connector and task:
        task.cancel()
        log.info("Relay connector stopped")


app = FastAPI(
    title="DICOM Index API",
    version="0.1.0",
    docs_url="/docs",
    lifespan=_lifespan,
)


def _db_url(request: Request, qparam: str = "") -> str:
    if qparam:
        return qparam
    return os.environ.get("DATABASE_URL", DATABASE_URL)


def _wado_base(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-Host") or request.headers.get("Host")
    if not forwarded:
        return ""
    scheme = request.headers.get("X-Forwarded-Proto", "http")
    return f"{scheme}://{forwarded}"


def _wado_url(request: Request, study_uid, series_uid, object_uid) -> str:
    base = _wado_base(request)
    if not base:
        return ""
    return (
        f"{base}/wado?requestType=WADO"
        f"&studyUID={study_uid}"
        f"&seriesUID={series_uid}"
        f"&objectUID={object_uid}"
    )


def _inject_wado_urls(result, request: Request):
    for s in result.get("series", []):
        for i in s.get("instances", []):
            i["wado_url"] = _wado_url(
                request,
                result["study"]["study_iuid"],
                s["series_iuid"],
                i["sop_iuid"],
            )


def _inject_metadata(result, conn, client):
    from fastwado.metadata_reader import read_series_metadata

    for s in result.get("series", []):
        paths = get_series_instance_paths(conn, client, s["series_iuid"], limit=2)
        # filter: only keep paths that exist on disk
        valid = [p for p in paths if os.path.isfile(p)]
        s["metadata"] = read_series_metadata(valid)


@app.get("/health")
def health(request: Request):
    return {"status": "ok"}


@app.api_route("/debug", methods=["GET", "POST", "PUT", "DELETE"])
async def debug_echo(request: Request):
    """Echo back request details to debug relay URL construction."""
    return {
        "method": request.method,
        "path": request.url.path,
        "query": str(request.url.query),
        "full_url": str(request.url),
    }


MAX_STUDIES_LIMIT = 2000


@app.get("/studies")
def get_studies(
    request: Request,
    client: str = Query(..., min_length=1, description="Client identifier"),
    date_from: str | None = Query(None, description="YYYY-MM-DD"),
    date_to: str | None = Query(None, description="YYYY-MM-DD"),
    modality: str | None = Query(None, description="Comma-separated, e.g. CT,MR"),
    patient_name: str | None = Query(None),
    patient_id: str | None = Query(None),
    accession_number: str | None = Query(None),
    limit: int | None = Query(None, description="Max results (capped at 2000)"),
    db_url: str = Query(default="", description="Override DATABASE_URL"),
):
    """Worklist: return filtered study list for a client."""
    dsn = _db_url(request, db_url)
    conn = connect(dsn)
    try:
        rows, truncated = list_studies(
            conn,
            client,
            date_from=date_from,
            date_to=date_to,
            modality=modality,
            patient_name=patient_name,
            patient_id=patient_id,
            accession_number=accession_number,
            limit=min(limit or MAX_STUDIES_LIMIT, MAX_STUDIES_LIMIT),
        )
    finally:
        conn.close()

    return JSONResponse(content={"studies": rows, "truncated": truncated})


@app.get("/study/{study_iuid:path}")
def get_study(
    study_iuid: str,
    request: Request,
    client: str = Query(..., min_length=1, description="Client identifier"),
    db_url: str = Query(
        default="",
        description="Override DATABASE_URL",
    ),
):
    """Return study metadata with all nested series and instances."""
    dsn = _db_url(request, db_url)
    conn = connect(dsn)
    try:
        result = get_study_full(conn, client, study_iuid)
        if result is None:
            raise HTTPException(status_code=404, detail=f"Study not found: {study_iuid}")
        _inject_metadata(result, conn, client)
    finally:
        conn.close()

    _inject_wado_urls(result, request)
    return JSONResponse(content=result)


@app.get("/wado")
def wado(
    request: Request,
    requestType: str = Query(...),
    studyUID: str = Query(...),
    seriesUID: str = Query(...),
    objectUID: str = Query(...),
    client: str = Query(..., min_length=1, description="Client identifier"),
    rows: int | None = Query(None, description="Thumbnail height in pixels"),
    columns: int | None = Query(None, description="Thumbnail width in pixels"),
    db_url: str = Query(default="", description="Override DATABASE_URL"),
):
    """WADO endpoint: return rendered DICOM image as JPEG.
    Optional *rows* and/or *columns* produce a thumbnail.
    If only one is given the aspect ratio is preserved.
    """
    if requestType != "WADO":
        raise HTTPException(status_code=400, detail="Only requestType=WADO is supported")

    from fastwado.wado_handler import NotAnImageError, read_and_render

    dsn = _db_url(request, db_url)
    conn = connect(dsn)
    try:
        info = get_instance_info(conn, client, objectUID)
    finally:
        conn.close()

    if info is None:
        raise HTTPException(status_code=404, detail=f"Instance not found: {objectUID}")

    filepath = info["file_path"]
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="DICOM file not found on disk")

    try:
        jpeg_bytes, _, _ = read_and_render(filepath, rows=rows, columns=columns)
    except NotAnImageError:
        raise HTTPException(status_code=400, detail="Not a renderable DICOM image")
    except Exception:
        log.exception("WADO render failed for %s", filepath)
        raise HTTPException(status_code=500, detail="Image rendering failed")

    return Response(content=jpeg_bytes, media_type="image/jpeg")


@app.post("/intensity")
def intensity(
    request: Request,
    client: str = Query(..., min_length=1, description="Client identifier"),
    db_url: str = Query(default="", description="Override DATABASE_URL"),
    body: dict = Body(...),
):
    """Calculate intensity / Hounsfield statistics over pixel ranges.

    Body (JSON):
      study_uid, series_uid, sop_uid : str
      frame : int (1-based, default 1)
      rows_array : list[[int, int]] — inclusive [start, end] ranges
    """
    from fastwado.intensity_handler import compute_intensity, read_intensity_data

    study_uid = body.get("study_uid", "")
    series_uid = body.get("series_uid", "")
    sop_uid = body.get("sop_uid", "")
    frame = body.get("frame", 1)
    rows_array = body.get("rows_array")

    if not sop_uid:
        raise HTTPException(status_code=400, detail="Missing sop_uid")
    if not isinstance(rows_array, list) or len(rows_array) == 0:
        raise HTTPException(status_code=400, detail="rows_array must be a non-empty list")
    if not isinstance(frame, int) or frame < 1:
        raise HTTPException(status_code=400, detail="frame must be a positive integer")

    dsn = _db_url(request, db_url)
    conn = connect(dsn)
    try:
        info = get_instance_info(conn, client, sop_uid)
    finally:
        conn.close()

    if info is None:
        raise HTTPException(status_code=404, detail=f"Instance not found: {sop_uid}")

    filepath = info["file_path"]
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="DICOM file not found on disk")

    try:
        px_info = read_intensity_data(filepath)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception:
        log.exception("Failed to read pixel data for %s", filepath)
        raise HTTPException(status_code=500, detail="Pixel data not available")

    try:
        stats = compute_intensity(px_info, frame, rows_array)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        log.exception("Intensity computation failed for %s", filepath)
        raise HTTPException(status_code=500, detail="Intensity computation failed")

    result = {
        "rows": px_info["rows"],
        "columns": px_info["columns"],
        "modality": px_info["modality"],
        "samples_per_pixel": px_info["samples_per_pixel"],
        "number_of_frames": px_info["number_of_frames"],
    }
    result.update(stats)
    return JSONResponse(content=result)
