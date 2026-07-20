import os
from datetime import datetime


def fmt_bytes(num):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} PB"


def generate_report(stats, scan_stats, non_dicom_count, client, path):
    """Generate a Markdown report string from DB stats + scan stats."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"# DICOM Scan Report",
        "",
        f"**Client** — `{client}`  ",
        f"**Path** — `{path}`  ",
        f"**Date** — {now}  ",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Studies | {stats['total_studies']} |",
        f"| Series | {stats['total_series']} |",
        f"| Instances | {stats['total_instances']} |",
        f"| Total size | {fmt_bytes(stats['total_size_bytes'])} |",
        f"| Non-DICOM files | {non_dicom_count} |",
        f"| Scan duration | {scan_stats.get('scan_duration_s', 0)} s |",
        "",
    ]

    modalities = stats.get("modalities", [])
    if modalities:
        lines.append("## Modalities")
        lines.append("")
        lines.append("| Modality | Count |")
        lines.append("|----------|-------|")
        for mod, cnt in modalities:
            lines.append(f"| {mod} | {cnt} |")
        lines.append("")

    study_list = stats.get("studies", [])
    if study_list:
        lines.append("## Studies (last 100)")
        lines.append("")
        lines.append(
            "| Study IUID | ID | Date | Description | Patient | Modalities | Series | Inst. |"
        )
        lines.append(
            "|------------|----|------|-------------|---------|------------|--------|------|"
        )
        for s in study_list:
            mods = ", ".join(s["modalities"]) if s["modalities"] else "-"
            suid_short = s["study_iuid"][:24] + "..."
            lines.append(
                f"| `{suid_short}` | {s['study_id'] or '-'} "
                f"| {s['study_date'] or '-'} | {s['study_description'] or '-'} "
                f"| {s['patient_name'] or '-'} | {mods} "
                f"| {s['num_series']} | {s['num_instances']} |"
            )
        lines.append("")

    return "\n".join(lines)


def write_report(report_text, client, output_dir=None):
    out_dir = output_dir or os.getcwd()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"dicom_report_{client}_{ts}.md"
    fpath = os.path.join(out_dir, fname)
    with open(fpath, "w") as f:
        f.write(report_text)
    return fpath


def write_non_dicom_log(paths, client, output_dir=None):
    if not paths:
        return None
    out_dir = output_dir or os.getcwd()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"non_dicom_{client}_{ts}.log"
    fpath = os.path.join(out_dir, fname)
    with open(fpath, "w") as f:
        for p in paths:
            f.write(p + "\n")
    return fpath
