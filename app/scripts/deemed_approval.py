import argparse
import configparser
import os
import sys
from datetime import datetime, timedelta

import psycopg2
import requests


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config", "app.ini")
MAX_SLA_DAYS = 3650


def load_config():
    config = configparser.ConfigParser()
    with open(CONFIG_PATH, encoding="utf-8") as file:
        config.read_file(file)

    required = {
        "database": ("host", "port", "name", "user"),
        "app": ("sms_gateway_url",),
        "pension": ("sla_days",),
    }
    for section, options in required.items():
        if not config.has_section(section):
            raise ValueError("missing configuration section: %s" % section)
        for option in options:
            if not config.has_option(section, option):
                raise ValueError("missing configuration option: %s.%s"
                                 % (section, option))

    sla_days = config.getint("pension", "sla_days")
    if not 0 < sla_days <= MAX_SLA_DAYS:
        raise ValueError("SLA must be between 1 and %d days" % MAX_SLA_DAYS)
    if not config.get("app", "sms_gateway_url").strip():
        raise ValueError("SMS gateway URL must not be empty")
    return config, sla_days


def connect(config, readonly):
    conn = psycopg2.connect(
        host=config.get("database", "host"),
        port=config.get("database", "port"),
        dbname=config.get("database", "name"),
        user=config.get("database", "user"),
        password=os.environ["SEWASETU_DB_PASSWORD"],
        connect_timeout=5,
        application_name="sewasetu-deemed-approval",
    )
    conn.set_session(readonly=readonly, autocommit=False)
    return conn


def run(config, sla_days, apply_changes):
    conn = connect(config, readonly=not apply_changes)
    notifications = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT CURRENT_TIMESTAMP::timestamp")
            database_now = cur.fetchone()[0]
            cutoff = database_now - timedelta(days=sla_days)

            if not apply_changes:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM applications
                    WHERE status = 'PENDING'
                      AND submitted_at < %s
                    """,
                    (cutoff,),
                )
                eligible_count = cur.fetchone()[0]
                print(
                    "DRY RUN: %d PENDING applications meet the configured "
                    "%d-day SLA. No records changed."
                    % (eligible_count, sla_days)
                )
                return 0

            cur.execute(
                """
                WITH eligible AS (
                    SELECT id
                    FROM applications
                    WHERE status = 'PENDING'
                      AND submitted_at < %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE applications AS applications
                SET status = 'DEEMED_APPROVED',
                    decided_at = %s,
                    decided_by = 'RTPS-AUTO'
                FROM eligible
                WHERE applications.id = eligible.id
                  AND applications.status = 'PENDING'
                RETURNING applications.application_no, applications.mobile
                """,
                (cutoff, database_now),
            )
            notifications = cur.fetchall()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if not apply_changes:
            conn.rollback()
        conn.close()

    print("Committed %d deemed approvals." % len(notifications))
    return notify(config, notifications)


def notify(config, applications):
    gateway = config.get("app", "sms_gateway_url").rstrip("/")
    failures = 0
    for app_no, mobile in applications:
        try:
            response = requests.post(
                gateway + "/api/send",
                json={
                    "to": mobile,
                    "text": (
                        "Sewa Setu: application %s stands deemed approved "
                        "under the configured SLA." % app_no
                    ),
                },
                timeout=(3, 5),
            )
            response.raise_for_status()
        except requests.RequestException:
            failures += 1

    print(
        "Notifications: %d failed; %d succeeded."
        % (failures, len(applications) - failures)
    )
    return 1 if failures else 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Sewa Setu deemed-approval scheduler"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply eligible status changes. Without this flag, only preview.",
    )
    args = parser.parse_args(argv)

    try:
        config, sla_days = load_config()
        return run(config, sla_days, args.apply)
    except Exception as exc:
        print(
            "Scheduler failed safely: %s" % type(exc).__name__,
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
