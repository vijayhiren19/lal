"""Start Flask API server without debug mode (no reloader)."""
from src.api.app import create_app

app = create_app()
print("Starting API server at http://127.0.0.1:5000", flush=True)
app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
