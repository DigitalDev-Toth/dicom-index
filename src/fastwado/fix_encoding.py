"""Targeted fix for corrupted latin-1 characters stored as � in the DB.

Re-reads patient_name and other text fields from the DICOM header
(no pixel data) only for rows that contain U+FFFD, and UPDATEs them
using the corrected _sanitize().
"""

import logging
import os
import sys

log = logging.getLogger(__name__)

TEXT_FIELDS = [
    ("studies", "patient_name"),
    ("studies", "study_description"),
    ("studies", "referring_physician"),
    ("studies", "accession_number"),
    ("studies", "patient_id"),
    ("series", "series_description"),
]

TAG_MAP = {
    "patient_name": (0x0010, 0x0010),
    "study_description": (0x0008, 0x1030),
    "referring_physician": (0x0008, 0x0090),
    "accession_number": (0x0008, 0x0050),
    "patient_id": (0x0010, 0x0020),
    "series_description": (0x0008, 0x103E),
}

CORRUPT_MARKER = "\ufffd"  # �


def find_corrupted(conn, client):
    """Return a set of (table, column, id) for rows with the corruption marker."""
    cur = conn.cursor()
    corrupted = []
    for table, col in TEXT_FIELDS:
        cur.execute(
            f"SELECT id, {col} FROM {table} "
            "WHERE client = %s AND " + col + " LIKE %s",
            (client, "%" + CORRUPT_MARKER + "%"),
        )
        for row in cur.fetchall():
            corrupted.append((table, col, row[0]))
    return corrupted


def read_text_tag(filepath, group, elem):
    """Read a single DICOM tag, return sanitized string or None."""
    try:
        import gdcm
    except ImportError:
        return None

    reader = gdcm.Reader()
    reader.SetFileName(filepath)
    if not reader.Read():
        return None

    ds = reader.GetFile().GetDataSet()
    sf = gdcm.StringFilter()
    sf.SetFile(reader.GetFile())

    tag = gdcm.Tag(group, elem)
    if not ds.FindDataElement(tag):
        return None
    de = ds.GetDataElement(tag)
    if de.IsEmpty():
        return None
    try:
        val = sf.ToString(tag).strip()
        if val:
            return sanitize(val)
    except Exception:
        pass
    try:
        val = str(de.GetValue()).strip()
        if val:
            return sanitize(val)
    except Exception:
        pass
    return None


def sanitize(s):
    from fastwado.dicom_reader import _sanitize
    return _sanitize(s)


def get_sample_path(conn, client, table, row_id):
    """Return a DICOM file_path that can be read to fix *table* row *row_id*."""
    cur = conn.cursor()
    if table == "studies":
        cur.execute(
            "SELECT file_path FROM instances "
            "WHERE client = %s AND study_iuid = ("
            "  SELECT study_iuid FROM studies WHERE id = %s"
            ") LIMIT 1",
            (client, row_id),
        )
    elif table == "series":
        cur.execute(
            "SELECT file_path FROM instances "
            "WHERE client = %s AND series_iuid = ("
            "  SELECT series_iuid FROM series WHERE id = %s"
            ") LIMIT 1",
            (client, row_id),
        )
    else:
        return None
    row = cur.fetchone()
    return row[0] if row else None


def fix_row(conn, client, table, col, row_id, new_val):
    if new_val is None:
        return
    cur = conn.cursor()
    cur.execute(
        f"UPDATE {table} SET {col} = %s WHERE client = %s AND id = %s",
        (new_val, client, row_id),
    )
    conn.commit()


def run_fix(conn, client, progress=None):
    """Main entry point: find & fix all corrupted text in the DB."""
    corrupted = find_corrupted(conn, client)
    if not corrupted:
        log.info("No corrupted text found for client=%s", client)
        return 0

    log.info("Found %d corrupted entries for client=%s", len(corrupted), client)

    from collections import defaultdict

    # Group by (table, row_id) to read only one file per table row
    needs_fix = defaultdict(list)
    for table, col, row_id in corrupted:
        needs_fix[(table, row_id)].append(col)

    pbar = None
    if progress:
        pbar = progress(len(needs_fix))

    fixed = 0
    for (table, row_id), cols in needs_fix.items():
        path = get_sample_path(conn, client, table, row_id)
        if not path or not os.path.isfile(path):
            if pbar:
                pbar.update(1)
            continue

        for col in cols:
            tag = TAG_MAP.get(col)
            if not tag:
                continue
            new_val = read_text_tag(path, tag[0], tag[1])
            if new_val and CORRUPT_MARKER not in new_val:
                fix_row(conn, client, table, col, row_id, new_val)
                fixed += 1

        if pbar:
            pbar.update(1)

    if pbar:
        pbar.close()

    log.info("Fixed %d text values for client=%s", fixed, client)
    return fixed
