"""Local web UI for the yt-dlp downloader. Run with start.bat (or venv python webapp/app.py)."""

from __future__ import annotations

import atexit
import logging
import os
import threading
import webbrowser

from flask import Flask, jsonify, request, send_from_directory

from runner import (
    Manager, ValidationError, display_command, downloads_dir, fetch_info, open_downloads,
    pick_folder, read_config, saved_download_dir, set_download_dir, validate_options, validate_url,
)

HOST = "127.0.0.1"  # this PC only
PORT = int(os.environ.get("YTDLP_WEB_PORT", "5000"))

class _HidePolling(logging.Filter):
    """The page polls GET /api/jobs every second; don't log those requests."""

    def filter(self, record):
        msg = record.getMessage()
        return not ('"GET /api/jobs ' in msg and '" 200 ' in msg)


logging.getLogger("werkzeug").addFilter(_HidePolling())

app = Flask(__name__, static_folder="static", static_url_path="/static")
manager = Manager()
atexit.register(manager.shutdown)


@app.errorhandler(ValidationError)
def _bad_request(e):
    return jsonify(error=str(e)), 400


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/version")
def version():
    return jsonify(version=manager.version())


@app.post("/api/info")
def info():
    data = request.get_json(force=True) or {}
    url = validate_url(data.get("url"))
    opts = validate_options(data)
    return jsonify(fetch_info(url, opts))


@app.post("/api/command")
def command():
    """The yt-dlp command the current form options would run (display only)."""
    data = request.get_json(force=True) or {}
    urls = [u.strip() for u in data.get("urls") or [] if isinstance(u, str) and u.strip()]
    return jsonify(commands=display_command(urls, validate_options(data)))


@app.get("/api/config")
def config():
    """yt-dlp.conf's contents (display only), loaded by every download."""
    return jsonify(read_config())


@app.post("/api/jobs")
def add_jobs():
    data = request.get_json(force=True) or {}
    raw = data.get("urls") or [data.get("url")]
    urls = [validate_url(u) for u in raw if u and u.strip()]
    if not urls:
        raise ValidationError("Paste at least one URL.")
    opts = validate_options(data)
    jobs = [manager.add(u, opts).id for u in urls]
    return jsonify(ids=jobs), 201


@app.get("/api/jobs")
def list_jobs():
    return jsonify(jobs=manager.list())


@app.post("/api/jobs/<job_id>/cancel")
def cancel_job(job_id):
    if not manager.cancel(job_id):
        return jsonify(error="Job is not queued or running."), 409
    return jsonify(ok=True)


@app.delete("/api/jobs/finished")
def clear_finished():
    return jsonify(removed=manager.clear_finished())


@app.post("/api/open-downloads")
def open_downloads_folder():
    try:
        return jsonify(path=open_downloads())
    except OSError as e:
        return jsonify(error=f"Could not open the downloads folder: {e}"), 500


def _download_dir_info():
    return {"path": str(downloads_dir()), "custom": saved_download_dir() is not None}


@app.get("/api/download-dir")
def get_download_dir():
    return jsonify(_download_dir_info())


@app.post("/api/download-dir/choose")
def choose_download_dir():
    """Open the native folder picker; save the choice for future downloads."""
    try:
        folder = pick_folder(str(downloads_dir()))
    except OSError as e:
        return jsonify(error=str(e)), 500
    if folder:
        set_download_dir(folder)
    return jsonify({**_download_dir_info(), "cancelled": folder is None})


@app.delete("/api/download-dir")
def reset_download_dir():
    set_download_dir(None)
    return jsonify(_download_dir_info())


@app.post("/api/update")
def update():
    result = manager.update()
    return jsonify(result), (200 if result["ok"] else 409)


if __name__ == "__main__":
    url = f"http://{HOST}:{PORT}"
    print(f"yt-dlp web UI running at {url}  (Ctrl+C to stop)")
    if not os.environ.get("YTDLP_WEB_NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=HOST, port=PORT, threaded=True, debug=False)
