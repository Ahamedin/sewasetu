import werkzeug
if not hasattr(werkzeug, "__version__"):
    werkzeug.__version__ = "3.1.9"

import app as portal
from datetime import datetime
from unittest.mock import patch

TEST_APPS = {
    101: ("SSP-TEST-A", "Test Citizen A", "TEST_A", None,
          "Test Village", "Test Block", "TEST_ACCOUNT", "TEST_IFSC",
          "PENDING", datetime.now(), None),
    202: ("SSP-TEST-B", "Test Citizen B", "TEST_B", None,
          "Test Village", "Test Block", "TEST_ACCOUNT", "TEST_IFSC",
          "PENDING", datetime.now(), None),
}

class FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, query, params=None):
        self.query = query
        self.params = params

    def fetchone(self):
        app_id = self.params[0]
        row = TEST_APPS.get(app_id)

        if "SELECT application_no" in self.query:
            return row

        if "SELECT mobile FROM applications" in self.query:
            return (row[2],) if row else None

        return None

    def close(self):
        pass

class FakeConnection:
    def cursor(self):
        return FakeCursor()

    def close(self):
        pass

client = portal.app.test_client()

with patch.object(portal, "get_db", return_value=FakeConnection()), \
     patch.object(portal.os.path, "exists", return_value=False), \
     patch.object(portal, "generate_acknowledgment", return_value=b"test PDF"):

    with client.session_transaction() as session:
        session["logged_in"] = True
        session["portal_mobile"] = "TEST_A"

    tests = [
        ("Citizen A views own application", "/application/101", 200),
        ("Citizen A views Citizen B application", "/application/202", 404),
        ("Citizen A requests own PDF", "/ack/101.pdf", 200),
        ("Citizen A requests Citizen B PDF", "/ack/202.pdf", 404),
    ]

    failures = 0
    for name, path, expected in tests:
        response = client.get(path)
        passed = response.status_code == expected
        print(f'{"PASS" if passed else "FAIL"}: {name} '
              f'(expected {expected}, got {response.status_code})')
        if not passed:
            failures += 1

    print(f"\nResult: {len(tests) - failures}/{len(tests)} tests passed.")
    if failures:
        raise SystemExit(1)
