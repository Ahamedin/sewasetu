# -*- coding: utf-8 -*-
"""
Sewa Setu - Old Age Pension Portal
Government of Purvanchal, Department of Social Welfare

Developed by: Netlink Infosolutions Pvt Ltd (2023)
Maintenance contract ended 31/01/2026.
"""

import os
import io
import csv
import random
import hashlib
import hmac
import base64
import secrets
import threading
import time
import configparser
from datetime import datetime, date, timedelta

import requests
import psycopg2
from werkzeug.security import check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from flask import (Flask, request, session, redirect, url_for, render_template,
                   flash, send_file, abort, jsonify)
from flask_wtf.csrf import CSRFProtect
from fpdf import FPDF

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


config = configparser.ConfigParser()
config.read(os.path.join(BASE_DIR, "config", "app.ini"))

# Allow deployment environment variables to override local configuration.
for option, env_name in (
    ("host", "SEWASETU_DB_HOST"),
    ("port", "SEWASETU_DB_PORT"),
    ("name", "SEWASETU_DB_NAME"),
    ("user", "SEWASETU_DB_USER"),
):
    value = os.environ.get(env_name)
    if value:
        config.set("database", option, value)



def env_bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized not in ("true", "false"):
        raise RuntimeError("%s must be true or false" % name)
    return normalized == "true"


def env_nonnegative_int(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError("%s must be a non-negative integer" % name) from exc
    if parsed < 0:
        raise RuntimeError("%s must be a non-negative integer" % name)
    return parsed


def env_positive_int(name, default):
    parsed = env_nonnegative_int(name, default)
    if parsed == 0:
        raise RuntimeError("%s must be a positive integer" % name)
    return parsed


app = Flask(__name__)
app.secret_key = os.environ["SEWASETU_FLASK_SECRET_KEY"]
csrf = CSRFProtect(app)
app.config["PERMANENT_SESSION_LIFETIME"] = env_positive_int(
    "SEWASETU_SESSION_LIFETIME_SECONDS", 1800
)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True
app.config["SESSION_COOKIE_SECURE"] = env_bool(
    "SEWASETU_SESSION_COOKIE_SECURE", False
)
app.config["SESSION_COOKIE_HTTPONLY"] = env_bool(
    "SEWASETU_SESSION_COOKIE_HTTPONLY", True
)
session_cookie_samesite = os.environ.get(
    "SEWASETU_SESSION_COOKIE_SAMESITE", "Lax"
).strip().capitalize()
if session_cookie_samesite not in ("Lax", "Strict", "None"):
    raise RuntimeError(
        "SEWASETU_SESSION_COOKIE_SAMESITE must be Lax, Strict, or None"
    )
if session_cookie_samesite == "None" and not app.config["SESSION_COOKIE_SECURE"]:
    raise RuntimeError(
        "SESSION_COOKIE_SAMESITE=None requires SESSION_COOKIE_SECURE=true"
    )
app.config["SESSION_COOKIE_SAMESITE"] = session_cookie_samesite

allowed_hosts = tuple(
    host.strip().lower()
    for host in os.environ.get("SEWASETU_ALLOWED_HOSTS", "").split(",")
    if host.strip()
)
trusted_proxy_count = env_nonnegative_int("SEWASETU_TRUSTED_PROXY_COUNT", 0)
if trusted_proxy_count:
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=trusted_proxy_count,
        x_proto=trusted_proxy_count,
        x_host=trusted_proxy_count,
        x_port=trusted_proxy_count,
        x_prefix=trusted_proxy_count,
    )


@app.before_request
def enforce_allowed_host():
    if not allowed_hosts:
        return
    host = request.host.rsplit(":", 1)[0].lower()
    if host not in allowed_hosts:
        abort(400)

ADMIN_USERNAME = os.environ["SEWASETU_ADMIN_USERNAME"]
ADMIN_PASSWORD_HASH = os.environ["SEWASETU_ADMIN_PASSWORD_HASH"]

SMS_GATEWAY_URL = os.environ.get(
    "SEWASETU_SMS_GATEWAY_URL",
    config.get("app", "sms_gateway_url"),
).rstrip("/")
OTP_VALIDITY_SECONDS = config.getint("app", "otp_validity_seconds")
UPLOAD_DIR = config.get("app", "upload_dir")
MAX_DOCUMENT_SIZE = 5 * 1024 * 1024
SCHEME_DEADLINE = datetime.strptime(config.get("pension", "scheme_deadline"),
                                    "%Y-%m-%d %H:%M")
MIN_AGE = config.getint("pension", "min_age")
SLA_DAYS = config.getint("pension", "sla_days")
DB_PASSWORD = os.environ["SEWASETU_DB_PASSWORD"]

BLOCKS = ["Sonari", "Rajapara", "Dhemaji Pathar", "Borgaon", "Namti", "Khelua"]
APPLICATION_SUMMARY_COLUMNS = (
    "application_no, applicant_name, mobile, dob, village, block, "
    "bank_account, ifsc, submitted_at, status, decided_at"
)

import logging
_logdir = "/var/log/sewasetu"
try:
    os.makedirs(_logdir, exist_ok=True)
    _fh = logging.FileHandler(os.path.join(_logdir, "sewasetu-app.log"))
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s in app: %(message)s"))
    app.logger.addHandler(_fh)
    app.logger.setLevel(logging.INFO)
except Exception:
    pass


def get_db():
    return psycopg2.connect(
        host=config.get("database", "host"),
        port=config.get("database", "port"),
        dbname=config.get("database", "name"),
        user=config.get("database", "user"),
        password=os.environ["SEWASETU_DB_PASSWORD"],
    )


def sanitize(value, maxlen=100):
    if value is None:
        return ""
    return value.strip()[:maxlen]


def validate_document_upload(upload):
    filename = (upload.filename or "").strip()
    if not filename:
        return None, None, "Please select an age-proof document."

    extension = os.path.splitext(filename)[1].lower()
    expected_types = {
        ".pdf": ("application/pdf", b"%PDF-"),
        ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
        ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
    }
    expected = expected_types.get(extension)
    if expected is None:
        return None, None, "Only JPG and PDF files are accepted."

    content = upload.read(MAX_DOCUMENT_SIZE + 1)
    if len(content) > MAX_DOCUMENT_SIZE:
        return None, None, "Document size must not exceed 5 MB."

    expected_mimetype, signature = expected
    declared_mimetype = (upload.mimetype or "").lower()
    if declared_mimetype and declared_mimetype != expected_mimetype:
        return None, None, "The document file type is invalid."
    if not content.startswith(signature):
        return None, None, "The document content does not match its file type."

    return content, extension, None


def redact_mobile(mobile):
    mobile = str(mobile or "")
    if len(mobile) <= 2:
        return "***"
    return "***" + mobile[-2:]



RESET_TOKEN_VALIDITY_SECONDS = 300
RESET_MAX_ATTEMPTS = 5
RESET_COOLDOWN_SECONDS = 60
RESET_WINDOW_SECONDS = 900
RESET_MAX_REQUESTS_PER_MOBILE = 3
RESET_MAX_REQUESTS_PER_IP = 10

RESET_CODE_PEPPER = os.environ[
    "SEWASETU_RESET_CODE_PEPPER"
].encode("utf-8")

REDIS_URL = os.environ.get("SEWASETU_REDIS_URL", "").strip()
if not REDIS_URL:
    raise RuntimeError("SEWASETU_REDIS_URL must be configured")

try:
    import redis
except ImportError as exc:
    raise RuntimeError("Redis client dependency is required") from exc


class RateLimiterUnavailable(Exception):
    """Raised when the Redis-backed reset rate limiter is unavailable."""


class ResetRateLimiter:
    _REQUEST_SCRIPT = """
    for _, key in ipairs(KEYS) do
        if redis.call('EXISTS', key .. ':cooldown') == 1 then
            return 0
        end
    end

    for index, key in ipairs(KEYS) do
        local count = redis.call('INCR', key .. ':count')
        if count == 1 then
            redis.call('EXPIRE', key .. ':count', ARGV[1])
        end

        if count > tonumber(ARGV[index + 2]) then
            for rollback = 1, index do
                redis.call('DECR', KEYS[rollback] .. ':count')
            end
            return 0
        end
    end

    for _, key in ipairs(KEYS) do
        redis.call('SET', key .. ':cooldown', '1', 'EX', ARGV[2])
    end

    return 1
    """

    def __init__(self, client):
        self.client = client

    @staticmethod
    def _key(kind, value):
        digest = hashlib.sha256(
            value.encode("utf-8")
        ).hexdigest()
        return "sewasetu:reset:%s:%s" % (kind, digest)

    def _run_redis(self, operation, *args):
        try:
            return operation(*args)
        except redis.RedisError as exc:
            # Never log Redis URLs, credentials, OTPs, or passwords.
            app.logger.error(
                "redis rate limiter unavailable operation=%s type=%s",
                operation.__name__,
                type(exc).__name__,
            )
            raise RateLimiterUnavailable from exc

    def check_connection(self):
        return self._run_redis(self.client.ping)

    def request_allowed(self, mobile, ip_address):
        result = self._run_redis(
            self.client.eval,
            self._REQUEST_SCRIPT,
            2,
            self._key("mobile", mobile),
            self._key("ip", ip_address),
            RESET_WINDOW_SECONDS,
            RESET_COOLDOWN_SECONDS,
            RESET_MAX_REQUESTS_PER_MOBILE,
            RESET_MAX_REQUESTS_PER_IP,
        )
        return bool(result)

    def attempt_allowed(self, mobile):
        key = self._key("attempt", mobile) + ":count"

        count = self._run_redis(self.client.incr, key)
        if count == 1:
            self._run_redis(
                self.client.expire,
                key,
                RESET_TOKEN_VALIDITY_SECONDS,
            )

        return count <= RESET_MAX_ATTEMPTS

    def clear_attempts(self, mobile):
        key = self._key("attempt", mobile) + ":count"
        self._run_redis(self.client.delete, key)


redis_client = redis.Redis.from_url(
    REDIS_URL,
    socket_connect_timeout=5,
    socket_timeout=5,
    health_check_interval=30,
    decode_responses=True,
)

reset_rate_limiter = ResetRateLimiter(redis_client)


def hash_password(password):
    salt = secrets.token_bytes(12)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, 600000
    )
    return "p2$%s$%s" % (
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def hash_temporary_password(password, expires_at):
    return "t$%d$%s" % (int(expires_at.timestamp()), hash_password(password))


def password_hash_is_temporary(stored_hash):
    if not stored_hash or not stored_hash.startswith("t$"):
        return False
    try:
        return datetime.now().timestamp() >= int(stored_hash.split("$", 2)[1])
    except (IndexError, TypeError, ValueError):
        return True


def check_compact_password_hash(stored_hash, password):
    try:
        _, salt_text, digest_text = stored_hash.split("$", 2)
        salt = base64.b64decode(salt_text.encode("ascii"))
        expected = base64.b64decode(digest_text.encode("ascii"))
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, 600000
    )
    return hmac.compare_digest(actual, expected)


def verify_password(stored_hash, password):
    if not stored_hash:
        return False, False, False
    temporary = stored_hash.startswith("t$")
    if stored_hash.startswith("t$"):
        parts = stored_hash.split("$", 2)
        modern_match = (
            len(parts) == 3 and
            not password_hash_is_temporary(stored_hash) and
            check_compact_password_hash(parts[2], password)
        )
    elif stored_hash.startswith("p2$"):
        modern_match = check_compact_password_hash(stored_hash, password)
    else:
        try:
            modern_match = check_password_hash(stored_hash, password)
        except ValueError:
            modern_match = False
    if modern_match:
        return True, False, temporary
    if temporary:
        return False, False, True

    # Upgrade only the legacy deterministic SHA-256 format after a valid login.
    if (len(stored_hash) == 64 and
            all(char in "0123456789abcdef" for char in stored_hash.lower()) and
            secrets.compare_digest(
                stored_hash, hashlib.sha256(password.encode("utf-8")).hexdigest()
            )):
        return True, True, False
    return False, False, False


def new_reset_code():
    return "%06d" % secrets.randbelow(1000000)


def reset_code_digest(code):
    return hmac.new(
        RESET_CODE_PEPPER, code.encode("ascii"), hashlib.sha256
    ).hexdigest()[:6]


def reset_request_allowed(mobile, ip_address, now=None):
    del now
    return reset_rate_limiter.request_allowed(mobile, ip_address)


def reset_attempt_allowed(mobile, now=None):
    del now
    return reset_rate_limiter.attempt_allowed(mobile)


def clear_reset_attempts(mobile):
    reset_rate_limiter.clear_attempts(mobile)


def valid_reset_code(created_at, submitted_code, expected_code, now=None):
    if not submitted_code or not expected_code:
        return False
    if not hmac.compare_digest(
            reset_code_digest(submitted_code), expected_code):
        return False
    now = now or datetime.now()
    age = (now - created_at).total_seconds()
    return 0 <= age <= RESET_TOKEN_VALIDITY_SECONDS


def complete_password_reset(cur, mobile, code, new_password, now=None):
    if len(new_password) < 12:
        return False
    cur.execute(
        "SELECT code, created_at FROM otps WHERE mobile = %s "
        "ORDER BY id DESC LIMIT 1 FOR UPDATE",
        (mobile,),
    )
    token = cur.fetchone()
    if not token or not valid_reset_code(token[1], code, token[0], now=now):
        return False
    cur.execute(
        "UPDATE portal_users SET password_hash = %s WHERE mobile = %s",
        (hash_password(new_password), mobile),
    )
    cur.execute(
        "DELETE FROM otps WHERE mobile = %s AND code = %s",
        (mobile, reset_code_digest(code)),
    )
    clear_reset_attempts(mobile)
    return True


def send_sms(mobile, text):
    token = os.environ.get("SEWASETU_SMS_GATEWAY_TOKEN", "")
    if not token:
        app.logger.error("SMS gateway token is not configured")
        return

    try:
        app.logger.info("SMS gateway URL configured: %s", SMS_GATEWAY_URL)

        response = requests.post(
            SMS_GATEWAY_URL + "/api/send",
            json={"to": mobile, "text": text},
            headers={"X-Gateway-Token": token},
            timeout=15,
        )
        app.logger.info("SMS gateway HTTP status: %s", response.status_code)
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "")
            app.logger.warning(
                "SMS gateway rate limit reached retry_after_present=%s",
                bool(retry_after),
            )
            return
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        app.logger.error(
            "SMS gateway request failed: %s; mobile=%s",
            str(exc)[:300],
            redact_mobile(mobile),
        )


def deadline_remaining():
    delta = SCHEME_DEADLINE - datetime.now()
    if delta.total_seconds() <= 0:
        return None
    return int(delta.total_seconds() // 3600)


def new_application_no():
    return "SSP" + datetime.now().strftime("%y") + str(random.randint(100000, 999999))


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", hours_left=deadline_remaining())


@app.route("/about")
def about():
    return render_template("about.html")




# ---------------------------------------------------------------------------
# Application flow: mobile -> OTP -> form steps -> upload -> declaration
# ---------------------------------------------------------------------------

@app.route("/apply", methods=["GET", "POST"])
def apply():
    if deadline_remaining() is None:
        flash("The application window for this scheme has closed.")
        return redirect(url_for("index"))
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        captcha = request.form.get("captcha", "")
        if len(mobile) != 10 or not mobile.isdigit():
            flash("Something went wrong. Please try again.")
            return render_template("apply.html", captcha_q=make_captcha())
        code = str(random.randint(100000, 999999))
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO otps (mobile, code, created_at) VALUES (%s, %s, %s)",
                    (mobile, code, datetime.now()))
        conn.commit()
        cur.close(); conn.close()
        send_sms(mobile, "Your Sewa Setu OTP is %s. Valid for 5 minutes." % code)
        session.permanent = True
        session["apply_mobile"] = mobile
        return redirect(url_for("verify"))
    return render_template("apply.html", captcha_q=make_captcha())


def make_captcha():
    a, b = random.randint(1, 9), random.randint(1, 9)
    session["captcha_answer"] = a + b
    return "%d + %d" % (a, b)


def husband_fields_required(gender, marital_status):
    return (
        gender == "Female" and
        marital_status in ("Married", "Widowed")
    )


def normalize_husband_fields(data):
    if not husband_fields_required(
            data.get("gender"), data.get("marital_status")):
        data["husband_name"] = ""
        data["husband_employer"] = ""
    return data


def application_validation_redirect(reason, step):
    app.logger.info(
        "application validation failed stage=final_submission reason=%s step=%d",
        reason,
        step,
    )
    flash("Please review the information requested on this step.")
    return redirect(url_for("form_step", step=step))


def parse_date_of_birth(value):
    if not value:
        return None

    value = value.strip()

    for date_format in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue

    return None


def describe_date_input(value, parsed):
    value = (value or "").strip()
    if not value:
        shape = "empty"
    elif len(value) == 10 and value[2] == "/" and value[5] == "/":
        shape = "dd_mm_yyyy"
    elif len(value) == 10 and value[2] == "-" and value[5] == "-":
        shape = "yyyy_mm_dd_or_other"
    elif len(value) == 10 and value[4] == "-" and value[7] == "-":
        shape = "yyyy_mm_dd"
    else:
        shape = "unexpected"
    return shape, "valid" if parsed is not None else "invalid"


@app.route("/verify", methods=["GET", "POST"])
def verify():
    mobile = session.get("apply_mobile")
    if not mobile:
        return redirect(url_for("apply"))
    if request.method == "POST":
        code = request.form.get("otp", "").strip()
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT code, created_at FROM otps WHERE mobile = %s "
                    "ORDER BY id DESC LIMIT 1", (mobile,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row[0] == code:
            age = (datetime.now() - row[1]).total_seconds()
            if age > OTP_VALIDITY_SECONDS:
                app.logger.warning(
                    "otp expired mobile=%s age=%ds",
                    redact_mobile(mobile),
                    int(age),
                )
                flash("OTP expired. Please request a new OTP.")
                return redirect(url_for("apply"))
            session["verified_mobile"] = mobile
            return redirect(url_for("form_step", step=1))
        flash("Invalid OTP.")
    return render_template("verify.html", mobile=mobile)


@app.route("/form/<int:step>", methods=["GET", "POST"])
def form_step(step):
    if not session.get("verified_mobile"):
        app.logger.info(
            "workflow session unavailable route=form_step step=%d",
            step,
        )
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if step not in (1, 2, 3):
        abort(404)
    if request.method == "POST":
        data = session.get("form_data", {})
        for k, v in request.form.items():
            data[k] = v
        if step == 1:
            normalize_husband_fields(data)
        session["form_data"] = data
        if step < 3:
            return redirect(url_for("form_step", step=step + 1))
        return redirect(url_for("upload"))
    return render_template("form_step%d.html" % step,
                           data=session.get("form_data", {}), blocks=BLOCKS)


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if request.method == "POST":
        f = request.files.get("document")
        if f is None:
            flash("Please select an age-proof document.")
            return render_template("upload.html")
        try:
            content, extension, validation_error = validate_document_upload(f)
        except (OSError, ValueError) as exc:
            app.logger.warning(
                "document upload validation failed type=%s",
                type(exc).__name__,
            )
            flash("Unable to validate the document. Please try again.")
            return render_template("upload.html")
        if validation_error:
            app.logger.info(
                "document upload rejected reason=%s",
                validation_error.split(".", 1)[0],
            )
            flash(validation_error)
            return render_template("upload.html")
        try:
            document_dir = os.path.join(UPLOAD_DIR, "documents")
            os.makedirs(document_dir, exist_ok=True)
            path = os.path.join(
                document_dir, secrets.token_hex(16) + extension
            )
            with open(path, "wb") as out:
                out.write(content)
        except OSError as exc:
            app.logger.warning(
                "document upload storage failed type=%s",
                type(exc).__name__,
            )
            flash("Unable to save the document. Please try again later.")
            return render_template("upload.html")
        session["doc_path"] = path
        return redirect(url_for("declaration"))
    return render_template("upload.html")


@app.route("/declaration", methods=["GET", "POST"])
def declaration():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if request.method == "POST":
        return handle_submission()
    return render_template("declaration.html")


def handle_submission():
    # TODO: split this up some day. It grew. - RK, 09/2024
    mobile = session.get("verified_mobile")
    data = session.get("form_data", {})
    doc_path = session.get("doc_path", "")

    if not mobile:
        app.logger.info(
            "application submission rejected stage=final_submission "
            "reason=missing_verified_session"
        )
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))

    name = sanitize(data.get("applicant_name", ""))
    village = data.get("village", "").strip()
    block = data.get("block", "").strip()
    gender = data.get("gender", "")
    marital = data.get("marital_status", "")
    normalize_husband_fields(data)
    husband_name = data.get("husband_name", "")
    husband_employer = data.get("husband_employer", "")
    bank_account = data.get("bank_account", "").strip()
    ifsc = data.get("ifsc", "").strip().upper()
    dob_raw = data.get("dob", "").strip()
    dob = parse_date_of_birth(dob_raw)
    dob_shape, dob_result = describe_date_input(dob_raw, dob)
    app.logger.info(
        "application submission input summary stage=final_submission "
        "name_present=%s dob_shape=%s dob_result=%s gender_present=%s "
        "marital_present=%s address_present=%s bank_present=%s "
        "document_present=%s husband_required=%s husband_name_present=%s "
        "husband_employer_present=%s",
        bool(name),
        dob_shape,
        dob_result,
        bool(gender),
        bool(marital),
        bool(village and block),
        bool(bank_account),
        bool(doc_path),
        husband_fields_required(gender, marital),
        bool(husband_name),
        bool(husband_employer),
    )

    if not name:
        return application_validation_redirect("missing_applicant_name", 1)
    if not dob_raw:
        return application_validation_redirect("missing_date_of_birth", 1)
    if not gender:
        return application_validation_redirect("missing_gender", 1)
    if not marital:
        return application_validation_redirect("missing_marital_status", 1)
    if not village or not block:
        return application_validation_redirect("missing_address_field", 2)
    if not bank_account:
        return application_validation_redirect("missing_bank_account", 3)
    if not doc_path:
        app.logger.info(
            "application validation failed stage=final_submission "
            "reason=missing_document step=5"
        )
        flash("Please upload an age-proof document before submitting.")
        return redirect(url_for("upload"))

    if husband_fields_required(gender, marital):
        if not husband_name or not husband_employer:
            return application_validation_redirect("missing_husband_field", 1)

    if dob is None:
        return application_validation_redirect("invalid_date_of_birth", 1)

    today = date.today()
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    if age < MIN_AGE:
        app.logger.info(
            "application validation failed stage=final_submission "
            "reason=underage step=1"
        )
        flash("Applicant must be above %d years of age to be eligible." % MIN_AGE)
        return redirect(url_for("form_step", step=1))

    if datetime.now() > SCHEME_DEADLINE:
        flash("The application deadline has passed.")
        return redirect(url_for("index"))

    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute(
            "SELECT count(*) FROM applications WHERE mobile = %s",
            (mobile,),
        )
        if cur.fetchone()[0] > 0:
            cur.close(); conn.close()
            flash("An application already exists for this mobile number. "
                  "Duplicate applications are not permitted.")
            return redirect(url_for("index"))

        app_no = new_application_no()
        cur.execute(
            """INSERT INTO applications
               (application_no, applicant_name, mobile, gender, marital_status,
                husband_name, husband_employer, village, block, bank_account,
                ifsc, doc_path, status, submitted_at, dob)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s,%s)
               RETURNING id""",
            (app_no, name, mobile, gender, marital, husband_name,
             husband_employer, village, block, bank_account, ifsc, doc_path,
             datetime.now(), dob))
        new_id = cur.fetchone()[0]

        portal_pass = secrets.token_urlsafe(18)
        cur.execute(
            "SELECT count(*) FROM portal_users WHERE mobile = %s",
            (mobile,),
        )
        if cur.fetchone()[0] == 0:
            cur.execute(
                "INSERT INTO portal_users (mobile, password_hash) "
                "VALUES (%s,%s)",
                (mobile, hash_temporary_password(
                    portal_pass,
                    datetime.now() + timedelta(hours=24),
                )),
            )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        app.logger.error(
            "application submission database failure type=%s",
            type(exc).__name__,
        )
        cur.close()
        conn.close()
        raise

    try:
        app.logger.info("generating acknowledgment pdf application=%d" % new_id)
        pdf_bytes = generate_acknowledgment(cur, new_id)
        ack_dir = os.path.join(UPLOAD_DIR, "ack")
        os.makedirs(ack_dir, exist_ok=True)
        with open(os.path.join(ack_dir, "%d.pdf" % new_id), "wb") as out:
            out.write(pdf_bytes)
    finally:
        cur.close()
        conn.close()

    session.pop("form_data", None)
    session.pop("doc_path", None)
    session.pop("verified_mobile", None)
    # Retain this signed, session-bound marker so the confirmation link can
    # be refreshed or reopened without making acknowledgment PDFs public.
    session["ack_app_id"] = new_id
    send_sms(
        mobile,
        "Sewa Setu: application %s received. Temporary status password: %s"
        % (app_no, portal_pass),
    )
    return render_template("confirmation.html", app_no=app_no, app_id=new_id)



def generate_acknowledgment(cur, app_id):
    cur.execute(
        "SELECT application_no, applicant_name, mobile, dob, village, block, "
        "bank_account, ifsc, submitted_at, status, decided_at "
        "FROM applications WHERE id = %s",
        (app_id,),
    )
    row = cur.fetchone()
    if row:
    app.logger.info(
        "ACK field presence app_id=%s fields=%s",
        app_id,
        {
            "applicant_name": bool(row[1]),
            "village": bool(row[4]),
            "block": bool(row[5]),
            "status": bool(row[9]),
        },
    )
    if not row:
        raise ValueError("Application not found")

    font_path = os.path.join(BASE_DIR, "fonts", "NotoSansBengali.ttf")
    if not os.path.isfile(font_path):
        raise FileNotFoundError("Unicode font not found: " + font_path)

    pdf = FPDF()
    pdf.set_text_shaping(use_shaping_engine=True)
    pdf.add_page()
    pdf.add_font("NotoBengali", fname=font_path)

    pdf.set_font("NotoBengali", size=14)
    pdf.cell(0, 10, "GOVERNMENT OF PURVANCHAL",
             new_x="LMARGIN", new_y="NEXT", align="C")

    pdf.set_font("NotoBengali", size=11)
    pdf.cell(0, 8, "Department of Social Welfare",
             new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.cell(0, 8, "Old Age Pension Scheme - Acknowledgment",
             new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(4)

    labels = [
        "Application No", "Applicant Name", "Mobile", "Date of Birth",
        "Village", "Block", "Bank Account", "IFSC", "Submitted At", "Status",
        "Decided At",
    ]


    if len(row) != len(labels):
        raise ValueError(
            "Acknowledgment field mismatch: "
            f"expected {len(labels)} values, got {len(row)}"
        )

    for label, value in zip(labels, row):
        if label in ("Status", "Decided At") and value is None:
            value = "Pending"

        pdf.set_font("Helvetica", size=10)
        pdf.cell(55, 8, label, border=1)
        pdf.set_font("NotoBengali", size=10)
        pdf.multi_cell(
            0, 8, str(value if value is not None else ""), border=1,
            new_x="LMARGIN", new_y="NEXT"
        )


    pdf.ln(6)
    pdf.set_font("Helvetica", size=9)
    pdf.multi_cell(
        0, 5,
        "This is a computer generated acknowledgment. Processing SLA "
        "as per the Purvanchal Right to Public Services Act applies."
    )

    return bytes(pdf.output())

# ---------------------------------------------------------------------------
# Status portal (citizen login)
# ---------------------------------------------------------------------------

@app.route("/status", methods=["GET", "POST"])
def status_login():
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        password = request.form.get("password", "")
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT password_hash FROM portal_users WHERE mobile = %s", (mobile,))
        row = cur.fetchone()
        authenticated, needs_upgrade, temporary = (
            verify_password(row[0], password) if row else (False, False, False)
        )
        if temporary:
            cur.close()
            conn.close()
            flash("Your temporary password must be replaced. "
                  "Request a verification code to continue.")
            return redirect(url_for("reset_password"))
        if authenticated:
            if needs_upgrade:
                cur.execute(
                    "UPDATE portal_users SET password_hash = %s WHERE mobile = %s",
                    (hash_password(password), mobile),
                )
                conn.commit()
            cur.close(); conn.close()
            session["logged_in"] = True
            session["portal_mobile"] = mobile
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT id FROM applications WHERE mobile = %s "
                        "ORDER BY submitted_at DESC LIMIT 1", (mobile,))
            r = cur.fetchone()
            cur.close(); conn.close()
            if r:
                return redirect(url_for("view_application", app_id=r[0]))
            flash("No application found for this mobile number.")
            return redirect(url_for("status_login"))
        cur.close(); conn.close()
        flash("Something went wrong. Please try again.")
    return render_template("status_login.html")


@app.route("/application/<int:app_id>")
def view_application(app_id):
    if not session.get("logged_in") and not session.get("admin"):
        flash("Please login to view application status.")
        return redirect(url_for("status_login"))

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT %s FROM applications WHERE id = %%s"
                % APPLICATION_SUMMARY_COLUMNS,
                (app_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        abort(404)

    if not session.get("admin") and row[2] != session.get("portal_mobile"):
        abort(404)

    return render_template("application.html", a=row, app_id=app_id)



@app.route("/ack/<int:app_id>.pdf")
def ack_pdf(app_id):
    # Do not clear this marker here: browser refreshes and reopened
    # confirmation links must continue to work during the active session.
    same_submission = session.get("ack_app_id") == app_id
    authenticated = session.get("logged_in") or session.get("admin")
    if not authenticated and not same_submission:
        app.logger.warning(
            "ACK PDF denied: app_id=%s reason=not_authenticated_or_same_submission",
            app_id,
        )
        abort(403)

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT mobile FROM applications WHERE id = %s",
                (app_id,),
            )
            row = cur.fetchone()

            # Diagnostic log: never log the applicant's mobile number.
            app.logger.info(
                "ACK PDF lookup: app_id=%s found=%s logged_in=%s admin=%s",
                app_id,
                row is not None,
                bool(session.get("logged_in")),
                bool(session.get("admin")),
            )

            if not row:
                app.logger.warning(
                    "ACK PDF not found: app_id=%s reason=missing_application",
                    app_id,
                )
                abort(404)

            # Verify ownership for logged-in portal users. A same-session
            # post-submission request is bound to this exact application ID.
            if authenticated and not session.get("admin"):
                portal_mobile = session.get("portal_mobile")

                app.logger.info(
                    "ACK PDF ownership check: app_id=%s "
                    "portal_mobile_present=%s",
                    app_id,
                    bool(portal_mobile),
                )

                if not portal_mobile or row[0] != portal_mobile:
                    app.logger.warning(
                        "ACK PDF denied: app_id=%s "
                        "reason=ownership_mismatch",
                        app_id,
                    )
                    abort(404)

            # Generate a fresh PDF using current database values.
            data = generate_acknowledgment(cur, app_id)

        # Refresh the disk cache atomically.
        pdf_path = os.path.join(UPLOAD_DIR, "ack", "%d.pdf" % app_id)
        os.makedirs(os.path.dirname(pdf_path), exist_ok=True)

        temporary_path = pdf_path + ".tmp"
        try:
            with open(temporary_path, "wb") as out:
                out.write(data)
            os.replace(temporary_path, pdf_path)
        finally:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)

        app.logger.info(
            "ACK PDF generated successfully: app_id=%s",
            app_id,
        )

        return send_file(
            io.BytesIO(data),
            mimetype="application/pdf",
            as_attachment=False,
            download_name="acknowledgment_%s.pdf" % app_id,
        )

    except Exception:
        app.logger.exception(
            "ACK PDF generation failed: app_id=%s",
            app_id,
        )
        raise

    finally:
        conn.close()


@app.route("/status/reset", methods=["GET", "POST"])
def reset_password():
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        code = request.form.get("code", "").strip()
        new_password = request.form.get("new_password", "")
        ip_address = request.remote_addr or "unknown"
        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute("SELECT mobile FROM portal_users WHERE mobile = %s", (mobile,))
            row = cur.fetchone()
            if not row:
                conn.rollback()
                flash("If the mobile number exists, a verification code was sent.")
                return render_template("reset.html")

            if not code:
                if not reset_request_allowed(mobile, ip_address):
                    conn.rollback()
                    flash("Please wait before requesting another code.")
                    return render_template("reset.html")
                reset_code = new_reset_code()
                cur.execute(
                    "INSERT INTO otps (mobile, code, created_at) VALUES (%s, %s, %s)",
                    (mobile, reset_code_digest(reset_code), datetime.now()),
                )
                conn.commit()
                send_sms(mobile, "Sewa Setu password reset verification code: %s"
                         % reset_code)
                flash("If the mobile number exists, a verification code was sent.")
                return render_template("reset.html")

            if not reset_attempt_allowed(mobile):
                clear_reset_attempts(mobile)
                cur.execute("DELETE FROM otps WHERE mobile = %s", (mobile,))
                conn.commit()
                flash("Too many verification attempts. Request a new code later.")
                return render_template("reset.html")

            if not complete_password_reset(
                    cur, mobile, code, new_password):
                conn.rollback()
                flash("The verification code is invalid, expired, or the "
                      "password is too short.")
                return render_template("reset.html")

            conn.commit()
            flash("Password reset successfully. Please sign in.")
        except RateLimiterUnavailable:
            conn.rollback()
            flash("The password reset service is temporarily unavailable. "
                  "Please try again later.")
            return render_template("reset.html")
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            conn.close()
    return render_template("reset.html")


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if (request.form.get("username") == ADMIN_USERNAME and
                check_password_hash(
                    ADMIN_PASSWORD_HASH, request.form.get("password", "")
                )):
            session["logged_in"] = True
            session["admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("Invalid credentials.")
    return render_template("admin_login.html")


@app.route("/admin/dashboard")
def admin_dashboard():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT status, count(*) FROM applications GROUP BY status")
    by_status = cur.fetchall()
    cur.execute("SELECT count(*) FROM applications WHERE status = 'PENDING' "
                "AND submitted_at < %s", (datetime.now() - timedelta(days=SLA_DAYS),))
    overdue = cur.fetchone()[0]
    cur.execute("SELECT block, count(*) FROM applications WHERE status = 'PENDING' "
                "GROUP BY block ORDER BY count(*) DESC")
    by_block = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_dashboard.html", by_status=by_status,
                           overdue=overdue, by_block=by_block, sla=SLA_DAYS)


@app.route("/admin/applications")
def admin_list():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    status = request.args.get("status", "PENDING")
    page = int(request.args.get("page", 1))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, application_no, applicant_name, mobile, block, status, "
                "submitted_at FROM applications WHERE status = %s "
                "ORDER BY submitted_at ASC LIMIT 50 OFFSET %s",
                (status, (page - 1) * 50))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_list.html", rows=rows, status=status, page=page)


@app.route("/admin/application/<int:app_id>")
def admin_view(app_id):
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT application_no, applicant_name, mobile, dob, gender, "
                "marital_status, village, block, bank_account, ifsc, doc_path, "
                "status, submitted_at, decided_at, decided_by "
                "FROM applications WHERE id = %s", (app_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        abort(404)
    return render_template("admin_view.html", a=row, app_id=app_id)


@app.route("/admin/approve/<int:app_id>", methods=["POST"])
def admin_approve(app_id):
    if not session.get("admin"):
        abort(403)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE applications SET status = 'APPROVED', decided_at = %s "
                "WHERE id = %s AND status = 'PENDING'",
                (datetime.now(), app_id))
    updated = cur.rowcount
    conn.commit()
    cur.close(); conn.close()
    flash("Application approved." if updated else
          "Application was already decided or is no longer pending.")
    return redirect(url_for("admin_list"))


@app.route("/admin/reject/<int:app_id>", methods=["POST"])
def admin_reject(app_id):
    if not session.get("admin"):
        abort(403)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE applications SET status = 'REJECTED', decided_at = %s "
                "WHERE id = %s AND status = 'PENDING'",
                (datetime.now(), app_id))
    updated = cur.rowcount
    conn.commit()
    cur.close(); conn.close()
    flash("Application rejected." if updated else
          "Application was already decided or is no longer pending.")
    return redirect(url_for("admin_list"))


# ---------------------------------------------------------------------------
# Unused / legacy
# ---------------------------------------------------------------------------

def export_to_excel(rows):
    # Was used for the monthly disbursement report before eKosh integration.
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["application_no", "name", "account", "ifsc", "amount"])
    for r in rows:
        writer.writerow(list(r) + ["250"])
    return out.getvalue()


@app.route("/__gateway/", defaults={"subpath": ""})
@app.route("/__gateway/<path:subpath>")
def gateway_proxy(subpath):
    """Proxy the simulated SMS gateway through the main website."""
    token = os.environ.get("SEWASETU_SMS_GATEWAY_TOKEN", "")

    if not token:
        app.logger.error("Gateway proxy unavailable: token not configured")
        return "SMS gateway is not configured", 503

    # Prevent access to internal API endpoints through the browser proxy.
    # The console at / is the intended demo interface.
    if subpath not in ("",):
        abort(404)

    try:
        response = requests.get(
            SMS_GATEWAY_URL.rstrip("/") + "/",
            headers={"X-Gateway-Token": token},
            timeout=10,
        )

        return (
            response.content,
            response.status_code,
            {"Content-Type": response.headers.get(
                "Content-Type", "text/html"
            )},
        )
    except requests.RequestException:
        app.logger.exception("SMS gateway console proxy failed")
        return "SMS gateway is temporarily unavailable", 502


def old_payment_gateway_callback(txn):
    # retained for reference; eChallan integration was descoped in Phase 2
    status = txn.get("STATUS")
    if status == "0300":
        return "SUCCESS"
    elif status == "0399":
        return "FAILED"
    return "PENDING"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
