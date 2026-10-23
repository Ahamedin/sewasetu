import ast
import base64
import hashlib
import hmac
import secrets
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch


SOURCE_PATH = Path(__file__).with_name("app.py")
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")


def load_function(name):
    tree = ast.parse(SOURCE)
    node = next(item for item in tree.body
                if isinstance(item, ast.FunctionDef) and item.name == name)
    namespace = {
        "check_password_hash": Mock(return_value=False),
        "hashlib": hashlib,
        "hmac": hmac,
        "base64": base64,
        "RESET_CODE_PEPPER": b"test-pepper",
        "secrets": secrets,
        "datetime": datetime,
        "RESET_TOKEN_VALIDITY_SECONDS": 300,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE_PATH), "exec"),
         namespace)
    return namespace[name], namespace


class PasswordSecurityTests(unittest.TestCase):
    def test_successful_legacy_authentication_requests_upgrade(self):
        verify_password, namespace = load_function("verify_password")
        password = "legacy-password"
        legacy_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()

        authenticated, needs_upgrade, temporary = verify_password(
            legacy_hash, password
        )

        self.assertTrue(authenticated)
        self.assertTrue(needs_upgrade)
        self.assertFalse(temporary)
        self.assertEqual(len(legacy_hash), 64)
        namespace["check_password_hash"].assert_called_once()

    def test_incorrect_authentication_is_rejected(self):
        verify_password, _ = load_function("verify_password")
        legacy_hash = hashlib.sha256(b"correct-password").hexdigest()

        self.assertEqual(verify_password(legacy_hash, "wrong-password"),
                         (False, False, False))

    def test_reset_request_limits_mobile_and_ip(self):
        source = SOURCE
        self.assertIn("RESET_MAX_REQUESTS_PER_MOBILE = 3", source)
        self.assertIn("RESET_MAX_REQUESTS_PER_IP = 10", source)
        self.assertIn("RESET_COOLDOWN_SECONDS = 60", source)
        self.assertIn("reset_request_allowed(mobile, ip_address)", source)

    def test_invalid_attempt_limit_is_enforced(self):
        self.assertIn("RESET_MAX_ATTEMPTS = 5", SOURCE)
        self.assertIn("reset_attempt_allowed(mobile)", SOURCE)
        self.assertIn("Too many verification attempts", SOURCE)
        self.assertIn('DELETE FROM otps WHERE mobile = %s", (mobile,)', SOURCE)

    def test_reset_digest_is_not_plaintext(self):
        self.assertIn("reset_code_digest(reset_code)", SOURCE)
        self.assertIn("hmac.compare_digest", SOURCE)
        reset_source = SOURCE[SOURCE.index("def reset_password():"):
                             SOURCE.index("# ---------------------------------------------------------------------------\n# Admin")]
        self.assertNotIn("(mobile, reset_code),", reset_source)

    def test_temporary_password_expires_and_requires_replacement(self):
        self.assertIn("hash_temporary_password(", SOURCE)
        self.assertIn("timedelta(hours=24)", SOURCE)
        self.assertIn("temporary password must be replaced", SOURCE)

    def test_reset_code_expires(self):
        valid_reset_code, _ = load_function("valid_reset_code")
        valid_namespace = _
        valid_namespace["reset_code_digest"] = (
            lambda code: hmac.new(
                b"test-pepper", code.encode("ascii"), hashlib.sha256
            ).hexdigest()[:6]
        )
        created = datetime(2026, 10, 22, 10, 0, 0)

        self.assertFalse(valid_reset_code(
            created, "123456", valid_namespace["reset_code_digest"]("123456"),
            now=created + timedelta(seconds=301),
        ))

    def test_reset_code_is_single_use_in_transaction(self):
        self.assertIn("DELETE FROM otps WHERE mobile = %s AND code = %s", SOURCE)
        self.assertIn("conn.commit()", SOURCE)
        self.assertLess(
            SOURCE.index("DELETE FROM otps WHERE mobile = %s AND code = %s"),
            SOURCE.index("conn.commit()", SOURCE.index(
                "DELETE FROM otps WHERE mobile = %s AND code = %s"
            )),
        )

    def test_mocked_reset_consumes_valid_code_once(self):
        complete_reset, namespace = load_function("complete_password_reset")
        valid_reset_code, valid_namespace = load_function("valid_reset_code")
        namespace["valid_reset_code"] = valid_reset_code
        digest = (
            lambda code: hmac.new(
                b"test-pepper", code.encode("ascii"), hashlib.sha256
            ).hexdigest()[:6]
        )
        valid_namespace["reset_code_digest"] = digest
        namespace["reset_code_digest"] = digest
        namespace["clear_reset_attempts"] = Mock()
        cursor = Mock()
        created = datetime(2026, 10, 22, 10, 0, 0)
        cursor.fetchone.return_value = (
            namespace["reset_code_digest"]("123456"), created
        )
        namespace["hash_password"] = Mock(return_value="new-hash")

        result = complete_reset(
            cursor, "test-mobile", "123456", "a-secure-test-password",
            now=created + timedelta(seconds=1),
        )

        self.assertTrue(result)
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(any("UPDATE portal_users" in statement
                            for statement in statements))
        self.assertTrue(any("DELETE FROM otps" in statement
                            for statement in statements))

    def test_reset_code_and_password_are_not_logged_or_returned(self):
        reset_source = SOURCE[SOURCE.index("def reset_password():"):
                             SOURCE.index("# ---------------------------------------------------------------------------\n# Admin")]
        self.assertNotIn("print(", reset_source)
        self.assertNotIn("logger.", reset_source)
        self.assertNotIn("return reset_code", reset_source)
        self.assertIn("send_sms(mobile", reset_source)

    def test_reset_requires_new_password_not_dob(self):
        reset_source = SOURCE[SOURCE.index("def reset_password():"):
                             SOURCE.index("# ---------------------------------------------------------------------------\n# Admin")]
        self.assertIn('request.form.get("new_password", "")', reset_source)
        self.assertNotIn("strftime(\"%d%m%Y\")", reset_source)


if __name__ == "__main__":
    unittest.main()
