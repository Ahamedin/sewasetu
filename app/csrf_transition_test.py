import importlib.util
import os
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("SEWASETU_DB_PASSWORD", "test-db")
os.environ.setdefault("SEWASETU_FLASK_SECRET_KEY", "test-flask")
os.environ.setdefault("SEWASETU_ADMIN_USERNAME", "test-admin")
os.environ.setdefault("SEWASETU_ADMIN_PASSWORD_HASH", "test-hash")
os.environ.setdefault("SEWASETU_RESET_CODE_PEPPER", "test-pepper")
os.environ.setdefault("SEWASETU_REDIS_URL", "redis://test.invalid/0")

requests_stub = types.ModuleType("requests")
requests_stub.post = Mock()
sys.modules["requests"] = requests_stub

psycopg2_stub = types.ModuleType("psycopg2")
psycopg2_stub.connect = Mock()
sys.modules["psycopg2"] = psycopg2_stub

fpdf_stub = types.ModuleType("fpdf")
fpdf_stub.FPDF = Mock
sys.modules["fpdf"] = fpdf_stub

redis_stub = types.ModuleType("redis")
redis_stub.Redis = Mock()
redis_stub.Redis.from_url.return_value = Mock()
sys.modules["redis"] = redis_stub

APP_PATH = Path(__file__).with_name("app.py")
spec = importlib.util.spec_from_file_location("csrf_transition_app", APP_PATH)
portal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(portal)


class FakeCursor:
    rowcount = 1

    def __init__(self):
        self.statements = []

    def execute(self, statement, params=()):
        self.statements.append((statement, params))

    def close(self):
        pass


class FakeConnection:
    def __init__(self):
        self.cursor_instance = FakeCursor()
        self.commits = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def close(self):
        pass


class CsrfAndTransitionTests(unittest.TestCase):
    def setUp(self):
        portal.app.config.update(TESTING=True, WTF_CSRF_ENABLED=True)
        self.client = portal.app.test_client()
        self.connection = FakeConnection()
        self.db_patch = patch.object(
            portal, "get_db", return_value=self.connection
        )
        self.db_patch.start()
        with self.client.session_transaction() as session:
            session["admin"] = True

    def tearDown(self):
        self.db_patch.stop()

    def test_missing_csrf_token_is_rejected_before_database(self):
        with patch.object(
            portal, "get_db",
            side_effect=AssertionError("CSRF-bypassed request reached database"),
        ):
            response = self.client.post("/admin/approve/101")
        self.assertEqual(response.status_code, 400)

    def test_valid_csrf_token_allows_pending_transition_only(self):
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 200)
        token = re.search(
            rb'name="csrf_token" type="hidden" value="([^"]+)"', response.data
        ).group(1).decode("ascii")

        response = self.client.post(
            "/admin/approve/101", data={"csrf_token": token}
        )
        self.assertEqual(response.status_code, 302)
        statement = self.connection.cursor_instance.statements[0][0]
        self.assertIn("status = 'PENDING'", statement)
        self.assertEqual(self.connection.commits, 1)


if __name__ == "__main__":
    unittest.main()
