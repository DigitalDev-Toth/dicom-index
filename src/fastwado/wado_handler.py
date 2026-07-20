"""WADO handler: extract pixels from DICOM, apply windowing, return JPEG."""

import io
import logging

import numpy as np

log = logging.getLogger(__name__)


class NotAnImageError(ValueError):
    pass


def _get_tag_str(ds, group, elem):
    gdcm = gdcm_import()
    tag = gdcm.Tag(group, elem)
    if ds.FindDataElement(tag):
        de = ds.GetDataElement(tag)
        if not de.IsEmpty():
            try:
                return str(de.GetValue()).strip()
            except Exception:
                return None
    return None


def _get_tag_float(ds, group, elem):
    val = _get_tag_str(ds, group, elem)
    if val is None:
        return None
    try:
        return float(val.split("\\")[0])
    except (ValueError, AttributeError):
        return None


def gdcm_import():
    import gdcm
    return gdcm


def read_and_render(filepath, quality=85, rows=None, columns=None):
    """Read a DICOM file, window it, optionally resize, return JPEG bytes.

    Returns (jpeg_bytes, width, height).  Raises NotAnImageError if the
    file is not a renderable DICOM image.
    """
    gdcm = gdcm_import()

    reader = gdcm.ImageReader()
    reader.SetFileName(filepath)
    if not reader.Read():
        raise NotAnImageError("GDCM ImageReader cannot read the file (no pixel data?)")

    img = reader.GetImage()
    ds = reader.GetFile().GetDataSet()

    # ── Validate this is an image ────────────────────────────────────────
    sop_class = _get_tag_str(ds, 0x0008, 0x0016) or ""
    if not is_image_sop_class(sop_class):
        raise NotAnImageError(sop_class)

    # ── Dimensions ───────────────────────────────────────────────────────
    dims = img.GetDimensions()
    orig_cols, orig_rows = int(dims[0]), int(dims[1])
    if orig_rows < 1 or orig_cols < 1:
        raise NotAnImageError("zero dimensions")

    pf = img.GetPixelFormat()
    samples = pf.GetSamplesPerPixel()
    bits_stored = pf.GetBitsStored()
    is_signed = pf.GetPixelRepresentation() != 0

    pi = img.GetPhotometricInterpretation().GetString()
    is_color = (samples == 3)

    # ── Numpy array from raw buffer ──────────────────────────────────────
    if bits_stored <= 8:
        dt = np.int8 if is_signed else np.uint8
        bpp = 1
    elif bits_stored <= 16:
        dt = np.int16 if is_signed else np.uint16
        bpp = 2
    else:
        dt = np.int32 if is_signed else np.uint32
        bpp = 4
    bpp *= samples

    raw = img.GetBuffer()
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "surrogateescape")
    if len(raw) != orig_rows * orig_cols * bpp:
        raise ValueError(f"buffer size mismatch: got {len(raw)}, expected {orig_rows*orig_cols*bpp}")

    if is_color:
        arr = np.frombuffer(raw, dtype=dt).reshape((orig_rows, orig_cols, samples))
    else:
        arr = np.frombuffer(raw, dtype=dt).reshape((orig_rows, orig_cols))

    # ── Render ───────────────────────────────────────────────────────────
    from PIL import Image

    if is_color:
        out = _render_color(arr, pi)
    else:
        slope = _get_tag_float(ds, 0x0028, 0x1053)
        intercept = _get_tag_float(ds, 0x0028, 0x1052)
        wc = _get_tag_float(ds, 0x0028, 0x1050)
        ww = _get_tag_float(ds, 0x0028, 0x1051)
        out = _render_gray(arr, pi, slope, intercept, wc, ww)

    # ── Resize ───────────────────────────────────────────────────────────
    out_cols, out_rows = orig_cols, orig_rows
    if rows is not None or columns is not None:
        out = _resize(out, rows, columns)
        out_cols, out_rows = out.size

    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=quality)
    return buf.getvalue(), out_cols, out_rows


def _resize(img, rows, columns):
    """Resize a PIL Image, maintaining aspect ratio when one dim is absent."""
    from PIL import Image

    w, h = img.size
    if rows is not None and columns is not None:
        size = (int(columns), int(rows))
        return img.resize(size, Image.LANCZOS)
    elif rows is not None:
        factor = int(rows) / h
        size = (max(1, int(w * factor)), int(rows))
        return img.resize(size, Image.LANCZOS)
    elif columns is not None:
        factor = int(columns) / w
        size = (int(columns), max(1, int(h * factor)))
        return img.resize(size, Image.LANCZOS)
    return img


# ---------------------------------------------------------------------------
def is_image_sop_class(uid):
    return uid.startswith("1.2.840.10008.5.1.4.1.1.")


def _render_gray(arr, pi_str, slope, intercept, wc, ww):
    from PIL import Image

    arr = arr.astype(np.float32)

    if slope is not None:
        arr = arr * slope
    if intercept is not None:
        arr = arr + intercept

    if wc is None or ww is None or ww <= 1e-6:
        flat = arr.ravel()
        p1 = np.percentile(flat, 1)
        p99 = np.percentile(flat, 99)
        ww = p99 - p1
        wc = (p1 + p99) * 0.5
        if ww <= 1e-6:
            ww = 1.0

    low = wc - ww * 0.5
    arr = (arr - low) * (255.0 / ww)
    arr = np.clip(arr, 0, 255).astype(np.uint8)

    if pi_str == "MONOCHROME1":
        arr = 255 - arr

    return Image.fromarray(arr, mode="L")


def _render_color(arr, pi_str):
    from PIL import Image

    if pi_str in ("YBR_FULL_422", "YBR_FULL"):
        arr = _ybr_to_rgb(arr)
    elif pi_str == "RGB":
        pass
    else:
        log.warning("unexpected photometric %s, using as-is", pi_str)

    arr = arr.astype(np.uint8) if arr.dtype != np.uint8 else arr
    return Image.fromarray(arr, mode="RGB")


def _ybr_to_rgb(ybr):
    ybr = ybr.astype(np.float32)
    Y = ybr[:, :, 0]
    Cb = ybr[:, :, 1] - 128.0
    Cr = ybr[:, :, 2] - 128.0
    R = Y + 1.402 * Cr
    G = Y - 0.344136 * Cb - 0.714136 * Cr
    B = Y + 1.772 * Cb
    return np.clip(np.stack([R, G, B], axis=2), 0, 255).astype(np.uint8)
