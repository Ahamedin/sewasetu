import ast
import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class CredentialSecurityTests(unittest.TestCase):
    def test_runtime_secrets_are_environment_backed(self):
        source = (ROOT / "app" / "app.py").read_text(encoding="utf-8")
        self.assertIn('os.environ["SEWASETU_FLASK_SECRET_KEY"]', source)
        self.assertIn('os.environ["SEWASETU_DB_PASSWORD"]', source)
        self.assertIn('os.environ["SEWASETU_ADMIN_PASSWORD_HASH"]', source)
        self.assertNotIn("ADMIN_PASSWORD =", source)

    def test_scheduler_database_password_is_environment_backed(self):
        source = (ROOT / "app" / "scripts" / "deemed_approval.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('os.environ["SEWASETU_DB_PASSWORD"]', source)
        self.assertNotIn('"database", "password"', source)

    def test_config_and_compose_contain_no_literal_secret_values(self):
        config = (ROOT / "app" / "config" / "app.ini").read_text(
            encoding="utf-8"
        )
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertNotIn("password =", config)
        self.assertNotIn("secret_key =", config)
        self.assertIn("${SEWASETU_DB_PASSWORD:?", compose)
        self.assertIn("${SEWASETU_FLASK_SECRET_KEY:?", compose)
        self.assertIn("${SEWASETU_ADMIN_PASSWORD_HASH:?", compose)

    def test_application_source_remains_valid_python(self):
        ast.parse((ROOT / "app" / "app.py").read_text(encoding="utf-8"))
        ast.parse(
            (ROOT / "app" / "scripts" / "deemed_approval.py").read_text(
                encoding="utf-8"
            )
        )

    def test_missing_secret_environment_is_not_silently_defaulted(self):
        for name in (
            "SEWASETU_DB_PASSWORD",
            "SEWASETU_FLASK_SECRET_KEY",
            "SEWASETU_ADMIN_PASSWORD_HASH",
        ):
            self.assertNotIn(name, os.environ)

    def test_admin_route_uses_hash_verification(self):
        source = (ROOT / "app" / "app.py").read_text(encoding="utf-8")
        self.assertIn("check_password_hash(", source)
        self.assertNotIn('request.form.get("password") == ADMIN_PASSWORD', source)


if __name__ == "__main__":
    unittest.main()
