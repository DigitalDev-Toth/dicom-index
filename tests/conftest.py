import os
import tempfile
import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURES_DIR = os.path.join(TESTS_DIR, "fixtures")


@pytest.fixture
def test_dicom_path():
    """Return path to test DICOM file, creating it if needed."""
    p = os.path.join(FIXTURES_DIR, "test1.dcm")
    if not os.path.exists(p):
        import subprocess
        subprocess.run(
            ["python", os.path.join(FIXTURES_DIR, "create_test_dicom.py")],
            cwd=FIXTURES_DIR,
            check=True,
        )
    return p


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d
