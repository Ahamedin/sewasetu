import importlib.util
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


APP_PATH = Path(__file__).with_name("app.py")


def load_app(upload_dir):
    environment = {
        "SEWASETU_DB_PASSWORD": "test-db",
        "SEWASETU_FLASK_SECRET_KEY": "test-secret",
        "SEWASETU_ADMIN_USERNAME": "test-admin",
        "SEWASETU_ADMIN_PASSWORD_HASH": "test-hash",
        "SEWASETU_RESET_CODE_PEPPER": "test-pepper",
        "SEWASETU_REDIS_URL": "redis://test.invalid/0",
        "SEWASETU_SESSION_COOKIE_SECURE": "false",
        "SEWASETU_SESSION_COOKIE_HTTPONLY": "true",
        "SEWASETU_SESSION_COOKIE_SAMESITE": "Lax",
        "SEWASETU_ALLOWED_HOSTS": "",
        "SEWASETU_TRUSTED_PROXY_COUNT": "0",
    }
    requests_stub = types.ModuleType("requests")
    requests_stub.post = Mock()
    psycopg2_stub = types.ModuleType("psycopg2")
    psycopg2_stub.connect = Mock()
    fpdf_stub = types.ModuleType("fpdf")
    fpdf_stub.FPDF = Mock
    redis_stub = types.ModuleType("redis")
    redis_stub.Redis = Mock
    redis_stub.Redis.from_url = Mock(return_value=Mock())

    module_name = "upload_test_app"
    spec = importlib.util.spec_from_file_location(module_name, APP_PATH)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, environment, clear=True), \
            patch.dict(sys.modules, {
                "requests": requests_stub,
                "psycopg2": psycopg2_stub,
                "fpdf": fpdf_stub,
                "redis": redis_stub,
            }):
        spec.loader.exec_module(module)
    module.UPLOAD_DIR = upload_dir
    module.app.config["WTF_CSRF_ENABLED"] = False
    return module


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.module = load_app(self.temp_dir.name)
        self.client = self.module.app.test_client()
        with self.client.session_transaction() as session:
            session["verified_mobile"] = "test-mobile"

    def tearDown(self):
        self.temp_dir.cleanup()

    def upload(self, name, content, content_type):
        return self.client.post(
            "/upload",
            data={
                "document": (io.BytesIO(content), name, content_type),
            },
            headers={"Host": "localhost"},
        )

    def test_valid_pdf_is_accepted_and_saved(self):
        response = self.upload("birth.pdf", b"%PDF-1.7 test", "application/pdf")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/declaration", response.headers["Location"])
        files = list(Path(self.temp_dir.name, "documents").glob("*.pdf"))
        self.assertEqual(len(files), 1)
        with self.client.session_transaction() as session:
            self.assertEqual(session["doc_path"], str(files[0]))

    def test_valid_jpg_is_accepted_and_saved(self):
        response = self.upload("voter.jpg", b"\xff\xd8\xfftest", "image/jpeg")
        self.assertEqual(response.status_code, 302)
        files = list(Path(self.temp_dir.name, "documents").glob("*.jpg"))
        self.assertEqual(len(files), 1)

    def test_invalid_extension_is_rejected_clearly(self):
        response = self.upload("birth.png", b"\x89PNG\r\n", "image/png")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Only JPG and PDF files are accepted.", response.data)

    def test_oversized_file_is_rejected_clearly(self):
        response = self.upload(
            "birth.pdf",
            b"%PDF-" + b"x" * self.module.MAX_DOCUMENT_SIZE,
            "application/pdf",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Document size must not exceed 5 MB.", response.data)

    def test_missing_file_is_rejected_clearly(self):
        response = self.client.post(
            "/upload",
            data={},
            headers={"Host": "localhost"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Please select an age-proof document.", response.data)

    def test_mismatched_content_is_rejected(self):
        response = self.upload("birth.pdf", b"not a pdf", "application/pdf")
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"The document content does not match its file type.",
            response.data,
        )


if __name__ == "__main__":
    unittest.main()
