import os
import subprocess
import shutil
import signal
import threading
import queue
import hmac
import logging
from collections import deque
from pathlib import Path
from urllib.parse import urlparse
from flask import Flask, render_template, request, Response, send_from_directory
from typing import cast

# Optional TOML config support: prefer stdlib `tomllib` (Python 3.11+), fall back to `tomli` if available
try:
    import tomllib as _toml
except Exception:
    try:
        import tomli as _toml
    except Exception:
        _toml = None

def _load_config():
    """Load configuration from TOML and environment variables.

    Order of precedence (highest first): environment variables -> /etc/tidal-dl/config.toml -> ./config.toml -> defaults
    Supports keys: template_folder, static_folder, tidal_dl_bin, download_timeout, download_token, flask_host, flask_port
    """
    cfg = {}
    if _toml is None:
        logging.info("TOML support not available (no tomllib/tomli); skipping config file load")
    else:
        candidates = [Path('/etc/tidal-dl/config.toml'), Path(__file__).parent / 'config.toml']
        for p in candidates:
            try:
                if p.exists():
                    with p.open('rb') as fh:
                        data = _toml.load(fh)
                    # allow both a top-level table `tidal-dl` or flat keys
                    if isinstance(data.get('tidal-dl'), dict):
                        cfg.update(data.get('tidal-dl', {}))
                    else:
                        cfg.update(data)
                    logging.info(f"Loaded config from {p}")
                    break
            except Exception as e:
                logging.warning(f"Failed to read config {p}: {e}")

    # defaults
    default_dir = os.path.dirname(os.path.abspath(__file__))

    def _env_or_cfg(name, cfg_key=None, default=None):
        # environment takes precedence
        v = os.environ.get(name)
        if v is not None:
            return v
        key = (cfg_key or name).lower()
        return cfg.get(key, default)

    TEMPLATE_FOLDER = _env_or_cfg('TEMPLATE_FOLDER', 'template_folder', os.path.join(default_dir, 'templates'))
    STATIC_FOLDER = _env_or_cfg('STATIC_FOLDER', 'static_folder', os.path.join(default_dir, 'static'))
    TIDAL_DL_BIN = _env_or_cfg('TIDAL_DL_BIN', 'tidal_dl_bin', 'tidal-dl-ng')
    # download timeout may be int in TOML; ensure string/env handled
    _dt = _env_or_cfg('DOWNLOAD_TIMEOUT', 'download_timeout', 0)
    try:
        DOWNLOAD_TIMEOUT = int(_dt)
    except Exception:
        DOWNLOAD_TIMEOUT = 0
    DOWNLOAD_TOKEN = _env_or_cfg('DOWNLOAD_TOKEN', 'download_token', None)

    # Return a dict of resolved values
    return {
        'TEMPLATE_FOLDER': TEMPLATE_FOLDER,
        'STATIC_FOLDER': STATIC_FOLDER,
        'TIDAL_DL_BIN': TIDAL_DL_BIN,
        'DOWNLOAD_TIMEOUT': DOWNLOAD_TIMEOUT,
        'DOWNLOAD_TOKEN': DOWNLOAD_TOKEN,
    }


# Resolve configuration (config file optional; environment variables override)
_cfg = _load_config()

TEMPLATE_FOLDER = _cfg['TEMPLATE_FOLDER']
STATIC_FOLDER = _cfg['STATIC_FOLDER']

app = Flask(__name__, template_folder=TEMPLATE_FOLDER, static_folder=STATIC_FOLDER)

# Configure logging (info and error only)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Configuration values already resolved by _load_config
TIDAL_DL_BIN = _cfg['TIDAL_DL_BIN']
DOWNLOAD_TIMEOUT = _cfg['DOWNLOAD_TIMEOUT']  # seconds; 0 = no timeout
DOWNLOAD_TOKEN = _cfg['DOWNLOAD_TOKEN']  # optional: if set, require header X-Download-Token

# Output is broadcast to every connected SSE client rather than consumed from
# a single shared queue, so multiple tabs/devices (or a page refresh) each
# see the full log instead of splitting it between whichever client happens
# to read a given line first.
HISTORY_MAXLEN = 500
output_history = deque(maxlen=HISTORY_MAXLEN)
subscribers = set()  # set of per-client queue.Queue, guarded by subscribers_lock
subscribers_lock = threading.Lock()

# current_process is protected by process_lock
current_process = None
process_lock = threading.Lock()

# TIDAL URLs only; keeps the wrapped binary from being pointed at arbitrary hosts
ALLOWED_URL_HOSTS = {"tidal.com", "www.tidal.com", "listen.tidal.com"}


def _broadcast(line):
    """Append a line to history and push it to every connected SSE subscriber."""
    with subscribers_lock:
        output_history.append(line)
        subs = list(subscribers)
    for q in subs:
        try:
            q.put_nowait(line)
        except queue.Full:
            # slow subscriber: drop its oldest buffered line to make room
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(line)
            except queue.Full:
                pass


def _terminate_process_group(process, grace_seconds=5):
    """Terminate a subprocess and its whole process group (e.g. ffmpeg
    children it spawned), escalating to SIGKILL if it doesn't exit in time."""
    if process is None or process.poll() is not None:
        return
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            os.killpg(pgid, signal.SIGKILL)
            process.wait()
    except ProcessLookupError:
        pass
    except Exception as e:
        logging.error(f"Error terminating process group: {e}")

# Validate binary at startup (log warnings but do not crash)
_resolved_bin = shutil.which(TIDAL_DL_BIN)
if not _resolved_bin:
    logging.warning(f"TIDAL_DL_BIN '{TIDAL_DL_BIN}' is not present or not executable. Please set env TIDAL_DL_BIN to a valid binary path.")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/tidal-dl/')
def index_tidal():
    return render_template('index.html')

@app.route('/favicon.ico')
@app.route('/tidal-dl/favicon.ico')
def favicon():
    # `app.static_folder` can be typed as `Optional[str]` by type-checkers;
    # cast to `str` here because we always provide a default static folder
    # in `_load_config()` and want to satisfy the type checker.
    return send_from_directory(cast(str, app.static_folder), 'favicon.ico')

def _check_token():
    """Return an (error_response, status) tuple if DOWNLOAD_TOKEN is set and
    missing/incorrect, else None."""
    if not DOWNLOAD_TOKEN:
        return None
    token = request.headers.get('X-Download-Token') or ''
    if not hmac.compare_digest(token, DOWNLOAD_TOKEN):
        logging.warning('Unauthorized request (invalid/missing token)')
        return {"error": "Unauthorized"}, 401
    return None


@app.route('/download', methods=['POST'])
@app.route('/tidal-dl/download', methods=['POST'])
def download():
    global current_process

    auth_error = _check_token()
    if auth_error:
        return auth_error

    url = request.form.get('url')
    if not url:
        logging.error("No URL provided")
        return {"error": "No URL provided"}, 400

    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ALLOWED_URL_HOSTS:
        logging.error(f"Invalid URL provided: {url}")
        return {"error": "Invalid URL provided"}, 400

    def run_download():
        global current_process
        process = None
        try:
            logging.info(f"Starting download for URL: {url}")
            with subscribers_lock:
                output_history.clear()

            # Run tidal-dl-ng. start_new_session=True makes it (and any
            # children it spawns, e.g. ffmpeg) the leader of its own process
            # group so Stop/timeout can terminate the whole tree, not just
            # the immediate process.
            command = [TIDAL_DL_BIN, 'dl', url]
            with process_lock:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
                current_process = process

            try:
                stdout = process.stdout
                if stdout is None:
                    logging.error("Process started without stdout capture")
                else:
                    for line in iter(stdout.readline, ''):
                        if line == '':
                            break
                        _broadcast(line)

                if DOWNLOAD_TIMEOUT and DOWNLOAD_TIMEOUT > 0:
                    try:
                        process.wait(timeout=DOWNLOAD_TIMEOUT)
                    except subprocess.TimeoutExpired:
                        _broadcast("Error: Command timed out")
                        logging.error("Command timed out")
                        _terminate_process_group(process)
                else:
                    process.wait()

                if process.returncode == 0:
                    _broadcast("Download completed successfully")
                    logging.info("Download completed successfully")
                else:
                    _broadcast(f"Error: Download failed (exit {process.returncode})")
                    logging.error(f"Download failed (exit {process.returncode})")
            finally:
                try:
                    if process.stdout:
                        process.stdout.close()
                except Exception:
                    pass
                with process_lock:
                    if current_process is process:
                        current_process = None
        except Exception as e:
            _broadcast(f"Error: {str(e)}")
            logging.error(f"Error in download: {str(e)}")
            with process_lock:
                if current_process is process:
                    current_process = None

    # Stop any existing process (safely) before starting the new one
    with process_lock:
        _terminate_process_group(current_process)
        current_process = None
    threading.Thread(target=run_download, daemon=True).start()
    return {"message": "Download started"}, 200

@app.route('/tidal-dl/stop', methods=['POST'])
def stop():
    auth_error = _check_token()
    if auth_error:
        return auth_error

    global current_process
    with process_lock:
        if current_process:
            _terminate_process_group(current_process)
            current_process = None
            _broadcast("Download stopped")
            logging.info("Download stopped")
            return {"message": "Download stopped"}, 200
    return {"message": "No download running"}, 200

@app.route('/tidal-dl/stream', methods=['GET'])
@app.route('/stream', methods=['GET'])
def stream():
    # Each connection gets its own queue and a replay of recent history, so
    # multiple tabs/devices (or a reconnect after a page refresh) each see
    # the full log instead of splitting one shared stream between them.
    client_queue = queue.Queue(maxsize=1000)
    with subscribers_lock:
        subscribers.add(client_queue)
        history_snapshot = list(output_history)

    def generate():
        try:
            yield ': keep-alive\n\n'
            for line in history_snapshot:
                yield f"data: {line}\n\n"
            while True:
                try:
                    line = client_queue.get(timeout=0.5)
                    yield f"data: {line}\n\n"
                except queue.Empty:
                    yield ': keep-alive\n\n'
        except GeneratorExit:
            logging.info("SSE stream closed")
        finally:
            with subscribers_lock:
                subscribers.discard(client_queue)

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive'}
    )

if __name__ == '__main__':
    host = os.environ.get('FLASK_HOST', '0.0.0.0')
    port = int(os.environ.get('FLASK_PORT', '5050'))
    app.run(host=host, port=port, debug=False)
