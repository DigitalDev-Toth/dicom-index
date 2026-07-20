import os
import shutil
import tempfile

import pytest

try:
    import psycopg2

    HAS_PG = True
except ImportError:
    HAS_PG = False

try:
    import gdcm

    HAS_GDCM = True
except ImportError:
    HAS_GDCM = False


TEST_CLIENT = "pytest"
TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://dicom:dicom@localhost:5433/dicom"
)

pytestmark = pytest.mark.skipif(
    not (HAS_PG and HAS_GDCM), reason="Requires PostgreSQL + GDCM"
)


@pytest.fixture(scope="module")
def db():
    from fastwado.db import connect, ensure_client, init_db

    conn = connect(TEST_DB_URL)
    try:
        init_db(conn)
        ensure_client(conn, TEST_CLIENT)
        yield conn
    finally:
        conn.close()


@pytest.fixture
def temp_dicom_dir(test_dicom_path):
    d = tempfile.mkdtemp()
    src = test_dicom_path
    shutil.copy(src, os.path.join(d, "test1.dcm"))
    yield d
    shutil.rmtree(d)


class TestDatabase:
    def test_init_and_status(self, db):
        from fastwado.db import db_status

        info = db_status(db)
        assert info["database"] == "dicom"
        assert info["user"] == "dicom"

    def test_scan_and_lookup(self, db, temp_dicom_dir):
        from fastwado.db import get_study_full, refresh_counters
        from fastwado.scanner import scan

        stats, non_dicom = scan(db, TEST_CLIENT, temp_dicom_dir, batch_size=10)
        assert stats["instances_new"] == 1
        assert stats["non_dicom"] == 0

        refresh_counters(db, TEST_CLIENT)

        result = get_study_full(
            db, TEST_CLIENT, "1.2.840.113619.2.5.1762389987.2836.9999"
        )
        assert result is not None
        assert result["study"]["study_id"] == "STUDY001"
        assert result["study"]["modalities"] == ["CT"]
        assert len(result["series"]) == 1
        assert result["series"][0]["series_number"] == 1
        assert len(result["series"][0]["instances"]) == 1

    def test_incremental_skip(self, db, temp_dicom_dir):
        from fastwado.scanner import scan

        # First scan
        stats1, _ = scan(db, TEST_CLIENT, temp_dicom_dir, batch_size=10)
        # Second scan should skip all
        stats2, _ = scan(db, TEST_CLIENT, temp_dicom_dir, batch_size=10)
        assert stats2["skipped"] >= 1
        assert stats2["instances_new"] == 0

    def test_get_stats(self, db):
        from fastwado.db import get_stats

        s = get_stats(db, TEST_CLIENT)
        assert s["total_studies"] >= 1
        assert s["total_series"] >= 1
        assert s["total_instances"] >= 1
        assert s["total_size_bytes"] > 0
        assert ("CT", 1) in s["modalities"]

    def test_report_generation(self, db, temp_dicom_dir):
        from fastwado.db import get_stats
        from fastwado.reporter import generate_report

        s = get_stats(db, TEST_CLIENT)
        scan_stub = {"scan_duration_s": 0.1}
        report = generate_report(s, scan_stub, 0, TEST_CLIENT, temp_dicom_dir)
        assert "# DICOM Scan Report" in report
        assert "CT" in report
        assert "Patient^Test" in report
