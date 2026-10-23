import werkzeug
if not hasattr(werkzeug, "__version__"):
    werkzeug.__version__ = "3.1.9"

import app as portal
from unittest.mock import patch

client = portal.app.test_client()

# Simulate a logged-in citizen, not an administrator.
with client.session_transaction() as session:
    session["logged_in"] = True
    session["portal_mobile"] = "TEST_A"
    session.pop("admin", None)

tests = [
    ("Citizen attempts to approve an application",
     "/admin/approve/101"),
    ("Citizen attempts to reject an application",
     "/admin/reject/101"),
]

failures = 0

# Fail the test if either request reaches the database.
with patch.object(
    portal, "get_db",
    side_effect=AssertionError("Unauthorized request reached database")
):
    for name, path in tests:
        response = client.post(path)
        passed = response.status_code == 403

        print(
            f'{"PASS" if passed else "FAIL"}: {name} '
            f'(expected 403, got {response.status_code})'
        )

        if not passed:
            failures += 1

print(f"\nResult: {len(tests) - failures}/{len(tests)} tests passed.")

if failures:
    raise SystemExit(1)
