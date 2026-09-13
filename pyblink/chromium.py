"""Locate and launch a Chromium/Chrome binary for CDP."""

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request

CHROME_CANDIDATES = (
    os.environ.get("CHROME_PATH"),
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "/usr/local/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)


def find_chromium():
    """Return the path to a Chrome/Chromium binary or raise FileNotFoundError."""
    for candidate in CHROME_CANDIDATES:
        if not candidate:
            continue
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise FileNotFoundError(
        "Google Chrome or Chromium was not found. Install Chrome or set CHROME_PATH."
    )


def unused_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def launch_chromium(user_data_dir, port, extension_paths=None, extra_args=None):
    """Start Chromium with remote debugging. Returns the Popen handle."""
    binary = find_chromium()
    os.makedirs(user_data_dir, exist_ok=True)
    cmd = [
        binary,
        f"--user-data-dir={user_data_dir}",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        "--headless=new",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-background-networking",
        "--disable-default-apps",
        "--disable-hang-monitor",
        "--disable-popup-blocking",
        "--disable-prompt-on-repost",
        "--disable-features=TranslateUI,MediaRouter",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--password-store=basic",
        "--use-mock-keychain",
        "--window-size=1280,800",
        "about:blank",
    ]
    if extension_paths:
        joined = ",".join(extension_paths)
        cmd.insert(-1, f"--load-extension={joined}")
        cmd.insert(-1, f"--disable-extensions-except={joined}")
    if extra_args:
        cmd[1:1] = list(extra_args)
    env = os.environ.copy()
    env.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
    )


def wait_for_devtools(port, proc, timeout=20):
    """Block until Chromium answers /json/version. Returns the JSON dict."""
    url = f"http://127.0.0.1:{port}/json/version"
    deadline = time.monotonic() + timeout
    last_err = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            err = (proc.stderr.read() if proc.stderr else b"") or b""
            raise RuntimeError(
                "Chromium exited before DevTools started: "
                + err.decode("utf-8", "replace")[-500:]
            )
        try:
            with urllib.request.urlopen(url, timeout=0.4) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last_err = exc
            time.sleep(0.15)
    raise TimeoutError(f"Chromium DevTools did not start on port {port}: {last_err}")
