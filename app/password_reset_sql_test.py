from pathlib import Path

source = Path("/app/app.py").read_text(encoding="utf-8")
start = source.index('def reset_password():')
end = source.index('@app.route("/admin"', start)
route = source[start:end]

checks = {
    "Mobile lookup uses a parameter": (
        'WHERE mobile = %s", (mobile,)' in route
    ),
    "Date-of-birth lookup uses a parameter": (
        'WHERE mobile = %s LIMIT 1", (mobile,)' in route
    ),
    "Password update uses parameters": (
        'WHERE mobile = %s",' in route
        and '(hash_password(newpass), row[0])' in route
    ),
    "No f-string SQL in reset route": (
        'cur.execute(f"' not in route
        and "cur.execute(f'" not in route
    ),
}

for name, passed in checks.items():
    print(f'{"PASS" if passed else "FAIL"}: {name}')

print(f"\nResult: {sum(checks.values())}/{len(checks)} checks passed.")

if not all(checks.values()):
    raise SystemExit(1)
