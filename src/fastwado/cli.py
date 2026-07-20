import asyncio
import json
import logging
import os
import sys

import click

from fastwado.config import DATABASE_URL, BATCH_SIZE
from fastwado.db import (
    connect,
    db_status,
    get_study_full,
    get_stats,
    init_db,
    refresh_counters,
)
from fastwado.reporter import (
    generate_report,
    write_non_dicom_log,
    write_report,
)
from fastwado.scanner import scan

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("dicom-index")


def _require_client(ctx):
    if not ctx.obj.get("client"):
        raise click.UsageError("Missing option '--client' / '-c'.", ctx)


def _make_progress(total):
    """Create a tqdm progress bar. Falls back to None if tqdm not installed."""
    try:
        from tqdm import tqdm
    except ImportError:
        return None
    return tqdm(total=total, unit="file", unit_scale=True,
                desc=" Scanning", bar_format="{desc}: {percentage:3.0f}% |{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]")


@click.group()
@click.option("--client", "-c", envvar="DICOM_CLIENT",
              help="Client identifier (e.g. nim, cdc, sanlorenzo)")
@click.option("--db-url", envvar="DATABASE_URL", default=DATABASE_URL,
              help="PostgreSQL connection URL")
@click.pass_context
def cli(ctx, client, db_url):
    ctx.ensure_object(dict)
    ctx.obj["client"] = client
    ctx.obj["db_url"] = db_url


# ── db subcommand group ──────────────────────────────────────────────────

@cli.group()
def db():
    """Database management commands."""
    pass


@db.command("init")
@click.pass_context
def db_init(ctx):
    """Create the database schema (tables + indexes)."""
    conn = connect(ctx.obj["db_url"])
    try:
        init_db(conn)
        click.echo("Schema created successfully.")
    finally:
        conn.close()


@db.command("status")
@click.pass_context
def db_status_cmd(ctx):
    """Test database connectivity."""
    conn = connect(ctx.obj["db_url"])
    try:
        info = db_status(conn)
        click.echo(f"Connected to {info['database']} as {info['user']}")
        click.echo(f"Version: {info['version']}")
    finally:
        conn.close()


# ── scan command ─────────────────────────────────────────────────────────

@cli.command("scan")
@click.argument("path", type=click.Path(exists=True, file_okay=False))
@click.option("--no-md", is_flag=True, help="Skip Markdown report generation")
@click.option("--workers", "-w", type=int, default=None,
              help="Number of parallel workers (default: cpu_count*2, max 16)")
@click.pass_context
def scan_cmd(ctx, path, no_md, workers):
    """Recursively scan a directory for DICOM files and index them."""
    _require_client(ctx)
    client = ctx.obj["client"]
    db_url = ctx.obj["db_url"]
    path = os.path.abspath(path)

    conn = connect(db_url)
    try:
        scan_stats, non_dicom = scan(conn, client, path, BATCH_SIZE,
                                     workers=workers, progress=_make_progress)
        refresh_counters(conn, client)
        stats = get_stats(conn, client)
    finally:
        conn.close()

    # Write non-DICOM log
    log_path = write_non_dicom_log(non_dicom, client)
    if log_path:
        click.echo(f"Non-DICOM log: {log_path} ({len(non_dicom)} files)")

    # Generate report
    if not no_md:
        report = generate_report(stats, scan_stats, len(non_dicom), client, path)
        rpt_path = write_report(report, client)
        click.echo(f"Report: {rpt_path}")

    # Print scan summary
    click.echo()
    click.echo(f"Total files scanned : {scan_stats['total_files']}")
    click.echo(f"  DICOM (indexed)  : {scan_stats['instances_new']}")
    click.echo(f"  Skipped (cached) : {scan_stats['skipped']}")
    click.echo(f"  Non-DICOM        : {scan_stats['non_dicom']}")
    click.echo(f"  Errors           : {scan_stats['errors']}")
    click.echo(f"Studies : {stats['total_studies']}")
    click.echo(f"Series  : {stats['total_series']}")
    click.echo(f"Duration: {scan_stats['scan_duration_s']} s")


# ── study command ────────────────────────────────────────────────────────

@cli.command("study")
@click.argument("iuid")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON")
@click.pass_context
def study_cmd(ctx, iuid, pretty):
    """Look up a study by StudyInstanceUID and print JSON."""
    _require_client(ctx)
    client = ctx.obj["client"]
    db_url = ctx.obj["db_url"]

    conn = connect(db_url)
    try:
        result = get_study_full(conn, client, iuid)
    finally:
        conn.close()

    if result is None:
        click.echo(f"Study not found: {iuid}", err=True)
        raise SystemExit(1)

    indent = 2 if pretty else None
    click.echo(json.dumps(result, indent=indent, ensure_ascii=False))


# ── report command ───────────────────────────────────────────────────────

@cli.command("report")
@click.argument("path", type=click.Path(exists=True, file_okay=False))
@click.pass_context
def report_cmd(ctx, path):
    """Generate a Markdown report from the existing database."""
    _require_client(ctx)
    client = ctx.obj["client"]
    db_url = ctx.obj["db_url"]
    path = os.path.abspath(path)

    conn = connect(db_url)
    try:
        stats = get_stats(conn, client)
    finally:
        conn.close()

    scan_stats = {"scan_duration_s": 0}
    report = generate_report(stats, scan_stats, 0, client, path)
    rpt_path = write_report(report, client)
    click.echo(f"Report written: {rpt_path}")


# ── serve command ───────────────────────────────────────────────────────

@cli.command("serve")
@click.option("--host", "-h", default="0.0.0.0", help="Bind address")
@click.option("--port", "-p", type=int, default=8001, help="Bind port")
@click.option("--relay-url", envvar="RELAY_URL",
              help="Relay server base URL (enables relay connector)")
@click.option("--token", envvar="RELAY_TOKEN",
              help="Bearer token for relay authentication")
@click.pass_context
def serve_cmd(ctx, host, port, relay_url, token):
    """Start the REST API server.
    If --relay-url is provided, also starts the relay connector in the background.
    """
    try:
        import uvicorn
    except ImportError:
        click.echo("Missing dependencies. Run: pip install fastapi uvicorn", err=True)
        raise SystemExit(1)

    # Pass relay config via env so the lifespan handler picks it up
    client = ctx.obj.get("client") or ""
    if relay_url:
        if not token:
            raise click.UsageError("--token is required when --relay-url is set")
        if not client:
            raise click.UsageError("--client is required for relay")
        os.environ["RELAY_URL"] = relay_url
        os.environ["RELAY_TOKEN"] = token
        os.environ["RELAY_CLIENT"] = client
        os.environ["RELAY_UPSTREAM"] = f"http://{host}:{port}"
        click.echo(f"Relay connector → {relay_url} (client={client})")

    click.echo(f"API server on http://{host}:{port}")
    click.echo(f"Docs at http://{host}:{port}/docs")
    os.environ["DATABASE_URL"] = ctx.obj["db_url"]
    uvicorn.run("fastwado.api:app", host=host, port=port, log_level="info")


# ── relay command ───────────────────────────────────────────────────────

@cli.command("relay")
@click.option("--relay-url", envvar="RELAY_URL", required=True,
              help="Relay server base URL")
@click.option("--token", envvar="RELAY_TOKEN", required=True,
              help="Bearer token for relay authentication")
@click.option("--upstream", envvar="UPSTREAM_URL",
              default="http://localhost:8001",
              help="Local API base URL (default: http://localhost:8001)")
@click.pass_context
def relay_cmd(ctx, relay_url, token, upstream):
    """Connect to the Mirror relay via WebSocket and proxy requests
    to the local API."""
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        click.echo("Missing dependencies. Run: pip install aiohttp", err=True)
        raise SystemExit(1)

    client = ctx.obj.get("client") or os.environ.get("DICOM_CLIENT")
    if not client:
        raise click.UsageError("Missing --client or DICOM_CLIENT")

    from fastwado.relay import RelayConnector

    connector = RelayConnector(
        relay_base=relay_url.rstrip("/"),
        client=client,
        token=token,
        upstream=upstream.rstrip("/"),
    )

    click.echo(f"Relay connector for client={client}")
    click.echo(f"  Relay:   {relay_url}")
    click.echo(f"  Upstream: {upstream}")

    try:
        asyncio.run(connector.run())
    except KeyboardInterrupt:
        click.echo("\nShutting down.")

