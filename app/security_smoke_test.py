import werkzeug
if not hasattr(werkzeug, "__version__"):
    werkzeug.__version__ = "3.1.9"

import app as portal
print("Flask app imported successfully.")
print("Werkzeug compatibility shim applied.")
