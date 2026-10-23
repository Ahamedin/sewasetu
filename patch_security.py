from pathlib import Path
import re
import shutil

path = Path("app/app.py")
source = path.read_text(encoding="utf-8")

# Verify the expected original code before making changes.
checks = {
    "application route": 'def view_application(app_id):',
    "PDF route": 'def ack_pdf(app_id):',
    "admin approval": 'def admin_approve(app_id):\n    if not session.get("logged_in"):',
    "admin rejection": 'def admin_reject(app_id):\n    if not session.get("logged_in"):',
    "reset SQL 1": '''cur.execute("SELECT mobile FROM portal_users WHERE mobile = '%s'" % mobile)''',
    "reset SQL 2": '''cur.execute("SELECT dob FROM applications WHERE mobile = '%s' LIMIT 1" % mobile)''',
}
for name, needle in checks.items():
    if source.count(needle) != 1:
        raise SystemExit(f"STOP: expected exactly one match for {name}; file unchanged.")

# Replace the citizen application and PDF routes.
pattern = (
    r'@app\.route\("/application/<int:app_id>"\)\n'
    r'def view_application\(app_id\):.*?'
    r'(?=@app\.route\("/status/reset", methods=\["GET", "POST"\]\)\n)'
)
replacement = '''@app.route("/application/<int:app_id>")
def view_application(app_id):
    if not session.get("logged_in") and not session.get("admin"):
        flash("Please login to view application status.")
        return redirect(url_for("status_login"))

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT application_no, applicant_name, mobile, dob, village, block, "
                "bank_account, ifsc, status, submitted_at, decided_at "
                "FROM applications WHERE id = %s",
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
    if not session.get("logged_in") and not session.get("admin"):
        abort(403)

    # Verify the application exists and belongs to this user before serving a PDF.
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT mobile FROM applications WHERE id = %s", (app_id,))
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        abort(404)
    if not session.get("admin") and row[0] != session.get("portal_mobile"):
        abort(404)

    pdf_path = os.path.join(UPLOAD_DIR, "ack", "%d.pdf" % app_id)
    if os.path.exists(pdf_path):
        return send_file(pdf_path, mimetype="application/pdf")

    conn = get_db()
    try:
        with conn.cursor() as cur:
            data = generate_acknowledgment(cur, app_id)
        return send_file(io.BytesIO(data), mimetype="application/pdf")
    finally:
        conn.close()


'''
updated, count = re.subn(pattern, replacement, source, count=1, flags=re.S)
if count != 1:
    raise SystemExit("STOP: could not safely replace application/PDF routes; file unchanged.")
source = updated

# Require an administrator session for both decision actions.
source = source.replace(
    'def admin_approve(app_id):\n    if not session.get("logged_in"):',
    'def admin_approve(app_id):\n    if not session.get("admin"):',
    1,
)
source = source.replace(
    'def admin_reject(app_id):\n    if not session.get("logged_in"):',
    'def admin_reject(app_id):\n    if not session.get("admin"):',
    1,
)

# Parameterize the password-reset SQL queries.
source = source.replace(
    '''cur.execute("SELECT mobile FROM portal_users WHERE mobile = '%s'" % mobile)''',
    '''cur.execute("SELECT mobile FROM portal_users WHERE mobile = %s", (mobile,))''',
    1,
)
source = source.replace(
    '''cur.execute("SELECT dob FROM applications WHERE mobile = '%s' LIMIT 1" % mobile)''',
    '''cur.execute("SELECT dob FROM applications WHERE mobile = %s LIMIT 1", (mobile,))''',
    1,
)

# Confirm every intended fix is present before writing.
required = [
    'if not session.get("admin"):',
    'row[2] != session.get("portal_mobile")',
    'row[0] != session.get("portal_mobile")',
    'cur.execute("SELECT mobile FROM portal_users WHERE mobile = %s", (mobile,))',
    'cur.execute("SELECT dob FROM applications WHERE mobile = %s LIMIT 1", (mobile,))',
]
for item in required:
    if item not in source:
        raise SystemExit(f"STOP: verification failed for {item!r}; file unchanged.")

# Create a separate backup and then write the verified result.
backup = Path("app/app.py.pre-final-patch")
if not backup.exists():
    shutil.copy2(path, backup)
path.write_text(source, encoding="utf-8")
print("Security patch applied.")
print("Backup:", backup)
