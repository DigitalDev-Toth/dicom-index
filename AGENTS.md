# AGENTS.md — Dicom Index

## Setup

```bash
docker compose up -d                      # start PostgreSQL 16
python3 -m venv .venv && source .venv/bin/activate
pip install -e .                          # editable install
pip install -e ".[dev]"                   # + pytest
pip install -e ".[api]"                   # + fastapi + uvicorn
dicom-index --client=test db init        # create schema
dicom-index --client=test db status      # verify connection
```

## Dev commands

```bash
pytest -v                                 # all tests
pytest -v tests/test_dicom_reader.py      # single file
pip install -e .                          # after dependency changes
pip install tqdm                          # optional: progress bar on scan
```

## Architecture

```
src/fastwado/
  cli.py          click entrypoint, all subcommands
  config.py       env vars: DATABASE_URL, DICOM_BATCH_SIZE
  db.py           postgres schema, upsert, queries
  dicom_reader.py gdcm wrapper → dict of DICOM tags
  scanner.py      os.walk → parallel read_tags → batch insert → counters
  reporter.py     markdown report + non_dicom.log
  api.py          FastAPI app: /health, /study/{iuid}?client=X,
                  /wado?requestType=WADO&studyUID=...&client=X
```

`dicom-index` always needs `--client` (or `DICOM_CLIENT` env). All tables
are partitioned on `client VARCHAR(32)`.  UIDs are globally unique per the
DICOM standard so the UNIQUE constraints are on `(client, uid)`.

## Parallel scan

`scan` uses `ThreadPoolExecutor` (default `min(16, cpu_count*2)` workers).
GDCM's C++ parser releases the GIL so threads yield real parallelism.
DB writes are serialized with a `Lock` — psycopg2 connections are not
thread-safe.  Override via `--workers/-w`.

## Database

PostgreSQL connection via `DATABASE_URL` (default:
`postgresql://dicom:dicom@localhost:5432/dicom` as set in docker-compose).

Three tables — `studies`, `series`, `instances` — with foreign keys and
composite indexes on `(client, study_iuid)`, `(client, series_iuid)`, etc.

Counters (`num_series`, `num_instances`, `modalities[]`) are refreshed
after each scan with `refresh_counters()`.

## Incremental scan

Before reading a file, the scanner loads all known `(file_path, file_size,
file_mtime)` tuples for the client + scan path prefix.  A file that matches
all three is skipped without opening it with GDCM.

## Non-DICOM files

Written to `non_dicom_<client>_<timestamp>.log`, not stored in the DB.

## Test DICOM fixture

Run `python tests/fixtures/create_test_dicom.py` to regenerate
`tests/fixtures/test1.dcm` if needed.  It uses pydicom (GDCM writer is
broken in the Python bindings — crashes with ReferenceCount error).
pydicom is only needed for fixture generation, not at runtime.

## WADO image rendering

`GET /wado?requestType=WADO&studyUID=...&seriesUID=...&objectUID=...&client=X`
returns `image/jpeg`.  Uses GDCM ImageReader → numpy → Pillow JPEG.
Windowing: RescaleSlope/Intercept → WindowCenter/Width → linear VOI LUT.

`GetBuffer()` returns `str` in GDCM 3.2.x — always encode with
`surrogateescape` before numpy.  `de.GetVL()` returns a `VL` object
(use `de.IsEmpty()` instead of `de.GetVL() > 0`).

## Single-binary build

Nuitka with `--standalone --onefile` and `--include-package=gdcm`.
GDCM's C++ lib must be present at build time.  psycopg2-binary bundles
libpq so no system pg_config is needed.
