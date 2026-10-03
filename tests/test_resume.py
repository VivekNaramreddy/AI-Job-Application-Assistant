import tempfile
import unittest
from pathlib import Path

import src.resume as resume_module


class ResumeUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_resumes_dir = resume_module.RESUMES_DIR
        resume_module.RESUMES_DIR = Path(self.temp_dir.name)

    def tearDown(self):
        resume_module.RESUMES_DIR = self.original_resumes_dir
        self.temp_dir.cleanup()

    def test_saves_valid_pdf_with_safe_filename(self):
        saved = resume_module.save_uploaded_resume(
            "Vivek Resume 2026.pdf", b"%PDF-1.7\nresume"
        )

        self.assertTrue(saved.exists())
        self.assertEqual(saved.read_bytes(), b"%PDF-1.7\nresume")
        self.assertTrue(saved.name.startswith("Vivek_Resume_2026_"))

    def test_rejects_unsupported_file_type(self):
        with self.assertRaisesRegex(ValueError, "PDF, DOC, or DOCX"):
            resume_module.save_uploaded_resume("resume.txt", b"resume")

    def test_rejects_invalid_pdf_content(self):
        with self.assertRaisesRegex(ValueError, "not a valid PDF"):
            resume_module.save_uploaded_resume("resume.pdf", b"not a pdf")

    def test_configured_resume_requires_absolute_existing_path(self):
        with self.assertRaisesRegex(ValueError, "must be absolute"):
            resume_module.ResumeManager.configured_resume(
                {"resume_path": "resume.pdf"}
            )

    def test_configured_resume_returns_validated_selection(self):
        path = Path(self.temp_dir.name) / "candidate.pdf"
        path.write_bytes(b"%PDF-1.7\nresume")
        selection = resume_module.ResumeManager.configured_resume(
            {"resume_path": str(path.resolve())}
        )
        self.assertEqual(selection.path, path.resolve())
        self.assertEqual(selection.filename, "candidate.pdf")


if __name__ == "__main__":
    unittest.main()
