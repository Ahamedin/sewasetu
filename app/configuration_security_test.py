import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


APP_PATH = Path(__file__).with_name("app.py")


def load_app(overrides=None):
    environment = {
        "SEWASETU_DB_PASSWORD": "test-db",
        "SEWASETU_FLASK_SECRET_KEY": "test-secret",
        "SEWASETU_ADMIN_USERNAME": "test-admin",
        "SEWASETU_ADMIN_PASSWORD_HASH": "test-hash",
        "SEWASETU_RESET_CODE_PEPPER": "test-pepper",
        "SEWASETU_REDIS_URL": "redis://test.invalid/0",
        "SEWASETU_SESSION_COOKIE_SECURE": "true",
        "SEWASETU_SESSION_COOKIE_HTTPONLY": "true",
        "SEWASETU_SESSION_COOKIE_SAMESITE": "Lax",
        "SEWASETU_ALLOWED_HOSTS": "portal.test,localhost",
        "SEWASETU_TRUSTED_PROXY_COUNT": "0",
    }
    if overrides:
        environment.update(overrides)

    requests_stub = types.ModuleType("requests")
    requests_stub.post = Mock()
    psycopg2_stub = types.ModuleType("psycopg2")
    psycopg2_stub.connect = Mock()
    fpdf_stub = types.ModuleType("fpdf")
    fpdf_stub.FPDF = Mock
    redis_stub = types.ModuleType("redis")
    redis_stub.Redis = Mock()
    redis_stub.Redis.from_url.return_value = Mock()

    module_name = "configuration_security_app"
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
    return module


class ConfigurationSecurityTests(unittest.TestCase):
    def test_secure_cookie_configuration(self):
        module = load_app()
        self.assertTrue(module.app.config["SESSION_COOKIE_SECURE"])
        self.assertTrue(module.app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(module.app.config["SESSION_COOKIE_SAMESITE"], "Lax")

    def test_samesite_none_requires_secure_cookies(self):
        with self.assertRaisesRegex(RuntimeError, "SESSION_COOKIE_SAMESITE=None"):
            load_app({
                "SEWASETU_SESSION_COOKIE_SECURE": "false",
                "SEWASETU_SESSION_COOKIE_SAMESITE": "None",
            })

    def test_disallowed_host_is_rejected(self):
        module = load_app()
        response = module.app.test_client().get(
            "/", headers={"Host": "attacker.test"}
        )
        self.assertEqual(response.status_code, 400)

    def test_allowed_host_is_accepted(self):
        module = load_app()
        response = module.app.test_client().get(
            "/", headers={"Host": "portal.test"}
        )
        self.assertEqual(response.status_code, 200)

    def test_forwarded_headers_are_ignored_without_trusted_proxy(self):
        module = load_app()
        request_seen = {}

        @module.app.route("/test-forwarded")
        def test_forwarded():
            request_seen["scheme"] = module.request.scheme
            request_seen["host"] = module.request.host
            request_seen["remote_addr"] = module.request.remote_addr
            return "ok"

        response = module.app.test_client().get(
            "/test-forwarded",
            headers={
                "Host": "portal.test",
                "X-Forwarded-For": "198.51.100.10",
                "X-Forwarded-Host": "forwarded.test",
                "X-Forwarded-Proto": "https",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(request_seen["scheme"], "http")
        self.assertEqual(request_seen["host"], "portal.test")
        self.assertNotEqual(request_seen["remote_addr"], "198.51.100.10")

    def test_proxy_fix_requires_positive_explicit_count(self):
        module = load_app({"SEWASETU_TRUSTED_PROXY_COUNT": "1"})
        self.assertEqual(
            module.app.wsgi_app.__class__.__name__, "ProxyFix"
        )

    def test_invalid_environment_values_fail_at_startup(self):
        with self.assertRaisesRegex(RuntimeError, "must be true or false"):
            load_app({"SEWASETU_SESSION_COOKIE_SECURE": "yes"})
        with self.assertRaisesRegex(RuntimeError, "non-negative integer"):
            load_app({"SEWASETU_TRUSTED_PROXY_COUNT": "-1"})
        with self.assertRaisesRegex(RuntimeError, "must be Lax, Strict, or None"):
            load_app({"SEWASETU_SESSION_COOKIE_SAMESITE": "CrossSite"})


if __name__ == "__main__":
    unittest.main()
