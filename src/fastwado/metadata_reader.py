"""Extract DICOM metadata tags (window, spacing, orientation, position, etc.)
from 1–2 instances per series for the Mirror viewer."""

import logging

log = logging.getLogger(__name__)


def read_series_metadata(filepaths):
    """Produce a metadata dict for a series by reading at most the first
    two instances from *filepaths* (pre-sorted by InstanceNumber ascending).

    Returns a dict with all the expected keys; missing / unparseable tags
    are set to ``None`` (or ``0`` for ``number_of_frames``).
    """
    result = {
        "pixel_spacing": None,
        "window_width": None,
        "window_center": None,
        "image_orientation_patient": None,
        "image_position_patient": None,
        "frame_of_reference_uid": None,
        "slice_thickness": None,
        "frame_time": None,
        "number_of_frames": 0,
        "rows": None,
        "columns": None,
    }
    if not filepaths:
        return result

    t1 = _read_raw_tags(filepaths[0])

    # pixel_spacing — first value of the pair
    ps = _get_str(t1, 0x0028, 0x0030)
    if ps:
        result["pixel_spacing"] = _first_float(ps)

    result["window_width"] = _first_float(_get_str(t1, 0x0028, 0x1051))
    result["window_center"] = _first_float(_get_str(t1, 0x0028, 0x1050))
    result["image_orientation_patient"] = _float_array(
        _get_str(t1, 0x0020, 0x0037), 6
    )
    result["frame_of_reference_uid"] = _get_str(t1, 0x0020, 0x0052)
    result["slice_thickness"] = _safe_float(_get_str(t1, 0x0018, 0x0050))
    result["frame_time"] = _safe_float(_get_str(t1, 0x0018, 0x1063))
    result["number_of_frames"] = _safe_int(_get_str(t1, 0x0028, 0x0008)) or 0
    result["rows"] = _safe_int(_get_str(t1, 0x0028, 0x0010))
    result["columns"] = _safe_int(_get_str(t1, 0x0028, 0x0011))

    # image_position_patient — needs first two instances
    ipp = []
    ipp1 = _float_array(_get_str(t1, 0x0020, 0x0032), 3)
    if ipp1:
        ipp.append(ipp1)
    if len(filepaths) >= 2:
        t2 = _read_raw_tags(filepaths[1])
        ipp2 = _float_array(_get_str(t2, 0x0020, 0x0032), 3)
        if ipp2:
            ipp.append(ipp2)
    if ipp:
        result["image_position_patient"] = ipp

    return result


# ---------------------------------------------------------------------------
def _read_raw_tags(filepath):
    """Return {Tag → str} for all relevant DICOM tags in *filepath*."""
    try:
        import gdcm
    except ImportError:
        log.error("GDCM not installed.")
        return {}

    reader = gdcm.Reader()
    reader.SetFileName(filepath)
    if not reader.Read():
        return {}

    ds = reader.GetFile().GetDataSet()
    sf = gdcm.StringFilter()
    sf.SetFile(reader.GetFile())

    raw = {}
    for g, e in [
        (0x0028, 0x0030),  # PixelSpacing
        (0x0028, 0x1051),  # WindowWidth
        (0x0028, 0x1050),  # WindowCenter
        (0x0020, 0x0037),  # ImageOrientationPatient
        (0x0020, 0x0032),  # ImagePositionPatient
        (0x0020, 0x0052),  # FrameOfReferenceUID
        (0x0018, 0x0050),  # SliceThickness
        (0x0018, 0x1063),  # FrameTime
        (0x0028, 0x0008),  # NumberOfFrames
        (0x0028, 0x0010),  # Rows
        (0x0028, 0x0011),  # Columns
    ]:
        tag = gdcm.Tag(g, e)
        if not ds.FindDataElement(tag):
            continue
        de = ds.GetDataElement(tag)
        if de.IsEmpty():
            continue
        try:
            val = sf.ToString(tag).strip()
            if val:
                raw[(g, e)] = val
        except Exception:
            try:
                val = str(de.GetValue()).strip()
                if val:
                    raw[(g, e)] = val
            except Exception:
                pass

    return raw


# ---------------------------------------------------------------------------
def _get_str(tags, g, e):
    return tags.get((g, e))


def _first_float(val):
    """``'350.0\\40.0'`` → 350.0"""
    if val is None:
        return None
    return _safe_float(val.split("\\")[0])


def _safe_float(val):
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val):
    if val is None:
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


def _float_array(val, expected_len):
    """``'1.0\\0.0\\0.0\\0.0\\1.0\\0.0'`` → [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]

    Returns ``None`` if the value can't be parsed or has the wrong length.
    """
    if val is None:
        return None
    parts = []
    for p in val.split("\\"):
        try:
            parts.append(float(p))
        except (ValueError, TypeError):
            return None
    if len(parts) != expected_len:
        return None
    return parts
