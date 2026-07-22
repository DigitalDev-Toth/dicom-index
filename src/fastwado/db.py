import logging
from datetime import date

import psycopg2
import psycopg2.extras

from fastwado.config import DATABASE_URL

log = logging.getLogger(__name__)

SCHEMA_SQL = """
BEGIN;

CREATE TABLE IF NOT EXISTS clients (
    name VARCHAR(32) PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS studies (
    id BIGSERIAL PRIMARY KEY,
    client VARCHAR(32) NOT NULL REFERENCES clients(name) ON DELETE CASCADE,
    study_iuid VARCHAR(64) NOT NULL,
    study_id VARCHAR(64),
    study_date DATE,
    study_description TEXT,
    accession_number VARCHAR(64),
    referring_physician TEXT,
    patient_name TEXT,
    patient_id VARCHAR(64),
    patient_birth_date DATE,
    patient_sex VARCHAR(4),
    modalities TEXT[],
    num_series INTEGER NOT NULL DEFAULT 0,
    num_instances INTEGER NOT NULL DEFAULT 0,
    source_path TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(client, study_iuid)
);

CREATE TABLE IF NOT EXISTS series (
    id BIGSERIAL PRIMARY KEY,
    client VARCHAR(32) NOT NULL,
    series_iuid VARCHAR(64) NOT NULL,
    study_id BIGINT NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
    study_iuid VARCHAR(64) NOT NULL,
    series_number INTEGER,
    series_description TEXT,
    modality VARCHAR(32),
    body_part_examined VARCHAR(64),
    num_instances INTEGER NOT NULL DEFAULT 0,
    source_path TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(client, series_iuid)
);

CREATE TABLE IF NOT EXISTS instances (
    id BIGSERIAL PRIMARY KEY,
    client VARCHAR(32) NOT NULL,
    sop_iuid VARCHAR(64) NOT NULL,
    series_id BIGINT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    series_iuid VARCHAR(64) NOT NULL,
    study_id BIGINT NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
    study_iuid VARCHAR(64) NOT NULL,
    instance_number INTEGER,
    sop_class_uid VARCHAR(64),
    file_path TEXT NOT NULL,
    file_size BIGINT NOT NULL DEFAULT 0,
    file_mtime DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(client, sop_iuid)
);

CREATE INDEX IF NOT EXISTS idx_series_client_study
    ON series(client, study_iuid);
CREATE INDEX IF NOT EXISTS idx_series_client_series
    ON series(client, series_iuid);
CREATE INDEX IF NOT EXISTS idx_instances_client_study
    ON instances(client, study_iuid);
CREATE INDEX IF NOT EXISTS idx_instances_client_series
    ON instances(client, series_iuid);
CREATE INDEX IF NOT EXISTS idx_instances_client_sop
    ON instances(client, sop_iuid);
CREATE INDEX IF NOT EXISTS idx_instances_file_path
    ON instances(client, file_path);

COMMIT;
"""

STUDY_COUNTERS_SQL = """
WITH inst_agg AS (
    SELECT study_id, COUNT(*) AS cnt
    FROM   instances
    WHERE  client = %(client)s
    GROUP  BY study_id
),
series_agg AS (
    SELECT study_id, COUNT(*) AS cnt
    FROM   series
    WHERE  client = %(client)s
    GROUP  BY study_id
)
UPDATE studies s SET
    num_series   = COALESCE(sa.cnt, 0),
    num_instances = COALESCE(ia.cnt, 0)
FROM       series_agg sa
LEFT JOIN inst_agg ia ON ia.study_id = sa.study_id
WHERE s.id = sa.study_id
  AND s.client = %(client)s;
"""

SERIES_COUNTERS_SQL = """
WITH inst_agg AS (
    SELECT series_id, COUNT(*) AS cnt
    FROM   instances
    WHERE  client = %(client)s
    GROUP  BY series_id
)
UPDATE series se SET
    num_instances = COALESCE(ia.cnt, 0)
FROM   inst_agg ia
WHERE  se.id = ia.series_id
  AND se.client = %(client)s;
"""

MODALITIES_SQL = """
UPDATE studies s SET
    modalities = se.mods
FROM (
    SELECT study_id,
           ARRAY_AGG(DISTINCT modality ORDER BY modality) AS mods
    FROM   series
    WHERE  client = %(client)s
      AND  modality IS NOT NULL
    GROUP  BY study_id
) se
WHERE s.id = se.study_id
  AND s.client = %(client)s;
"""


def _parse_dicom_date(val):
    if not val or len(val) != 8 or not val.isdigit():
        return None
    try:
        return date(int(val[:4]), int(val[4:6]), int(val[6:8]))
    except (ValueError, TypeError):
        return None


def _safe_int(val):
    if val is None:
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------


def connect(url=None):
    return psycopg2.connect(url or DATABASE_URL)


def init_db(conn):
    cur = conn.cursor()
    cur.execute(SCHEMA_SQL)
    conn.commit()
    log.info("Schema initialized.")


def ensure_client(conn, client):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO clients (name) VALUES (%s) ON CONFLICT DO NOTHING",
        (client,),
    )
    conn.commit()


def db_status(conn):
    cur = conn.cursor()
    cur.execute("SELECT version(), current_database(), current_user")
    row = cur.fetchone()
    return {"version": row[0], "database": row[1], "user": row[2]}


def load_known_files(conn, client, base_path):
    cur = conn.cursor()
    cur.execute(
        "SELECT file_path, file_size, file_mtime FROM instances "
        "WHERE client = %s AND file_path LIKE %s",
        (client, base_path.rstrip("/") + "/%"),
    )
    return {(r[0], r[1], r[2]) for r in cur.fetchall()}


# ---------------------------------------------------------------------------
# Upserts
# ---------------------------------------------------------------------------


def upsert_study(cur, client, tags, source_dir):
    siuid = tags.get("StudyInstanceUID")
    if not siuid:
        return
    cur.execute(
        """
        INSERT INTO studies (client, study_iuid, study_id, study_date,
            study_description, accession_number, referring_physician,
            patient_name, patient_id, patient_birth_date, patient_sex,
            source_path)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (client, study_iuid) DO UPDATE SET
            study_id = EXCLUDED.study_id,
            study_description = EXCLUDED.study_description,
            updated_at = NOW()
        """,
        (
            client,
            siuid,
            tags.get("StudyID") or None,
            _parse_dicom_date(tags.get("StudyDate")),
            tags.get("StudyDescription") or None,
            tags.get("AccessionNumber") or None,
            tags.get("ReferringPhysicianName") or None,
            tags.get("PatientName") or None,
            tags.get("PatientID") or None,
            _parse_dicom_date(tags.get("PatientBirthDate")),
            tags.get("PatientSex") or None,
            source_dir,
        ),
    )
    return cur.statusmessage


def upsert_series(cur, client, study_iuid, tags, source_dir):
    seuid = tags.get("SeriesInstanceUID")
    if not seuid:
        return
    cur.execute(
        """
        INSERT INTO series (client, series_iuid, study_id, study_iuid,
            series_number, series_description, modality, body_part_examined,
            source_path)
        SELECT %s, %s, s.id, %s, %s, %s, %s, %s, %s
        FROM studies s
        WHERE s.client = %s AND s.study_iuid = %s
        ON CONFLICT (client, series_iuid) DO UPDATE SET
            series_number = EXCLUDED.series_number,
            series_description = EXCLUDED.series_description,
            updated_at = NOW()
        """,
        (
            client,
            seuid,
            study_iuid,
            _safe_int(tags.get("SeriesNumber")),
            tags.get("SeriesDescription") or None,
            tags.get("Modality") or None,
            tags.get("BodyPartExamined") or None,
            source_dir,
            client,
            study_iuid,
        ),
    )
    return cur.statusmessage


def batch_insert_instances(cur, client, rows):
    """Batch-insert instance rows. *rows* is a list of dicts with keys:
    sop_iuid, series_iuid, study_iuid, instance_number, sop_class_uid,
    file_path, file_size, file_mtime.
    """
    if not rows:
        return

    study_iuids = {r["study_iuid"] for r in rows}
    series_iuids = {r["series_iuid"] for r in rows}

    cur.execute(
        "SELECT study_iuid, id FROM studies "
        "WHERE client = %s AND study_iuid = ANY(%s)",
        (client, list(study_iuids)),
    )
    study_map = {r[0]: r[1] for r in cur.fetchall()}

    cur.execute(
        "SELECT series_iuid, id FROM series "
        "WHERE client = %s AND series_iuid = ANY(%s)",
        (client, list(series_iuids)),
    )
    series_map = {r[0]: r[1] for r in cur.fetchall()}

    argslist = []
    seen = set()
    for r in rows:
        sid = study_map.get(r["study_iuid"])
        seid = series_map.get(r["series_iuid"])
        if sid is None or seid is None:
            log.warning("Missing FK for instance %s", r["sop_iuid"])
            continue
        sop = r["sop_iuid"]
        if sop in seen:
            log.warning("Duplicate SOPInstanceUID in batch: %s", sop)
            continue
        seen.add(sop)
        argslist.append((
            client,
            sop,
            seid,
            r["series_iuid"],
            sid,
            r["study_iuid"],
            _safe_int(r.get("instance_number")),
            r.get("sop_class_uid"),
            r["file_path"],
            int(r["file_size"]),
            float(r["file_mtime"]),
        ))

    if argslist:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO instances (client, sop_iuid, series_id, series_iuid,
                study_id, study_iuid, instance_number, sop_class_uid,
                file_path, file_size, file_mtime)
            VALUES %s
            ON CONFLICT (client, sop_iuid) DO UPDATE SET
                instance_number = EXCLUDED.instance_number,
                file_path = EXCLUDED.file_path,
                file_size = EXCLUDED.file_size,
                file_mtime = EXCLUDED.file_mtime
            """,
            argslist,
        )


def refresh_counters(conn, client):
    cur = conn.cursor()
    cur.execute(STUDY_COUNTERS_SQL, {"client": client})
    cur.execute(SERIES_COUNTERS_SQL, {"client": client})
    cur.execute(MODALITIES_SQL, {"client": client})
    conn.commit()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def get_study_full(conn, client, study_iuid):
    """Return nested dict {study: {...}, series: [{..., instances: [...]}]}."""
    cur = conn.cursor()

    cur.execute(
        """
        SELECT study_iuid, study_id, study_date, study_description,
               accession_number, patient_name, patient_id,
               patient_birth_date, patient_sex, modalities,
               num_series, num_instances, source_path
        FROM studies
        WHERE client = %s AND study_iuid = %s
        """,
        (client, study_iuid),
    )
    row = cur.fetchone()
    if not row:
        return None

    study = {
        "study_iuid": row[0],
        "study_id": row[1],
        "study_date": str(row[2]) if row[2] else None,
        "study_description": row[3],
        "accession_number": row[4],
        "patient_name": row[5],
        "patient_id": row[6],
        "patient_birth_date": str(row[7]) if row[7] else None,
        "patient_sex": row[8],
        "modalities": row[9] if row[9] else [],
        "num_series": row[10],
        "num_instances": row[11],
        "source_path": row[12],
    }

    cur.execute(
        """
        SELECT series_iuid, series_number, series_description,
               modality, body_part_examined, num_instances
        FROM series
        WHERE client = %s AND study_iuid = %s
        ORDER BY series_number NULLS LAST
        """,
        (client, study_iuid),
    )
    series_rows = cur.fetchall()

    series_list = []
    for sr in series_rows:
        seuid = sr[0]
        cur.execute(
            """
            SELECT sop_iuid, instance_number, sop_class_uid,
                   file_path, file_size
            FROM instances
            WHERE client = %s AND series_iuid = %s
            ORDER BY instance_number NULLS LAST
            """,
            (client, seuid),
        )
        series_list.append({
            "series_iuid": seuid,
            "series_number": sr[1],
            "series_description": sr[2],
            "modality": sr[3],
            "body_part_examined": sr[4],
            "num_instances": sr[5],
            "instances": [
                {
                    "sop_iuid": i[0],
                    "instance_number": i[1],
                    "sop_class_uid": i[2],
                    "file_path": i[3],
                    "file_size": i[4],
                }
                for i in cur.fetchall()
            ],
        })

    return {"study": study, "series": series_list}


def get_stats(conn, client):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM studies WHERE client = %s", (client,))
    total_studies = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM series WHERE client = %s", (client,))
    total_series = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM instances WHERE client = %s", (client,))
    total_instances = cur.fetchone()[0]

    cur.execute(
        "SELECT COALESCE(SUM(file_size), 0) FROM instances WHERE client = %s",
        (client,),
    )
    total_size = cur.fetchone()[0]

    cur.execute(
        """
        SELECT modality, COUNT(*)
        FROM series WHERE client = %s AND modality IS NOT NULL
        GROUP BY modality ORDER BY COUNT(*) DESC
        """,
        (client,),
    )
    modalities = [(r[0], r[1]) for r in cur.fetchall()]

    cur.execute(
        """
        SELECT study_iuid, study_id, study_date, study_description,
               patient_name, patient_id, modalities, num_series, num_instances
        FROM studies WHERE client = %s
        ORDER BY study_date DESC NULLS LAST LIMIT 100
        """,
        (client,),
    )
    studies = [
        {
            "study_iuid": r[0],
            "study_id": r[1],
            "study_date": str(r[2]) if r[2] else None,
            "study_description": r[3],
            "patient_name": r[4],
            "patient_id": r[5],
            "modalities": r[6] if r[6] else [],
            "num_series": r[7],
            "num_instances": r[8],
        }
        for r in cur.fetchall()
    ]

    return {
        "total_studies": total_studies,
        "total_series": total_series,
        "total_instances": total_instances,
        "total_size_bytes": total_size,
        "modalities": modalities,
        "studies": studies,
    }


def get_instance_info(conn, client, sop_iuid):
    """Return (file_path, study_iuid, series_iuid, sop_class_uid) or None."""
    cur = conn.cursor()
    cur.execute(
        """SELECT file_path, study_iuid, series_iuid, sop_class_uid
           FROM instances WHERE client = %s AND sop_iuid = %s""",
        (client, sop_iuid),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "file_path": row[0],
        "study_iuid": row[1],
        "series_iuid": row[2],
        "sop_class_uid": row[3],
    }


def get_series_instance_paths(conn, client, series_iuid, limit=2):
    """Return file paths for the first *limit* instances in a series,
    ordered by instance_number ASC."""
    cur = conn.cursor()
    cur.execute(
        """SELECT file_path FROM instances
           WHERE client = %s AND series_iuid = %s
           ORDER BY instance_number NULLS LAST
           LIMIT %s""",
        (client, series_iuid, limit),
    )
    return [r[0] for r in cur.fetchall()]


def list_studies(
    conn,
    client,
    date_from=None,
    date_to=None,
    modality=None,
    patient_name=None,
    patient_id=None,
    accession_number=None,
    limit=2000,
):
    """Return studies matching the given filters, ordered by study_date DESC.

    *modality* may be a comma-separated string (e.g. "CT,MR").
    Text fields use ILIKE partial matching.
    Never returns more than *limit* rows.
    Returns ``(rows, truncated)`` where *truncated* is True when the
    result count reached *limit*.
    """
    clauses = ["client = %(client)s"]
    params = {"client": client, "limit": limit + 1}

    if date_from:
        clauses.append("study_date >= %(date_from)s")
        params["date_from"] = date_from
    if date_to:
        clauses.append("study_date <= %(date_to)s")
        params["date_to"] = date_to
    if modality:
        mods = [m.strip() for m in modality.split(",") if m.strip()]
        if mods:
            clauses.append("modalities && %(mods)s")
            params["mods"] = mods
    if patient_name:
        clauses.append("patient_name ILIKE %(patient_name)s")
        params["patient_name"] = "%" + patient_name + "%"
    if patient_id:
        clauses.append("patient_id ILIKE %(patient_id)s")
        params["patient_id"] = "%" + patient_id + "%"
    if accession_number:
        clauses.append("accession_number ILIKE %(accession_number)s")
        params["accession_number"] = "%" + accession_number + "%"

    where = " AND ".join(clauses)

    cur = conn.cursor()
    cur.execute(
        f"""SELECT study_iuid, study_date, study_description,
                   accession_number, patient_name, patient_id,
                   patient_sex, patient_birth_date,
                   modalities, num_series, num_instances
            FROM studies
            WHERE {where}
            ORDER BY study_date DESC NULLS LAST
            LIMIT %(limit)s""",
        params,
    )
    rows = cur.fetchall()
    truncated = len(rows) > limit

    result = []
    for r in rows[:limit]:
        result.append({
            "study_iuid": r[0],
            "study_date": str(r[1]) if r[1] else None,
            "study_description": r[2],
            "accession_number": r[3],
            "patient_name": r[4],
            "patient_id": r[5],
            "patient_sex": r[6],
            "patient_birth_date": str(r[7]) if r[7] else None,
            "modalities": r[8] if r[8] else [],
            "num_series": r[9],
            "num_instances": r[10],
        })

    return result, truncated
