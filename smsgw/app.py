
"""PurvSMS - authenticated simulated SMS gateway."""

import hmac
import os
import random
import time
from flask import Flask, request, jsonify, render_template_string, abort

app = Flask(__name__)

# Set this environment variable in Render. Never hardcode the token.
GATEWAY_TOKEN = os.environ.get("SEWASETU_SMS_GATEWAY_TOKEN", "")

MESSAGES = []

DELAY_MIN = 1
DELAY_MAX = 4

CONSOLE = """
<!doctype html><html><head><title>PurvSMS Console</title>
<meta http-equiv="refresh" content="5">
<style>
body{font-family:Arial,sans-serif;margin:24px;background:#f4f4f4}
h2{color:#1a3c6e} table{border-collapse:collapse;background:#fff;width:100%}
td,th{border:1px solid #ccc;padding:6px 10px;font-size:14px;text-align:left}
.q{color:#b36b00}.d{color:#1b7e3c}
</style></head><body>
<h2>PurvSMS Gateway Console (simulation)</h2>
<p>Messages appear below once delivered. Auto-refreshes every 5s.</p>
<table><tr><th>#</th><th>To</th><th>Message</th><th>Sent</th><th>Status</th></tr>
{% for m in messages %}
<tr><td>{{m.id}}</td><td>{{m.to}}</td>
<td>{% if m.delivered %}{{m.text}}{% else %}<i>(in transit)</i>{% endif %}</td>
<td>{{m.sent_hhmm}}</td>
<td>{% if m.delivered %}<span class="d">Delivered</span>
{% else %}<span class="q">Queued &middot; ~{{m.eta}}s</span>{% endif %}</td></tr>
{% endfor %}
</table></body></html>
"""


def authorized():
    supplied = request.headers.get("X-Gateway-Token", "")
    return bool(GATEWAY_TOKEN) and hmac.compare_digest(
        supplied, GATEWAY_TOKEN
    )



@app.before_request
def require_gateway_token():
    # Public health check.
    # Allow the main app's authenticated proxy to display the console.
    if request.path == "/":
        if authorized():
            return None

        demo_key = os.environ.get("SEWASETU_DEMO_CONSOLE_KEY", "")
        supplied_key = request.args.get("key", "")

        if demo_key and hmac.compare_digest(supplied_key, demo_key):
            return None

        return jsonify({"error": "Demo console access required"}), 401



@app.route("/healthz")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/send", methods=["POST"])
def send():
    data = request.get_json(silent=True) or request.form
    to = str(data.get("to", "")).strip()
    message_text = str(data.get("text", "")).strip()

    if not to or not message_text:
        return jsonify({"status": "error", "reason": "to and text required"}), 400

    now = time.time()
    msg = {
        "id": len(MESSAGES) + 1,
        "to": to,
        "text": message_text,
        "sent_at": now,
        "delivery_at": now + random.randint(DELAY_MIN, DELAY_MAX),
    }
    MESSAGES.append(msg)
    return jsonify({"status": "queued", "id": msg["id"]})


@app.route("/api/messages")
def messages():
    to = request.args.get("to", "").strip()
    now = time.time()
    out = []

    for m in MESSAGES:
        if to and m["to"] != to:
            continue
        out.append({
            "id": m["id"],
            "to": m["to"],
            "text": m["text"] if now >= m["delivery_at"] else None,
            "delivered": now >= m["delivery_at"],
            "sent_at": m["sent_at"],
            "delivery_at": m["delivery_at"],
        })

    return jsonify({"messages": out})


@app.route("/")
def console():
    now = time.time()
    view = []

    for m in reversed(MESSAGES[-100:]):
        view.append({
            "id": m["id"],
            "to": m["to"],
            "text": m["text"],
            "sent_hhmm": time.strftime(
                "%H:%M:%S", time.localtime(m["sent_at"])
            ),
            "delivered": now >= m["delivery_at"],
            "eta": max(0, int(m["delivery_at"] - now)),
        })

    return render_template_string(CONSOLE, messages=view)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8025")))
