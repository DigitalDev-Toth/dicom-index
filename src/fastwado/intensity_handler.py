"""Intensity / Hounsfield calculation for the Mirror viewer.

Reads pixel data from DICOM files and calculates min/avg/max over
row-major index ranges supplied by the client (rows_array).
"""

import logging

import numpy as np

log = logging.getLogger(__name__)


def read_intensity_data(filepath):
    """Open a DICOM file and return a dict with everything needed for
    intensity extraction: rows, cols, samples, planar_config, num_frames,
    raw buffer pointer and numpy dtype, plus rescale metadata.

    Raises ValueError if the file cannot be read or has no pixel data.
    """
    import gdcm

    reader = gdcm.ImageReader()
    reader.SetFileName(filepath)
    if not reader.Read():
        raise ValueError("GDCM could not read pixel data")

    img = reader.GetImage()
    ds = reader.GetFile().GetDataSet()

    dims = img.GetDimensions()
    cols, rows = int(dims[0]), int(dims[1])

    pf = img.GetPixelFormat()
    samples = pf.GetSamplesPerPixel()
    bits_stored = pf.GetBitsStored()
    is_signed = pf.GetPixelRepresentation() != 0

    pi = img.GetPhotometricInterpretation()

    raw = img.GetBuffer()
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "surrogateescape")

    if bits_stored <= 8:
        dt = np.int8 if is_signed else np.uint8
        bps = 1
    elif bits_stored <= 16:
        dt = np.int16 if is_signed else np.uint16
        bps = 2
    elif bits_stored <= 32:
        dt = np.int32 if is_signed else np.uint32
        bps = 4
    else:
        dt = np.int64 if is_signed else np.uint64
        bps = 8

    bpp = bps * samples
    frame_size = rows * cols * bpp
    num_frames = max(1, len(raw) // frame_size)

    # Planar configuration
    planar = _get_tag_int(ds, 0x0028, 0x0006) or 0

    # Rescale
    slope = _get_tag_float(ds, 0x0028, 0x1053)
    intercept = _get_tag_float(ds, 0x0028, 0x1052)
    has_rescale = (slope is not None) and (intercept is not None)

    # Modality
    modality = (_get_tag_str(ds, 0x0008, 0x0060) or "").strip()

    sop_class = _get_tag_str(ds, 0x0008, 0x0016) or ""
    num_tags = _get_tag_int(ds, 0x0028, 0x0008)

    return {
        "rows": rows,
        "columns": cols,
        "modality": modality,
        "samples_per_pixel": samples,
        "number_of_frames": num_tags or num_frames,
        "sop_class_uid": sop_class,
        "planar_config": planar,
        "raw": raw,
        "dtype": dt,
        "bps": bps,
        "bpp": bpp,
        "frame_size": frame_size,
        "has_rescale": has_rescale,
        "rescale_slope": slope if has_rescale else None,
        "rescale_intercept": intercept if has_rescale else None,
    }


def compute_intensity(info, frame, rows_array):
    """Extract pixel values for the given *rows_array* ranges on *frame*
    (1-based).  Returns a dict with 'intensity' stats and optionally
    'hounsfield' stats.

    Raises ValueError on invalid frame or out-of-bounds indices.
    """
    rows = info["rows"]
    cols = info["columns"]
    samples = info["samples_per_pixel"]
    planar = info["planar_config"]
    raw = info["raw"]
    dt = info["dtype"]
    bps = info["bps"]
    bpp = info["bpp"]
    frame_size = info["frame_size"]
    num_frames = info["number_of_frames"]

    if frame < 1 or frame > num_frames:
        raise ValueError(f"frame {frame} out of range [1, {num_frames}]")

    max_idx = rows * cols - 1
    for start, end in rows_array:
        if start < 0 or end < start or end > max_idx:
            raise ValueError(f"range [{start}, {end}] out of bounds [0, {max_idx}]")

    frame_offset = (frame - 1) * frame_size

    all_vals = _extract_ranges(
        raw, frame_offset, cols, samples, planar, dt, bps, rows_array
    )

    count = int(all_vals.shape[0]) if samples == 1 else all_vals.shape[0]

    if samples == 1:
        vmin = float(np.min(all_vals))
        vmax = float(np.max(all_vals))
        vavg = float(np.mean(all_vals))
        result = {
            "intensity": {"min": vmin, "avg": round(vavg, 4), "max": vmax},
        }
    else:
        # RGB: luminance = R*0.29 + G*0.59 + B*0.14
        L = (all_vals[:, 0] * 0.29 + all_vals[:, 1] * 0.59 + all_vals[:, 2] * 0.14)
        # Separate channels for avg
        avg_r = round(float(np.mean(all_vals[:, 0])), 4)
        avg_g = round(float(np.mean(all_vals[:, 1])), 4)
        avg_b = round(float(np.mean(all_vals[:, 2])), 4)
        idx_min = int(np.argmin(L))
        idx_max = int(np.argmax(L))
        vmin_trip = [float(x) for x in all_vals[idx_min]]
        vmax_trip = [float(x) for x in all_vals[idx_max]]
        result = {
            "intensity": {
                "min": vmin_trip,
                "avg": [avg_r, avg_g, avg_b],
                "max": vmax_trip,
            },
        }

    result["points_count"] = count

    if info["has_rescale"]:
        s = info["rescale_slope"]
        i = info["rescale_intercept"]
        if samples == 1:
            hu = all_vals * s + i
            result["hounsfield"] = {
                "min": round(float(np.min(hu)), 4),
                "avg": round(float(np.mean(hu)), 4),
                "max": round(float(np.max(hu)), 4),
                "rescale_slope": s,
                "rescale_intercept": i,
            }
        else:
            result["hounsfield"] = None

    return result


# ---------------------------------------------------------------------------
def _extract_ranges(raw, frame_offset, cols, samples, planar, dt, bps, rows_array):
    """Bulk-read pixel ranges and return a numpy array."""
    parts = []

    for start, end in rows_array:
        count = end - start + 1
        idx_start = frame_offset + start * bps * samples
        idx_len = count * bps * samples

        if samples == 1 or planar == 0:
            chunk = raw[idx_start : idx_start + idx_len]
            arr = np.frombuffer(chunk, dtype=dt).reshape(count, samples) if samples > 1 else np.frombuffer(chunk, dtype=dt)
        else:
            # planar == 1: channels are separated
            channel_size = cols * rows * bps  # per channel
            ch_parts = []
            for ch in range(samples):
                ch_offset = frame_offset + ch * channel_size + start * bps
                ch_raw = raw[ch_offset : ch_offset + count * bps]
                ch_parts.append(np.frombuffer(ch_raw, dtype=dt))
            arr = np.column_stack(ch_parts)

        parts.append(arr.astype(np.float64))

    return np.concatenate(parts)


def _get_tag_str(ds, group, elem):
    import gdcm

    tag = gdcm.Tag(group, elem)
    if ds.FindDataElement(tag):
        de = ds.GetDataElement(tag)
        if not de.IsEmpty():
            try:
                return str(de.GetValue()).strip()
            except Exception:
                return None
    return None


def _get_tag_int(ds, group, elem):
    val = _get_tag_str(ds, group, elem)
    if val is None:
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


def _get_tag_float(ds, group, elem):
    val = _get_tag_str(ds, group, elem)
    if val is None:
        return None
    try:
        return float(val.split("\\")[0])
    except (ValueError, AttributeError):
        return None
