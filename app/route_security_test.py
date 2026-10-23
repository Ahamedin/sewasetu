import hashlib
import importlib.util
import ast
import os
import pkgutil
import sys
import threading
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from jinja2 import FileSystemLoader


os.environ.setdefault("SEWASETU_DB_PASSWORD", "test-db-password")
os.environ.setdefault("SEWASETU_FLASK_SECRET_KEY", "test-flask-secret")
os.environ.setdefault("SEWASETU_ADMIN_USERNAME", "test-admin")
os.environ.setdefault("SEWASETU_ADMIN_PASSWORD_HASH", "test-admin-hash")
os.environ.setdefault("SEWASETU_RESET_CODE_PEPPER", "test-reset-pepper")
os.environ.setdefault("SEWASETU_REDIS_URL", "redis://test.invalid/0")


if not hasattr(pkgutil, "get_loader"):
    pkgutil.get_loader = lambda name: importlib.util.find_spec(name)
if not hasattr(ast, "Str"):
    class LegacyStr(ast.Constant):
        def __init__(self, value="", s=None):
            if s is not None:
                value = s
            super().__init__(value=value)
            self.s = value

    ast.Str = LegacyStr


APP_PATH = Path(__file__).with_name("app.py")
app_spec = importlib.util.spec_from_file_location("sewasetu_route_app", APP_PATH)
module = importlib.util.module_from_spec(app_spec)

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


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.row = None

    def execute(self, statement, params=()):
        self.connection.statements.append(statement)
        if self.connection.fail_on and self.connection.fail_on in statement:
            raise RuntimeError("database failure")
        if "SELECT password_hash FROM portal_users" in statement:
            self.row = ((self.connection.password_hash,)
                        if self.connection.user_exists else None)
        elif "SELECT mobile FROM portal_users" in statement:
            self.row = (("test-mobile",) if self.connection.user_exists else None)
        elif "INSERT INTO otps" in statement:
            self.connection.otp = (params[1], params[2])
            self.row = None
        elif "SELECT code, created_at FROM otps" in statement:
            if "FOR UPDATE" in statement:
                self.connection.lock.acquire()
            self.row = self.connection.otp
        elif "UPDATE portal_users SET password_hash" in statement:
            self.connection.password_hash = params[0]
        elif "DELETE FROM otps" in statement:
            if len(params) == 1 or (
                    self.connection.otp and self.connection.otp[0] == params[1]):
                self.connection.otp = None
        elif "SELECT id FROM applications" in statement:
            self.row = (1,) if self.connection.user_exists else None

    def fetchone(self):
        row, self.row = self.row, None
        return row

    def close(self):
        pass


class FakeConnection:
    def __init__(self, password_hash):
        self.password_hash = password_hash
        self.user_exists = True
        self.otp = None
        self.statements = []
        self.fail_on = None
        self.rollback_count = 0
        self.commit_count = 0
        self.lock = threading.Lock()

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commit_count += 1
        if self.lock.locked():
            self.lock.release()

    def rollback(self):
        self.rollback_count += 1
        if self.lock.locked():
            self.lock.release()

    def close(self):
        pass


class FakeLimiter:
    def __init__(self, request_allowed=True, attempt_allowed=True):
        self.request_allowed_value = request_allowed
        self.attempt_allowed_value = attempt_allowed
        self.request_calls = 0
        self.attempt_calls = 0

    def request_allowed(self, mobile, ip_address):
        self.request_calls += 1
        return self.request_allowed_value

    def attempt_allowed(self, mobile):
        self.attempt_calls += 1
        return self.attempt_allowed_value

    def clear_attempts(self, mobile):
        pass


app_spec.loader.exec_module(module)
module.app.jinja_loader = FileSystemLoader(str(APP_PATH.parent / "templates"))
module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)


class RouteSecurityTests(unittest.TestCase):
    def setUp(self):
        self.password = "a-secure-password"
        self.connection = FakeConnection(module.hash_password(self.password))
        self.limiter = FakeLimiter()
        self.sms = Mock()
        self.db = patch.object(module, "get_db", return_value=self.connection)
        self.limiter_patch = patch.object(module, "reset_rate_limiter", self.limiter)
        self.sms_patch = patch.object(module, "send_sms", self.sms)
        self.db.start()
        self.limiter_patch.start()
        self.sms_patch.start()
        self.client = module.app.test_client()

    def tearDown(self):
        self.sms_patch.stop()
        self.limiter_patch.stop()
        self.db.stop()

    def test_successful_and_failed_login(self):
        response = self.client.post(
            "/status", data={"mobile": "test-mobile", "password": self.password}
        )
        self.assertEqual(response.status_code, 302)
        failed = self.client.post(
            "/status", data={"mobile": "test-mobile", "password": "wrong"}
        )
        self.assertEqual(failed.status_code, 200)

    def test_legacy_login_upgrades_hash(self):
        self.connection.password_hash = hashlib.sha256(
            self.password.encode("utf-8")
        ).hexdigest()
        response = self.client.post(
            "/status", data={"mobile": "test-mobile", "password": self.password}
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.connection.password_hash.startswith("p2$"))

    def test_temporary_password_requires_reset_after_expiry(self):
        self.connection.password_hash = module.hash_temporary_password(
            self.password, datetime.now() - timedelta(seconds=1)
        )
        response = self.client.post(
            "/status", data={"mobile": "test-mobile", "password": self.password}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/status/reset", response.location)

    def test_reset_request_has_cooldown_and_sends_once(self):
        response = self.client.post("/status/reset", data={"mobile": "test-mobile"})
        self.assertEqual(response.status_code, 200)
        self.sms.assert_called_once()
        self.limiter.request_allowed_value = False
        response = self.client.post("/status/reset", data={"mobile": "test-mobile"})
        self.assertEqual(response.status_code, 200)
        self.sms.assert_called_once()

    def test_invalid_and_expired_codes_do_not_change_password(self):
        created = datetime.now() - timedelta(seconds=301)
        self.connection.otp = (module.reset_code_digest("123456"), created)
        response = self.client.post(
            "/status/reset",
            data={"mobile": "test-mobile", "code": "123456",
                  "new_password": "new-secure-password"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.connection.commit_count, 0)
        self.assertTrue(self.connection.password_hash.startswith("p2$"))

    def test_attempt_limit_invalidates_challenge(self):
        self.limiter.attempt_allowed_value = False
        self.connection.otp = (module.reset_code_digest("123456"), datetime.now())
        response = self.client.post(
            "/status/reset",
            data={"mobile": "test-mobile", "code": "000000",
                  "new_password": "new-secure-password"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.connection.otp)

    def test_successful_reset_sends_no_sms_and_consumes_code_once(self):
        self.connection.otp = (module.reset_code_digest("123456"), datetime.now())
        response = self.client.post(
            "/status/reset",
            data={"mobile": "test-mobile", "code": "123456",
                  "new_password": "new-secure-password"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.connection.otp)
        self.assertEqual(self.connection.commit_count, 1)
        self.sms.assert_not_called()

    def test_reset_rolls_back_on_database_error(self):
        self.connection.otp = (module.reset_code_digest("123456"), datetime.now())
        self.connection.fail_on = "UPDATE portal_users"
        with self.assertRaises(RuntimeError):
            self.client.post(
                "/status/reset",
                data={"mobile": "test-mobile", "code": "123456",
                      "new_password": "new-secure-password"},
            )
        self.assertEqual(self.connection.rollback_count, 1)
        self.sms.assert_not_called()

    def test_concurrent_consumption_is_guarded_by_row_lock_query(self):
        self.connection.otp = (module.reset_code_digest("123456"), datetime.now())
        results = []

        def consume():
            with module.app.test_client() as client:
                response = client.post(
                    "/status/reset",
                    data={"mobile": "test-mobile", "code": "123456",
                          "new_password": "new-secure-password"},
                )
                results.append(response.status_code)

        first = threading.Thread(target=consume)
        second = threading.Thread(target=consume)
        first.start()
        second.start()
        first.join()
        second.join()
        self.assertEqual(sorted(results), [200, 200])
        self.assertIn("FOR UPDATE", " ".join(self.connection.statements))


if __name__ == "__main__":
    unittest.main()
