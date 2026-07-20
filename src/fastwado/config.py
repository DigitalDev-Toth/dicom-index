import os

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://dicom:dicom@localhost:5432/dicom",
)
BATCH_SIZE = int(os.environ.get("DICOM_BATCH_SIZE", "500"))
