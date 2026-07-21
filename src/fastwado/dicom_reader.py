import logging

log = logging.getLogger(__name__)


def read_tags(filepath):
    """Read DICOM tags from a file using GDCM.

    Returns a dict mapping tag names to string values, or None if the file
    is not a valid DICOM file.
    """
    try:
        import gdcm
    except ImportError:
        log.error("GDCM is not installed. Install python-gdcm.")
        raise

    reader = gdcm.Reader()
    reader.SetFileName(filepath)
    if not reader.Read():
        return None

    ds = reader.GetFile().GetDataSet()
    sf = gdcm.StringFilter()
    sf.SetFile(reader.GetFile())

    tag_map = {
        "StudyInstanceUID": (0x0020, 0x000D),
        "SeriesInstanceUID": (0x0020, 0x000E),
        "SOPInstanceUID": (0x0008, 0x0018),
        "StudyID": (0x0020, 0x0010),
        "StudyDate": (0x0008, 0x0020),
        "StudyTime": (0x0008, 0x0030),
        "StudyDescription": (0x0008, 0x1030),
        "AccessionNumber": (0x0008, 0x0050),
        "ReferringPhysicianName": (0x0008, 0x0090),
        "PatientName": (0x0010, 0x0010),
        "PatientID": (0x0010, 0x0020),
        "PatientBirthDate": (0x0010, 0x0030),
        "PatientSex": (0x0010, 0x0040),
        "Modality": (0x0008, 0x0060),
        "SeriesNumber": (0x0020, 0x0011),
        "SeriesDescription": (0x0008, 0x103E),
        "BodyPartExamined": (0x0018, 0x0015),
        "InstanceNumber": (0x0020, 0x0013),
        "SOPClassUID": (0x0008, 0x0016),
    }

    result = {}
    for name, (group, elem) in tag_map.items():
        tag = gdcm.Tag(group, elem)
        if not ds.FindDataElement(tag):
            continue
        de = ds.GetDataElement(tag)
        if de.GetVL() == 0:
            continue
        try:
            val = sf.ToString(tag).strip()
            if val:
                result[name] = _sanitize(val)
        except Exception:
            try:
                val = str(de.GetValue()).strip()
                if val:
                    result[name] = _sanitize(val)
            except Exception:
                continue

    return result if result else None


def _sanitize(s):
    """Recover original bytes from surrogate escapes and decode properly.

    GDCM may return latin-1 characters as lone surrogates in the Python str.
    We recover the raw bytes with surrogateescape, then try UTF-8 first
    (for correctly-decoded text), falling back to ISO 8859-1 (latin-1) for
    Spanish/European names (e.g. 'Ñ', 'á').
    """
    b = s.encode("utf-8", errors="surrogateescape")
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("iso-8859-1")
