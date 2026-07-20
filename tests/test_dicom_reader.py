import os
import sys
import pytest

# Must be importable even if GDCM is missing; the tests will skip gracefully.
try:
    from fastwado.dicom_reader import read_tags
    HAS_GDCM = True
except ImportError:
    HAS_GDCM = False

try:
    import psycopg2
    HAS_PG = True
except ImportError:
    HAS_PG = False


@pytest.mark.skipif(not HAS_GDCM, reason="python-gdcm not installed")
class TestDicomReader:
    def test_read_test_file(self, test_dicom_path):
        tags = read_tags(test_dicom_path)
        assert tags is not None
        assert tags["StudyInstanceUID"] == "1.2.840.113619.2.5.1762389987.2836.9999"
        assert tags["SeriesInstanceUID"] == "1.2.840.113619.2.5.1762389987.2836.9999.1"
        assert tags["SOPInstanceUID"] == "1.2.840.113619.2.5.1762389987.2836.9999.1.1"
        assert tags["PatientName"] == "Patient^Test"
        assert tags["Modality"] == "CT"
        assert tags["StudyDate"] == "20240101"
        assert tags["StudyDescription"] == "Test Study Desc"

    def test_non_dicom_file(self, temp_dir):
        p = os.path.join(temp_dir, "not_dicom.txt")
        with open(p, "w") as f:
            f.write("hello world")
        assert read_tags(p) is None
