import os
import subprocess
import shutil
import time
import threading
import queue
import logging
from pathlib import Path
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

# Store output in a bounded queue for streaming (prevents unbounded memory growth)
output_queue = queue.Queue(maxsize=2000)
# current_process is protected by process_lock
current_process = None
process_lock = threading.Lock()

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

@app.route('/download', methods=['POST'])
@app.route('/tidal-dl/download', methods=['POST'])
def download():
    global current_process
    # optional token auth: if DOWNLOAD_TOKEN is set, require header
    if DOWNLOAD_TOKEN:
        token = request.headers.get('X-Download-Token')
        if token != DOWNLOAD_TOKEN:
            logging.warning('Unauthorized download attempt (invalid/missing token)')
            return ({"error": "Unauthorized"}, 401)
    url = request.form.get('url')
    if not url:
        logging.error("No URL provided")
        return {"error": "No URL provided"}, 400

    url = url.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        logging.error(f"Invalid URL provided: {url}")
        return {"error": "Invalid URL provided"}, 400

    def run_download():
        global current_process
        try:
            logging.info(f"Starting download for URL: {url}")
            # Clear previous output (non-atomic but reasonable)
            try:
                while True:
                    output_queue.get_nowait()
            except queue.Empty:
                pass

            # Run tidal-dl-ng
            command = [TIDAL_DL_BIN, 'dl', url]
            process = None
            with process_lock:
                # Start process and register it as the current process
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                current_process = process

            # Read lines robustly
            try:
                stdout = process.stdout
                if stdout is None:
                    logging.error("Process started without stdout capture")
                else:
                    for line in iter(stdout.readline, ''):
                        if line == '':
                            break
                        # best-effort non-blocking put: drop oldest if full
                        try:
                            output_queue.put(line, timeout=0.5)
                        except queue.Full:
                            try:
                                _ = output_queue.get_nowait()
                            except queue.Empty:
                                pass
                            try:
                                output_queue.put_nowait(line)
                            except queue.Full:
                                # if still full, discard
                                pass

                # Wait for completion (configurable)
                if DOWNLOAD_TIMEOUT and DOWNLOAD_TIMEOUT > 0:
                    try:
                        process.wait(timeout=DOWNLOAD_TIMEOUT)
                    except subprocess.TimeoutExpired:
                        output_queue.put("Error: Command timed out")
                        logging.error("Command timed out")
                        # attempt graceful termination below
                else:
                    process.wait()

                if process.returncode == 0:
                    output_queue.put("Download completed successfully")
                    logging.info("Download completed successfully")
                else:
                    output_queue.put(f"Error: Download failed (exit {process.returncode})")
                    logging.error(f"Download failed (exit {process.returncode})")
            finally:
                # Ensure process pipe is closed
                try:
                    if process and process.stdout:
                        process.stdout.close()
                except Exception:
                    pass
                with process_lock:
                    if current_process is process:
                        current_process = None
        except subprocess.TimeoutExpired:
            output_queue.put("Error: Command timed out")
            logging.error("Command timed out")
            # Attempt graceful termination
            try:
                if process:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            except Exception as e:
                logging.error(f"Error terminating process after timeout: {e}")
            with process_lock:
                if current_process is process:
                    current_process = None
        except Exception as e:
            output_queue.put(f"Error: {str(e)}")
            logging.error(f"Error in download: {str(e)}")
            with process_lock:
                if current_process is process:
                    current_process = None

    # Stop any existing process (safely)
    with process_lock:
        if current_process:
            try:
                current_process.terminate()
                try:
                    current_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    current_process.kill()
                    current_process.wait()
            except Exception as e:
                logging.error(f"Error stopping previous process: {e}")
            current_process = None
    # Start new download
    threading.Thread(target=run_download, daemon=True).start()
    return {"message": "Download started"}, 200

@app.route('/tidal-dl/stop', methods=['POST'])
def stop():
    if DOWNLOAD_TOKEN:
        token = request.headers.get('X-Download-Token')
        if token != DOWNLOAD_TOKEN:
            logging.warning('Unauthorized stop attempt (invalid/missing token)')
            return ({"error": "Unauthorized"}, 401)

    global current_process
    with process_lock:
        if current_process:
            try:
                current_process.terminate()
                try:
                    current_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    current_process.kill()
                    current_process.wait()
            except Exception as e:
                logging.error(f"Error stopping process: {e}")
            current_process = None
            output_queue.put("Download stopped")
            logging.info("Download stopped")
            return {"message": "Download stopped"}, 200
    return {"message": "No download running"}, 200

@app.route('/tidal-dl/stream', methods=['GET'])
@app.route('/stream', methods=['GET'])
def stream():
    def generate():
        try:
            yield ': keep-alive\n\n'
            while True:
                try:
                    line = output_queue.get(timeout=0.5)
                    yield f"data: {line}\n\n"
                except queue.Empty:
                    yield ': keep-alive\n\n'
                    time.sleep(0.5)
        except GeneratorExit:
            logging.info("SSE stream closed")

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive'}
    )

if __name__ == '__main__':
    host = os.environ.get('FLASK_HOST', '0.0.0.0')
    port = int(os.environ.get('FLASK_PORT', '5050'))
    app.run(host=host, port=port, debug=False)
