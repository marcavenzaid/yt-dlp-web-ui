"""yt-dlp subprocess runner: argument building, a one-at-a-time job queue,
progress parsing, previews and self-update.

yt-dlp is run as `python -m yt_dlp` from the venv with the yt-dlp/ folder as the
working directory, so yt-dlp.conf (and its relative --ffmpeg-location) behave
exactly like running the commands from commands.txt by hand inside that folder.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
YTDLP_DIR = ROOT / "yt-dlp"          # holds yt-dlp.conf, commands.txt, ffmpeg build
CONF = YTDLP_DIR / "yt-dlp.conf"
_venv_python = ROOT / "venv" / "Scripts" / "python.exe"
PYTHON = str(_venv_python if _venv_python.exists() else Path(sys.executable))

IS_WINDOWS = os.name == "nt"
CREATION_FLAGS = (
    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
)
CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}

# ---------------------------------------------------------------- validation

QUALITIES = ("360", "480", "720", "1080", "1440", "2160")
PRESETS = ("video", "best", "audio")
BROWSERS = ("firefox", "chrome", "edge", "brave", "opera", "vivaldi", "chromium")

URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
TIME_RE = re.compile(r"^\d+(?::\d{1,2}){0,2}(?:\.\d+)?$")


class ValidationError(ValueError):
    pass


def _seconds(t: str) -> float:
    total = 0.0
    for part in t.split(":"):
        total = total * 60 + float(part)
    return total


def validate_url(url: str) -> str:
    url = (url or "").strip()
    if not URL_RE.match(url):
        raise ValidationError(f"Not a valid http(s) URL: {url!r}")
    return url


def validate_options(data: dict) -> dict:
    """Normalise and validate the download options sent by the browser."""
    preset = data.get("preset", "video")
    if preset not in PRESETS:
        raise ValidationError(f"Unknown preset: {preset!r}")

    quality = str(data.get("quality", "1080"))
    if preset == "video" and quality not in QUALITIES:
        raise ValidationError(f"Unknown quality: {quality!r}")

    start = (data.get("start") or "").strip()
    end = (data.get("end") or "").strip()
    for label, value in (("Start", start), ("End", end)):
        if value and not TIME_RE.match(value):
            raise ValidationError(f"{label} time must look like 58, 0:58 or 1:04:51 (got {value!r})")
    if start and end and _seconds(start) >= _seconds(end):
        raise ValidationError("Start time must be before end time")

    cookies = bool(data.get("cookies"))
    browser = data.get("browser", "firefox")
    if cookies and browser not in BROWSERS:
        raise ValidationError(f"Unknown browser: {browser!r}")

    return {
        "preset": preset,
        "quality": quality if preset == "video" else None,
        "start": start,
        "end": end,
        "cookies": cookies,
        "browser": browser if cookies else None,
        # Captured now, so a queued job keeps the folder it was added with.
        "folder": saved_download_dir(),
    }


# ------------------------------------------------------------- arg building

def base_args() -> list[str]:
    # --ignore-config + --config-locations: load exactly our yt-dlp.conf once.
    return [PYTHON, "-m", "yt_dlp", "--ignore-config", "--config-locations", str(CONF)]


def preset_args(opts: dict) -> list[str]:
    """Format selection, copied verbatim from commands.txt."""
    preset = opts["preset"]
    if preset == "video":
        q = opts["quality"]
        return [
            "-f", f"bv*[height<={q}]+ba[format_note*=original]/bv*[height<={q}]+ba/b[height<={q}]",
            "-S", "res,fps,br,ext:mp4:m4a",
        ]
    if preset == "best":
        return ["-f", "bv*+ba[format_note*=original]/bv*+ba/b", "-S", "res,fps,br,ext:mp4:m4a"]
    return [
        "--extract-audio",
        "-f", "ba[ext=m4a][format_note*=original]/ba[format_note*=original]/ba[ext=m4a]/ba/b[acodec!=none]",
        "-S", "abr,acodec:mp4a",
    ]


def cookie_args(opts: dict) -> list[str]:
    return ["--cookies-from-browser", opts["browser"]] if opts.get("cookies") else []


def option_args(opts: dict) -> list[str]:
    """The user-chosen part of the command: format selection, cookies, sections.
    Shared by the real download and the command shown in the UI."""
    args = location_args(opts.get("folder")) + preset_args(opts) + cookie_args(opts)
    if opts["start"] or opts["end"]:
        args += ["--download-sections", f"*{opts['start'] or '0'}-{opts['end'] or 'inf'}"]
    return args


_SAFE_ARG = re.compile(r"^[\w@%+=:./-]+$")


def _quote(arg: str) -> str:
    return arg if _SAFE_ARG.match(arg) else '"' + arg.replace('"', '\\"') + '"'


def display_command(urls: list[str], opts: dict) -> str:
    """The command as you'd type it in a terminal (from the yt-dlp folder)."""
    return " ".join(_quote(a) for a in ["yt-dlp", *option_args(opts), *(urls or ["URL"])])


PROGRESS_FIELDS = (
    "progress.status", "progress.downloaded_bytes", "progress.total_bytes",
    "progress.total_bytes_estimate", "progress.speed", "progress.eta",
    "info.vcodec", "info.acodec", "info.title",  # title last: it may contain "|"
)
PROGRESS_TAG = "[[P]]"
FILE_TAG = "[[F]]"


def download_args(url: str, opts: dict) -> list[str]:
    args = base_args() + option_args(opts)
    template = PROGRESS_TAG + "|".join(f"%({f})s" for f in PROGRESS_FIELDS)
    args += [
        "--newline", "--progress", "--no-quiet",
        "--progress-template", f"download:{template}",
        "--print", f"after_move:{FILE_TAG}%(filepath)s",
        "--", url,
    ]
    return args


# ---------------------------------------------------------- downloads folder

SETTINGS_FILE = ROOT / "settings.json"  # server-side UI settings (gitignored)
DEFAULT_TEMPLATE = "%(title)s [%(id)s].%(ext)s"  # yt-dlp's default -o


def _load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_settings(settings: dict):
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)


def saved_download_dir() -> str | None:
    """The download folder chosen in the UI, or None to use yt-dlp.conf's."""
    folder = _load_settings().get("download_dir")
    return folder if isinstance(folder, str) and os.path.isabs(folder) else None


def set_download_dir(folder: str | None):
    settings = _load_settings()
    if folder:
        settings["download_dir"] = os.path.normpath(os.path.abspath(folder))
    else:
        settings.pop("download_dir", None)
    _save_settings(settings)


def _conf_template() -> str | None:
    """The last -o/--output value in yt-dlp.conf (last one wins, like yt-dlp)."""
    template = None
    try:
        lines = CONF.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            continue
        for i, tok in enumerate(tokens):
            if tok in ("-o", "--output") and i + 1 < len(tokens):
                template = tokens[i + 1]  # last one wins, like yt-dlp
            elif tok.startswith("--output="):
                template = tok.split("=", 1)[1]
    return template


def _split_template(template: str) -> tuple[Path, str]:
    """Split an output template into its fixed folder (everything before the first
    %(field)s path part) and the rest, e.g. "../Downloads/%(title)s.%(ext)s" ->
    (Path("../Downloads"), "%(title)s.%(ext)s")."""
    parts = Path(template).parts
    i = next((n for n, p in enumerate(parts[:-1]) if "%(" in p), len(parts) - 1)
    folder = Path(*parts[:i]) if i else Path(".")
    return folder, "/".join(parts[i:])


def location_args(folder: str | None) -> list[str]:
    """Send downloads to a chosen folder: -P sets it, -o keeps the config's file naming."""
    if not folder:
        return []
    _, name = _split_template(_conf_template() or DEFAULT_TEMPLATE)
    return ["-P", folder, "-o", name]


def downloads_dir() -> Path:
    """Where downloads are saved: the folder chosen in the UI if any, otherwise the
    folder yt-dlp.conf's -o template saves into (resolved against YTDLP_DIR,
    yt-dlp's cwd), or <project>/Downloads if the config has no usable -o line."""
    if folder := saved_download_dir():
        return Path(folder)
    template = _conf_template()
    if not template:
        return ROOT / "Downloads"
    folder, _ = _split_template(template)
    return (YTDLP_DIR / folder).resolve()


_PICK_FOLDER_SCRIPT = r"""
import sys, tkinter as tk
from tkinter import filedialog
root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)  # don't open behind the browser
path = filedialog.askdirectory(parent=root, title="Choose download folder",
                               initialdir=sys.argv[1] or None, mustexist=True)
sys.stdout.write(path or "")
"""
_picker_lock = threading.Lock()


def pick_folder(initial: str) -> str | None:
    """Show the native folder picker on this PC. Returns the folder, or None if
    the user cancelled. Runs Tk in a child process so it never touches Flask's threads."""
    if not _picker_lock.acquire(blocking=False):
        raise ValidationError("The folder picker is already open.")
    try:
        out = subprocess.run(
            [sys.executable, "-I", "-c", _PICK_FOLDER_SCRIPT, initial], env=CHILD_ENV,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    finally:
        _picker_lock.release()
    if out.returncode != 0:
        detail = (out.stderr.strip().splitlines() or ["unknown error"])[-1]
        raise OSError(f"Could not open the folder picker: {detail}")
    path = out.stdout.strip()
    return os.path.normpath(path) if path else None


def open_downloads() -> str:
    """Open the downloads folder in the system file manager (creating it if needed)."""
    folder = downloads_dir()
    folder.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        os.startfile(folder)  # noqa: S606 - local-only UI
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(folder)])
    return str(folder)


# --------------------------------------------------------------------- jobs

class Job:
    def __init__(self, url: str, opts: dict):
        self.id = uuid.uuid4().hex[:10]
        self.url = url
        self.opts = opts
        self.status = "queued"  # queued | running | done | error | cancelled
        self.created = time.time()
        self.started: float | None = None
        self.finished: float | None = None
        self.title: str | None = None
        self.stage = ""          # "video", "audio", "Merger", ...
        self.percent: float | None = None
        self.downloaded = 0
        self.total: int | None = None
        self.speed: float | None = None
        self.eta: int | None = None
        self.item: str | None = None   # "3 of 10" for playlists
        self.files: list[str] = []
        self.temp_paths: list[str] = []  # announced by yt-dlp; removed on cancel
        self.log: deque[str] = deque(maxlen=300)
        self.error: str | None = None
        self.proc: subprocess.Popen | None = None
        self.cancel_requested = False

    def to_dict(self) -> dict:
        return {
            "id": self.id, "url": self.url, "opts": self.opts, "status": self.status,
            "created": self.created, "started": self.started, "finished": self.finished,
            "title": self.title, "stage": self.stage, "percent": self.percent,
            "downloaded": self.downloaded, "total": self.total, "speed": self.speed,
            "eta": self.eta, "item": self.item, "files": list(self.files),
            "log": list(self.log), "error": self.error,
        }


def _num(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


DEST_RE = re.compile(r"^\[download\] Destination: (.+)$")
THUMB_RE = re.compile(r"^\[info\] Writing video thumbnail .* to: (.+)$")
ITEM_RE = re.compile(r"^\[download\] Downloading item (\d+) of (\d+)")
PP_RE = re.compile(r"^\[([A-Z][A-Za-z0-9]+)\]")


class Manager:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.queue: queue.Queue[str] = queue.Queue()
        # Held while a download (or a yt-dlp update) runs, so they never overlap.
        self.busy = threading.Lock()
        self._version: str | None = None
        threading.Thread(target=self._worker, daemon=True, name="ytdlp-worker").start()

    # ---- queue API
    def add(self, url: str, opts: dict) -> Job:
        job = Job(url, opts)
        with self.lock:
            self.jobs[job.id] = job
        self.queue.put(job.id)
        return job

    def list(self) -> list[dict]:
        with self.lock:
            return [j.to_dict() for j in self.jobs.values()]

    def cancel(self, job_id: str) -> bool:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job.status not in ("queued", "running"):
                return False
            job.cancel_requested = True
            if job.status == "queued":
                job.status = "cancelled"
                job.finished = time.time()
                return True
            proc = job.proc
        if proc and proc.poll() is None:
            kill_tree(proc)
        return True

    def clear_finished(self) -> int:
        with self.lock:
            done = [k for k, j in self.jobs.items() if j.status in ("done", "error", "cancelled")]
            for k in done:
                del self.jobs[k]
        return len(done)

    def shutdown(self):
        with self.lock:
            procs = [j.proc for j in self.jobs.values() if j.proc and j.proc.poll() is None]
        for p in procs:
            kill_tree(p)

    # ---- worker
    def _worker(self):
        while True:
            job_id = self.queue.get()
            with self.lock:
                job = self.jobs.get(job_id)
                if not job or job.status != "queued":
                    continue
            with self.busy:
                self._run(job)

    def _run(self, job: Job):
        with self.lock:
            if job.cancel_requested:
                return
            job.status = "running"
            job.started = time.time()
            job.stage = "starting"
        try:
            proc = subprocess.Popen(
                download_args(job.url, job.opts), cwd=YTDLP_DIR, env=CHILD_ENV,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace", creationflags=CREATION_FLAGS,
            )
        except OSError as e:
            with self.lock:
                job.status, job.error, job.finished = "error", f"Could not start yt-dlp: {e}", time.time()
            return

        with self.lock:
            job.proc = proc
        cancel_now = job.cancel_requested  # cancelled between status change and Popen
        if cancel_now:
            kill_tree(proc)

        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            if line:
                with self.lock:
                    self._parse_line(job, line)
        code = proc.wait()

        with self.lock:
            job.proc = None
            job.finished = time.time()
            job.speed = job.eta = None
            if job.cancel_requested:
                job.status, job.stage = "cancelled", ""
                removed = remove_partials(job.temp_paths, keep=job.files)
                if removed:
                    job.log.append(f"[web] Removed {removed} partial file(s) after cancel")
            elif code == 0:
                job.status, job.stage, job.percent = "done", "", 100.0
            else:
                job.status, job.stage = "error", ""
                if not job.error:
                    job.error = f"yt-dlp exited with code {code}"

    def _parse_line(self, job: Job, line: str):
        if line.startswith(PROGRESS_TAG):
            parts = line[len(PROGRESS_TAG):].split("|", len(PROGRESS_FIELDS) - 1)
            if len(parts) != len(PROGRESS_FIELDS):
                return
            status, done, total, est, speed, eta, vcodec, acodec, title = parts
            if title and title != "NA":
                job.title = title
            total_n = _num(total) or _num(est)
            done_n = _num(done) or 0
            job.downloaded = int(done_n)
            job.total = int(total_n) if total_n else None
            job.percent = (done_n / total_n * 100) if total_n else job.percent
            job.speed = _num(speed)
            eta_n = _num(eta)
            job.eta = int(eta_n) if eta_n is not None else None
            if status == "finished":
                job.percent = 100.0
            has_v = vcodec not in ("none", "NA", "")
            has_a = acodec not in ("none", "NA", "")
            job.stage = "video+audio" if has_v and has_a else "video" if has_v else "audio" if has_a else "downloading"
            return

        if line.startswith(FILE_TAG):
            path = line[len(FILE_TAG):]
            if path and path not in job.files:
                job.files.append(path)
            return

        job.log.append(line)

        if m := DEST_RE.match(line):
            job.temp_paths.append(m.group(1))
            job.percent = 0.0
        elif m := THUMB_RE.match(line):
            job.temp_paths.append(m.group(1))
        elif m := ITEM_RE.match(line):
            job.item = f"{m.group(1)} of {m.group(2)}"
        elif line.startswith("ERROR:"):
            job.error = line[len("ERROR:"):].strip()
        elif m := PP_RE.match(line):
            job.stage = m.group(1)
            job.speed = job.eta = None

    # ---- version / update
    def version(self, refresh: bool = False) -> str:
        if self._version is None or refresh:
            try:
                out = subprocess.run(
                    [PYTHON, "-m", "yt_dlp", "--version"], cwd=YTDLP_DIR, env=CHILD_ENV,
                    capture_output=True, text=True, timeout=30, creationflags=CREATION_FLAGS,
                )
                self._version = out.stdout.strip() or "unknown"
            except (OSError, subprocess.TimeoutExpired):
                self._version = "unknown"
        return self._version

    def update(self) -> dict:
        if not self.busy.acquire(blocking=False):
            return {"ok": False, "output": "A download is running. Try again when the queue is idle."}
        try:
            before = self.version()
            out = subprocess.run(
                [PYTHON, "-m", "pip", "install", "-U", "yt-dlp[default]"], cwd=YTDLP_DIR, env=CHILD_ENV,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=600, creationflags=CREATION_FLAGS,
            )
            after = self.version(refresh=True)
            return {
                "ok": out.returncode == 0, "before": before, "version": after,
                "output": (out.stdout + out.stderr).strip(),
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "output": "pip timed out after 10 minutes."}
        finally:
            self.busy.release()


def remove_partials(paths: list[str], keep: list[str]) -> int:
    """Delete leftovers of a cancelled download: only paths yt-dlp announced for
    this job (plus their .part/.ytdl/fragment files), never a finished file."""
    keep_set = {os.path.normcase(os.path.abspath(k)) for k in keep}
    removed = 0
    for p in dict.fromkeys(paths):
        if os.path.normcase(os.path.abspath(p)) in keep_set:
            continue
        base = Path(p)
        candidates = [base, Path(p + ".part"), Path(p + ".ytdl"), *base.parent.glob(glob_escape(base.name) + ".part-Frag*")]
        for c in candidates:
            try:
                if c.is_file():
                    c.unlink()
                    removed += 1
            except OSError:
                pass
    return removed


def glob_escape(name: str) -> str:
    # Titles often contain [ ] which glob treats as character classes.
    return re.sub(r"([\[\]*?])", r"[\1]", name)


def kill_tree(proc: subprocess.Popen):
    """Kill yt-dlp together with any ffmpeg children it spawned."""
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        proc.kill()


# ------------------------------------------------------------------ preview

def _fmt_row(f: dict) -> dict:
    return {
        "id": f.get("format_id"),
        "ext": f.get("ext"),
        "resolution": f.get("resolution") or ("audio only" if f.get("vcodec") == "none" else None),
        "fps": f.get("fps"),
        "vcodec": f.get("vcodec"),
        "acodec": f.get("acodec"),
        "size": f.get("filesize"),
        "size_approx": f.get("filesize_approx") if not f.get("filesize") else None,
        "tbr": f.get("tbr"),
        "note": f.get("format_note"),
        "language": f.get("language"),
    }


def fetch_info(url: str, opts: dict) -> dict:
    args = base_args() + cookie_args(opts) + ["-J", "--flat-playlist", "--", url]
    try:
        out = subprocess.run(
            args, cwd=YTDLP_DIR, env=CHILD_ENV, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=90, creationflags=CREATION_FLAGS,
        )
    except subprocess.TimeoutExpired:
        raise ValidationError("yt-dlp took too long to fetch info (90 s).")
    if out.returncode != 0 or not out.stdout.strip():
        errors = [l for l in out.stderr.splitlines() if l.startswith("ERROR:")]
        raise ValidationError(errors[-1][6:].strip() if errors else out.stderr.strip()[-500:] or "yt-dlp failed")

    info = json.loads(out.stdout)
    formats = [
        _fmt_row(f) for f in reversed(info.get("formats") or [])
        if not (f.get("vcodec") == "none" and f.get("acodec") == "none")  # skip storyboards
    ]
    entries = info.get("entries")
    return {
        "type": info.get("_type", "video"),
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail") or next(
            (t.get("url") for t in reversed(info.get("thumbnails") or []) if t.get("url")), None),
        "webpage_url": info.get("webpage_url") or url,
        "entries": len(entries) if isinstance(entries, list) else None,
        "formats": formats,
    }
