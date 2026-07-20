"""Generate a minimal valid DICOM file for testing."""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT = os.path.join(HERE, "test1.dcm")


def create_test_dicom():
    import pydicom
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ImplicitVRLittleEndian

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    file_meta.MediaStorageSOPInstanceUID = (
        "1.2.840.113619.2.5.1762389987.2836.9999.1.1"
    )
    file_meta.TransferSyntaxUID = ImplicitVRLittleEndian
    file_meta.ImplementationVersionName = "DICOM_INDEX_TEST"

    ds = Dataset()
    ds.SpecificCharacterSet = "ISO_IR 100"
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    ds.SOPInstanceUID = "1.2.840.113619.2.5.1762389987.2836.9999.1.1"
    ds.StudyInstanceUID = "1.2.840.113619.2.5.1762389987.2836.9999"
    ds.SeriesInstanceUID = "1.2.840.113619.2.5.1762389987.2836.9999.1"
    ds.StudyID = "STUDY001"
    ds.StudyDate = "20240101"
    ds.StudyDescription = "Test Study Desc"
    ds.AccessionNumber = "ACC999"
    ds.ReferringPhysicianName = "Doctor^Test"
    ds.PatientName = "Patient^Test"
    ds.PatientID = "PAT999"
    ds.PatientBirthDate = "19700101"
    ds.PatientSex = "O"
    ds.Modality = "CT"
    ds.SeriesNumber = "1"
    ds.SeriesDescription = "Test Series Desc"
    ds.InstanceNumber = "1"

    ds.file_meta = file_meta
    ds.is_implicit_VR = True
    ds.is_little_endian = True

    ds.save_as(OUTPUT, write_like_original=False)
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    create_test_dicom()
