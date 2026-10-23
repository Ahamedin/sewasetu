import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


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


if __name__ == "__main__":
    unittest.main()
