import importlib.util
import ast
import os
import pkgutil
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from jinja2 import FileSystemLoader


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
    fpdf_stub.FPDF = Mock
    redis_stub = types.ModuleType("redis")
    redis_stub.Redis = Mock
    redis_stub.Redis.from_url = Mock(return_value=Mock())

    spec = importlib.util.spec_from_file_location("application_flow_app",
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
    module.app.jinja_loader = FileSystemLoader(
        str(APP_PATH.parent / "templates")
    )
    return module


class ApplicationFlowTests(unittest.TestCase):
    def setUp(self):
        self.module = load_app()
        self.client = self.module.app.test_client()
        with self.client.session_transaction() as session:
            session["verified_mobile"] = "test-mobile"
            session["form_data"] = {
                "applicant_name": "Test Applicant",
                "dob": "01/01/1950",
                "gender": "Male",
                "marital_status": "Unmarried",
                "village": "Test Village",
                "block": "Sonari",
                "bank_account": "",
                "ifsc": "TEST0001",
            }
            session["doc_path"] = "/tmp/test-document.pdf"

    def test_missing_bank_account_returns_to_bank_step(self):
        with patch.object(self.module, "get_db") as get_db:
            response = self.client.post("/declaration")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/form/3"))
        get_db.assert_not_called()

    def test_invalid_date_returns_to_personal_step(self):
        with self.client.session_transaction() as session:
            session["form_data"] = {
                "applicant_name": "Test Applicant",
                "dob": "not-a-date",
                "gender": "Male",
                "marital_status": "Unmarried",
                "village": "Test Village",
                "block": "Sonari",
                "bank_account": "123456",
                "ifsc": "TEST0001",
            }
        with patch.object(self.module, "get_db") as get_db:
            response = self.client.post("/declaration")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/form/1"))
        get_db.assert_not_called()

    def test_valid_dd_mm_yyyy_date_reaches_submission(self):
        self.assertEqual(
            self.module.parse_date_of_birth("23/10/1950").strftime("%d/%m/%Y"),
            "23/10/1950",
        )

    def test_each_missing_personal_field_returns_to_step_one(self):
        cases = (
            ("applicant_name", "missing_applicant_name"),
            ("dob", "missing_date_of_birth"),
            ("gender", "missing_gender"),
            ("marital_status", "missing_marital_status"),
        )
        for field, reason in cases:
            with self.subTest(field=field):
                with self.client.session_transaction() as session:
                    data = {
                        "applicant_name": "Test Applicant",
                        "dob": "01/01/1950",
                        "gender": "Male",
                        "marital_status": "Unmarried",
                        "village": "Test Village",
                        "block": "Sonari",
                        "bank_account": "123456",
                        "ifsc": "TEST0001",
                    }
                    data["bank_account"] = "123456"
                    data[field] = ""
                    session["form_data"] = data
                with patch.object(self.module.app.logger, "info") as log:
                    response = self.client.post("/declaration")
                self.assertTrue(response.location.endswith("/form/1"))
                self.assertIn(reason, str(log.call_args))

    def test_database_failure_is_not_disguised_as_step_redirect(self):
        with self.client.session_transaction() as session:
            data = dict(session["form_data"])
            data["bank_account"] = "123456"
            session["form_data"] = data
        with patch.object(
            self.module, "get_db", side_effect=RuntimeError("database failure")
        ):
            with self.assertRaises(RuntimeError):
                self.client.post("/declaration")

    def test_database_insert_failure_rolls_back_and_is_not_redirected(self):
        with self.client.session_transaction() as session:
            data = dict(session["form_data"])
            data["bank_account"] = "123456"
            session["form_data"] = data

        class Cursor:
            def execute(self, statement, params=()):
                if "SELECT count(*) FROM applications" in statement:
                    raise RuntimeError("insert failure")

            def close(self):
                pass

        class Connection:
            def __init__(self):
                self.rollback_count = 0
                self.cursor_instance = Cursor()

            def cursor(self):
                return self.cursor_instance

            def rollback(self):
                self.rollback_count += 1

            def close(self):
                pass

        connection = Connection()
        with patch.object(self.module, "get_db", return_value=connection):
            with self.assertRaises(RuntimeError):
                self.client.post("/declaration")
        self.assertEqual(connection.rollback_count, 1)

    def test_complete_six_step_session_reaches_confirmation(self):
        with self.client.session_transaction() as session:
            session["form_data"] = {
                "applicant_name": "Test Applicant",
                "dob": "01/01/1950",
                "gender": "Male",
                "marital_status": "Unmarried",
                "village": "Test Village",
                "block": "Sonari",
                "bank_account": "123456",
                "ifsc": "TEST0001",
            }
            session["doc_path"] = "/tmp/test-document.pdf"

        class Cursor:
            def __init__(self):
                self.statement = ""

            def execute(self, statement, params=()):
                self.statement = statement

            def fetchone(self):
                if "SELECT count(*) FROM applications" in self.statement:
                    return (0,)
                if "RETURNING id" in self.statement:
                    return (101,)
                if "SELECT count(*) FROM portal_users" in self.statement:
                    return (1,)
                return None

            def close(self):
                pass

        class Connection:
            def __init__(self):
                self.cursor_instance = Cursor()
                self.commit_count = 0

            def cursor(self):
                return self.cursor_instance

            def commit(self):
                self.commit_count += 1

            def close(self):
                pass

        connection = Connection()
        with patch.object(self.module, "get_db", return_value=connection), \
                patch.object(
                    self.module, "generate_acknowledgment",
                    return_value=b"acknowledgment",
                ), \
                patch.object(self.module, "send_sms"):
            response = self.client.post("/declaration")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Application Submitted", response.data)
        self.assertEqual(connection.commit_count, 1)

    def test_missing_document_returns_to_upload_step(self):
        with self.client.session_transaction() as session:
            data = dict(session["form_data"])
            data["bank_account"] = "123456"
            session["form_data"] = data
            session["doc_path"] = ""
        response = self.client.post("/declaration")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/upload"))


if __name__ == "__main__":
    unittest.main()
