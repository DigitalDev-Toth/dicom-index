import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

from fastwado.db import (
    batch_insert_instances,
    ensure_client,
    upsert_series,
    upsert_study,
)
from fastwado.dicom_reader import read_tags

log = logging.getLogger(__name__)


def _process_one(args):
    fpath, fsize, fmtime = args
    try:
        tags = read_tags(fpath)
    except Exception:
        return {"status": "error", "path": fpath}
    if tags is None:
        return {"status": "skip", "path": fpath}
    siuid = tags.get("StudyInstanceUID")
    seuid = tags.get("SeriesInstanceUID")
    souid = tags.get("SOPInstanceUID")
    if not (siuid and seuid and souid):
        return {"status": "skip", "path": fpath}
    return {
        "status": "ok",
        "sop_iuid": souid,
        "series_iuid": seuid,
        "study_iuid": siuid,
        "instance_number": tags.get("InstanceNumber"),
        "sop_class_uid": tags.get("SOPClassUID"),
        "file_path": fpath,
        "file_size": fsize,
        "file_mtime": fmtime,
        "tags": tags,
        "source_dir": os.path.dirname(fpath),
    }


def scan(conn, client, path, batch_size=500, workers=None, progress=None):
    if workers is None:
        workers = min(16, (os.cpu_count() or 4) * 2)

    ensure_client(conn, client)

    non_dicom = []
    stats = {
        "studies_new": 0,
        "series_new": 0,
        "instances_new": 0,
        "skipped": 0,
        "non_dicom": 0,
        "total_files": 0,
        "errors": 0,
        "scan_duration_s": 0,
    }

    t0 = time.time()

    # ── Phase 1: collect file paths (fast, no stat) ──────────────────────
    if progress:
        sys.stderr.write("Counting files...\n")
        sys.stderr.flush()

    files_to_process = []
    for root, dirs, files in os.walk(path):
        for fname in files:
            fpath = os.path.join(root, fname)
            files_to_process.append(fpath)

    stats["total_files"] = len(files_to_process)
    total = stats["total_files"] or 1
    pbar = None
    if progress:
        pbar = progress(total)

    # ── Phase 2: batch-stamp → parallel read → serial insert ─────────────
    db_lock = Lock()
    studies_done = set()
    series_done = set()
    instance_batch = []

    def _handle_result(result):
        nonlocal instance_batch
        if result["status"] != "ok":
            non_dicom.append(result["path"])
            stats["non_dicom"] += 1
            return

        with db_lock:
            cur = conn.cursor()
            siuid = result["study_iuid"]
            seuid = result["series_iuid"]

            if siuid not in studies_done:
                upsert_study(cur, client, result["tags"], result["source_dir"])
                studies_done.add(siuid)
                stats["studies_new"] += 1
                conn.commit()

            if seuid not in series_done:
                upsert_series(cur, client, siuid, result["tags"], result["source_dir"])
                series_done.add(seuid)
                stats["series_new"] += 1
                conn.commit()

        instance_batch.append({
            "sop_iuid": result["sop_iuid"],
            "series_iuid": result["series_iuid"],
            "study_iuid": result["study_iuid"],
            "instance_number": result["instance_number"],
            "sop_class_uid": result["sop_class_uid"],
            "file_path": result["file_path"],
            "file_size": result["file_size"],
            "file_mtime": result["file_mtime"],
        })
        stats["instances_new"] += 1

        if len(instance_batch) >= batch_size:
            _flush(conn, client, instance_batch, db_lock)
            instance_batch.clear()

    CHECK_BATCH = 2000  # how many files to stamp + check in one round
    SUBMIT_BATCH = workers * 50

    idx = 0
    total_n = len(files_to_process)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        while idx < total_n:
            check_end = min(idx + CHECK_BATCH, total_n)
            check_paths = files_to_process[idx:check_end]
            idx = check_end

            # Stamp the whole check batch
            stamped = _stamp_batch(check_paths)

            # Filter out known files (they contribute to skipped count)
            known = _filter_known(conn, client, stamped)
            stats["skipped"] += len(known)

            # Only new/changed files go through GDCM
            new_files = [f for f in stamped if f not in known]

            # Submit in smaller sub-batches to keep memory low
            j = 0
            while j < len(new_files):
                sub_end = min(j + SUBMIT_BATCH, len(new_files))
                sub_batch = new_files[j:sub_end]
                j = sub_end

                futures = {}
                for fp, fs, fm in sub_batch:
                    fut = executor.submit(_process_one, (fp, fs, fm))
                    futures[fut] = fp

                for future in as_completed(futures):
                    result = future.result()
                    _handle_result(result)
                    if pbar:
                        pbar.update(1)

            # Update pbar for known files too
            if pbar:
                for _ in known:
                    pbar.update(1)

    if instance_batch:
        _flush(conn, client, instance_batch, db_lock)

    if pbar:
        pbar.close()

    stats["scan_duration_s"] = round(time.time() - t0, 2)
    return stats, non_dicom


def _stamp_batch(paths):
    """stat every path, return [(path, size, mtime), ...] for valid files."""
    result = []
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        result.append((p, st.st_size, st.st_mtime))
    return result


def _filter_known(conn, client, stamped):
    """Given a list of (path, size, mtime), return the subset that
    already exist in the DB with the same size and mtime."""
    if not stamped:
        return set()

    cur = conn.cursor()
    # Build VALUES clause: (file_path, file_size, file_mtime)
    rows_sql = ",".join(
        cur.mogrify("(%s,%s,%s)", (f[0], f[1], f[2])).decode() for f in stamped
    )
    cur.execute(
        f"""
        SELECT i.file_path, i.file_size, i.file_mtime
        FROM (VALUES {rows_sql}) AS v(path, size, mtime)
        INNER JOIN instances i
          ON i.client = %s
         AND i.file_path = v.path
         AND i.file_size = v.size
         AND i.file_mtime = v.mtime
        """,
        (client,),
    )
    return {(r[0], r[1], r[2]) for r in cur.fetchall()}


def _flush(conn, client, batch, lock=None):
    def _do():
        with conn:
            cur = conn.cursor()
            batch_insert_instances(cur, client, batch)

    if lock:
        with lock:
            _do()
    else:
        _do()
