import importlib.util
import os
import sys
import threading
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch


for name, value in {
    "SEWASETU_DB_PASSWORD": "test-db-password",
    "SEWASETU_FLASK_SECRET_KEY": "test-flask-secret",
    "SEWASETU_ADMIN_USERNAME": "test-admin",
    "SEWASETU_ADMIN_PASSWORD_HASH": "test-admin-hash",
    "SEWASETU_RESET_CODE_PEPPER": "test-reset-pepper",
    "SEWASETU_REDIS_URL": "redis://test.invalid/0",
}.items():
    os.environ.setdefault(name, value)

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
redis_stub.RedisError = type("RedisError", (Exception,), {})
redis_stub.Redis = Mock()
redis_stub.Redis.from_url.return_value = Mock()
sys.modules["redis"] = redis_stub

app_path = Path(__file__).with_name("app.py")
spec = importlib.util.spec_from_file_location("sewasetu_redis_outage_app", app_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.row = None

    def execute(self, statement, params=()):
        self.connection.statements.append(statement)
        if "SELECT mobile FROM portal_users" in statement:
            self.row = (("test-mobile",) if self.connection.user_exists else None)
        elif "SELECT code, created_at FROM otps" in statement:
            self.row = self.connection.otp
        elif "UPDATE portal_users SET password_hash" in statement:
            self.connection.pending_password_update = True
        elif "DELETE FROM otps" in statement:
            self.connection.pending_otp_delete = True
        elif "INSERT INTO otps" in statement:
            self.connection.pending_otp = (params[1], params[2])

    def fetchone(self):
        row, self.row = self.row, None
        return row

    def close(self):
        pass


class FakeConnection:
    def __init__(self):
        self.user_exists = True
        self.otp = None
        self.statements = []
        self.password_updated = False
        self.pending_password_update = False
        self.pending_otp = None
        self.pending_otp_delete = False
        self.commit_count = 0
        self.rollback_count = 0
        self.lock = threading.Lock()

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commit_count += 1
        if self.pending_password_update:
            self.password_updated = True
        if self.pending_otp is not None:
            self.otp = self.pending_otp
        if self.pending_otp_delete:
            self.otp = None
        self.pending_password_update = False
        self.pending_otp = None
        self.pending_otp_delete = False

    def rollback(self):
        self.rollback_count += 1
        self.pending_password_update = False
        self.pending_otp = None
        self.pending_otp_delete = False

    def close(self):
        pass


class FakeRedisClient:
    def __init__(self, failure=None):
        self.failure = failure

    def eval(self, *args):
        if self.failure == "request":
            raise redis_stub.RedisError("connection detail must not be logged")
        return 1

    def incr(self, key):
        if self.failure == "attempt":
            raise redis_stub.RedisError("connection detail must not be logged")
        return 6 if self.failure in ("exceeded", "exceeded-clear") else 1

    def expire(self, key, seconds):
        pass

    def delete(self, key):
        if self.failure in ("clear", "exceeded-clear"):
            raise redis_stub.RedisError("connection detail must not be logged")


def limiter_with_failure(failure=None):
    return module.ResetRateLimiter(FakeRedisClient(failure))


class RedisOutageTests(unittest.TestCase):
    def setUp(self):
        self.connection = FakeConnection()
        self.sms = Mock()
        self.db_patch = patch.object(
            module, "get_db", return_value=self.connection
        )
        self.sms_patch = patch.object(module, "send_sms", self.sms)
        self.db_patch.start()
        self.sms_patch.start()

    def tearDown(self):
        self.sms_patch.stop()
        self.db_patch.stop()

    def post_reset(self, limiter, code=""):
        limiter_patch = patch.object(module, "reset_rate_limiter", limiter)
        limiter_patch.start()
        try:
            with module.app.test_client() as client:
                return client.post(
                    "/status/reset",
                    data={
                        "mobile": "test-mobile",
                        "code": code,
                        "new_password": "new-secure-password",
                    },
                )
        finally:
            limiter_patch.stop()

    def test_request_limit_failure_is_controlled_and_sends_no_otp(self):
        response = self.post_reset(limiter_with_failure("request"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.connection.commit_count, 0)
        self.assertEqual(self.connection.rollback_count, 1)
        self.assertFalse(any("INSERT INTO otps" in s
                             for s in self.connection.statements))
        self.sms.assert_not_called()

    def test_attempt_limit_failure_is_controlled_and_changes_no_password(self):
        self.connection.otp = (
            module.reset_code_digest("123456"), datetime.now()
        )
        response = self.post_reset(
            limiter_with_failure("attempt"), code="123456"
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.connection.password_updated)
        self.assertEqual(self.connection.commit_count, 0)
        self.assertEqual(self.connection.rollback_count, 1)

    def test_valid_reset_does_not_commit_when_cleanup_fails(self):
        self.connection.otp = (
            module.reset_code_digest("123456"), datetime.now()
        )
        response = self.post_reset(
            limiter_with_failure("clear"), code="123456"
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.connection.password_updated)
        self.assertEqual(self.connection.commit_count, 0)
        self.assertEqual(self.connection.rollback_count, 1)

    def test_exceeded_attempt_cleanup_failure_does_not_delete_otp(self):
        original_otp = (module.reset_code_digest("123456"), datetime.now())
        self.connection.otp = original_otp
        response = self.post_reset(
            limiter_with_failure("exceeded-clear"), code="000000"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.connection.otp, original_otp)
        self.assertEqual(self.connection.commit_count, 0)
        self.assertEqual(self.connection.rollback_count, 1)
        self.assertFalse(any("DELETE FROM otps" in s
                             for s in self.connection.statements))

    def test_healthy_reset_preserves_commit_and_cleanup_behavior(self):
        self.connection.otp = (
            module.reset_code_digest("123456"), datetime.now()
        )
        response = self.post_reset(limiter_with_failure(), code="123456")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.connection.password_updated)
        self.assertIsNone(self.connection.otp)
        self.assertEqual(self.connection.commit_count, 1)


if __name__ == "__main__":
    unittest.main()
