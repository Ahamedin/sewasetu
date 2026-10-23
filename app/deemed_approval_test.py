import configparser
import sys
import types
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

if "psycopg2" not in sys.modules:
    sys.modules["psycopg2"] = types.SimpleNamespace(connect=Mock())
if "requests" not in sys.modules:
    sys.modules["requests"] = types.SimpleNamespace(
        RequestException=Exception, post=Mock()
    )

from scripts import deemed_approval


class FakeCursor:
    def __init__(self, connection, count=2, rows=None, error=None):
        self.connection = connection
        self.count = count
        self.rows = rows or []
        self.error = error
        self.statements = []
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, statement, parameters=()):
        self.statements.append((statement, parameters))
        if self.error:
            raise self.error
        if "CURRENT_TIMESTAMP" in statement:
            self.result = (datetime(2026, 10, 22, 5, 12, 46),)
        elif "SELECT COUNT" in statement:
            self.result = (self.count,)
        else:
            self.result = self.rows

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.result


class FakeConnection:
    def __init__(self, cursor):
        self.cursor_instance = cursor
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.session_options = None

    def cursor(self):
        return self.cursor_instance

    def set_session(self, **options):
        self.session_options = options

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def test_config():
    config = configparser.ConfigParser()
    config.read_dict(
        {
            "database": {
                "host": "db",
                "port": "5432",
                "name": "test",
                "user": "test",
                "password": "test",
            },
            "app": {"sms_gateway_url": "http://gateway"},
            "pension": {"sla_days": "15"},
        }
    )
    return config


class DeemedApprovalTests(unittest.TestCase):
    def test_dry_run_is_read_only_and_sends_no_sms(self):
        cursor = FakeCursor(None, count=3)
        connection = FakeConnection(cursor)
        cursor.connection = connection

        with patch.object(
            deemed_approval, "connect",
            side_effect=lambda config, readonly: (
                connection.set_session(readonly=readonly, autocommit=False)
                or connection
            ),
        ), \
                patch.object(deemed_approval.requests, "post") as post:
            result = deemed_approval.run(test_config(), 15, False)

        self.assertEqual(result, 0)
        self.assertEqual(connection.commits, 0)
        self.assertTrue(connection.session_options["readonly"])
        self.assertFalse(any("UPDATE" in statement for statement, _ in cursor.statements))
        post.assert_not_called()

    def test_apply_is_explicit_and_only_pending_rows_are_updated(self):
        cursor = FakeCursor(None, rows=[("application-1", "mobile-1")])
        connection = FakeConnection(cursor)
        cursor.connection = connection
        response = Mock()
        response.raise_for_status.return_value = None

        with patch.object(
            deemed_approval, "connect",
            side_effect=lambda config, readonly: (
                connection.set_session(readonly=readonly, autocommit=False)
                or connection
            ),
        ), \
                patch.object(deemed_approval.requests, "post", return_value=response) as post:
            result = deemed_approval.run(test_config(), 15, True)

        self.assertEqual(result, 0)
        self.assertEqual(connection.commits, 1)
        self.assertFalse(connection.session_options["readonly"])
        update = next(statement for statement, _ in cursor.statements
                      if "UPDATE" in statement)
        self.assertIn("status = 'PENDING'", update)
        post.assert_called_once()

    def test_notifications_are_sent_only_after_commit(self):
        cursor = FakeCursor(None, rows=[("application-1", "mobile-1")])
        connection = FakeConnection(cursor)
        cursor.connection = connection
        response = Mock()
        response.raise_for_status.return_value = None
        commit_count_at_notification = []

        def post(*args, **kwargs):
            commit_count_at_notification.append(connection.commits)
            return response

        with patch.object(
            deemed_approval, "connect",
            side_effect=lambda config, readonly: (
                connection.set_session(readonly=readonly, autocommit=False)
                or connection
            ),
        ), \
                patch.object(deemed_approval.requests, "post", side_effect=post):
            deemed_approval.run(test_config(), 15, True)

        self.assertEqual(commit_count_at_notification, [1])

    def test_database_error_rolls_back(self):
        cursor = FakeCursor(None, error=RuntimeError("database failure"))
        connection = FakeConnection(cursor)
        cursor.connection = connection

        with patch.object(
            deemed_approval, "connect",
            side_effect=lambda config, readonly: (
                connection.set_session(readonly=readonly, autocommit=False)
                or connection
            ),
        ):
            with self.assertRaises(RuntimeError):
                deemed_approval.run(test_config(), 15, True)

        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)

    def test_missing_configuration_fails_without_connecting(self):
        with patch.object(
            deemed_approval, "load_config",
            side_effect=configparser.NoSectionError("database"),
        ), patch.object(deemed_approval, "connect") as connect:
            result = deemed_approval.main([])

        self.assertEqual(result, 2)
        connect.assert_not_called()

    def test_database_connection_failure_fails_safely(self):
        with patch.object(
            deemed_approval, "load_config", return_value=(test_config(), 15)
        ), patch.object(
            deemed_approval, "connect", side_effect=RuntimeError("database failure")
        ):
            result = deemed_approval.main([])

        self.assertEqual(result, 2)


if __name__ == "__main__":
    unittest.main()
