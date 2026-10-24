import importlib.util
import io
import os
import pkgutil
import ast
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


if not hasattr(pkgutil, "get_loader"):
    pkgutil.get_loader = lambda name: importlib.util.find_spec(name)
class LegacyStr(ast.Constant):
    def __init__(self, value="", s=None):
        if s is not None:
            value = s
        super().__init__(value=value)
        self.s = value


ast.Str = LegacyStr


APP_PATH = Path(__file__).with_name("app.py")


class FakePdf:
    def __init__(self):
        self.values = []

    def set_text_shaping(self, **kwargs):
        pass

    def add_page(self):
        pass

    def add_font(self, *args, **kwargs):
        pass

    def set_font(self, *args, **kwargs):
        pass

    def cell(self, width, height, text="", **kwargs):
        self.values.append(str(text))

    def multi_cell(self, width, height, text="", **kwargs):
        self.values.append(str(text))

    def ln(self, *args, **kwargs):
        pass

    def output(self):
        return "\n".join(self.values).encode("utf-8")


def load_app():
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
        "SEWASETU_SESSION_LIFETIME_SECONDS": "1800",
        "SEWASETU_ALLOWED_HOSTS": "",
        "SEWASETU_TRUSTED_PROXY_COUNT": "0",
    }
    requests_stub = types.ModuleType("requests")
    requests_stub.post = Mock()
    psycopg2_stub = types.ModuleType("psycopg2")
    psycopg2_stub.connect = Mock()
    fpdf_stub = types.ModuleType("fpdf")
    fpdf_stub.FPDF = FakePdf
    redis_stub = types.ModuleType("redis")
    redis_stub.Redis = Mock
    redis_stub.Redis.from_url = Mock(return_value=Mock())

    spec = importlib.util.spec_from_file_location("acknowledgment_pdf_app",
                                                  APP_PATH)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, environment, clear=True), \
            patch.dict(sys.modules, {
                "requests": requests_stub,
                "psycopg2": psycopg2_stub,
                "fpdf": fpdf_stub,
                "redis": redis_stub,
            }):
        spec.loader.exec_module(module)
    module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return module


class Cursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.statements = []

    def execute(self, statement, params=()):
        self.statements.append((statement, params))

    def fetchone(self):
        return self.rows.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Connection:
    def __init__(self, rows):
        self.cursor_instance = Cursor(rows)
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def close(self):
        self.closed = True


class AcknowledgmentPdfTests(unittest.TestCase):
    def setUp(self):
        self.module = load_app()

    def test_generator_maps_populated_fields_and_decision_date(self):
        row = (
            "SSP26453142", "Test Applicant", "9000000000",
            "1950-10-23", "Test Village", "Sonari", "123456",
            "TEST0001", "2026-10-23 10:00", "PENDING", None,
        )
        cursor = Cursor([row])
        data = self.module.generate_acknowledgment(cursor, 1)
        text = data.decode("utf-8")
        for value in row[:-1]:
            self.assertIn(str(value), text)
        self.assertIn("Decided At", text)

    def test_generator_renders_null_fields_without_losing_other_values(self):
        row = (
            "SSP26453142", "Test Applicant", "9000000000",
            None, "Test Village", "Sonari", "123456", "TEST0001",
            "2026-10-23 10:00", "PENDING", None,
        )
        data = self.module.generate_acknowledgment(Cursor([row]), 1)
        text = data.decode("utf-8")
        self.assertIn("Test Applicant", text)
        self.assertIn("Test Village", text)
        self.assertIn("PENDING", text)

    def test_ack_route_regenerates_stale_cached_file(self):
        with tempfile.TemporaryDirectory() as directory:
            self.module.UPLOAD_DIR = directory
            path = Path(directory, "ack", "1.pdf")
            path.parent.mkdir()
            path.write_bytes(b"stale")
            row = (
                "SSP26453142", "Test Applicant", "9000000000",
                "1950-10-23", "Test Village", "Sonari", "123456",
                "TEST0001", "2026-10-23 10:00", "PENDING", None,
            )
            connection = Connection([("9000000000",), row])
            with patch.object(self.module, "get_db", return_value=connection):
                client = self.module.app.test_client()
                with client.session_transaction() as session:
                    session["admin"] = True
                response = client.get("/ack/1.pdf")
            self.assertEqual(response.status_code, 200)
            self.assertNotEqual(path.read_bytes(), b"stale")
            self.assertIn(b"Test Applicant", response.data)
            self.assertTrue(connection.closed)

    def test_ack_route_preserves_authorization(self):
        client = self.module.app.test_client()
        with patch.object(
            self.module, "get_db",
            side_effect=AssertionError("unauthorized database access"),
        ):
            response = client.get("/ack/1.pdf")
        self.assertEqual(response.status_code, 403)

    def test_status_and_pdf_use_the_same_summary_column_order(self):
        source = APP_PATH.read_text(encoding="utf-8-sig")
        self.assertIn(
            "APPLICATION_SUMMARY_COLUMNS",
            source,
        )
        self.assertIn("SELECT %s FROM applications WHERE id = %%s", source)


if __name__ == "__main__":
    unittest.main()
