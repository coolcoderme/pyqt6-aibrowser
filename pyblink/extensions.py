"""Chrome Web Store CRX download, unpack, and local install list."""

import io
import json
import os
import re
import shutil
import struct
import urllib.error
import urllib.request
import zipfile

EXTENSIONS_DIR_NAME = "extensions"
EXTENSIONS_FILE_NAME = "extensions.json"

STORE_ID_RE = re.compile(
    r"chromewebstore\.google\.com/(?:detail|extensions?)/"
    r"(?:[^/]+/)?([a-p]{32})",
    re.IGNORECASE,
)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "browser_data")
EXTENSIONS_DIR = os.path.join(DATA_DIR, EXTENSIONS_DIR_NAME)
EXTENSIONS_FILE = os.path.join(DATA_DIR, EXTENSIONS_FILE_NAME)


def parse_store_id(url):
    """Return the 32-char Chrome Web Store id from a detail URL, or None."""
    if not url:
        return None
    match = STORE_ID_RE.search(url)
    return match.group(1) if match else None


def _load_list():
    try:
        with open(EXTENSIONS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return []


def _save_list(items):
    os.makedirs(os.path.dirname(EXTENSIONS_FILE), exist_ok=True)
    with open(EXTENSIONS_FILE, "w", encoding="utf-8") as fh:
        json.dump(items, fh, indent=2)


def list_extensions():
    items = []
    for entry in _load_list():
        path = entry.get("path") or ""
        if path and os.path.isdir(path):
            items.append(entry)
    return items


def extension_paths():
    return [item["path"] for item in list_extensions() if item.get("path")]


def unpack_crx(data, dest_dir):
    """Extract a CRX2/CRX3 or raw zip payload into dest_dir."""
    if data[:4] == b"Cr24":
        if len(data) < 12:
            raise ValueError("Truncated CRX header")
        header_size = struct.unpack_from("<I", data, 8)[0]
        zip_start = 12 + header_size
        zip_data = data[zip_start:]
    elif data[:2] == b"PK":
        zip_data = data
    else:
        raise ValueError("Not a CRX or ZIP archive")
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(zip_data)) as archive:
        archive.extractall(dest_dir)


def _manifest_name(dest_dir):
    path = os.path.join(dest_dir, "manifest.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        return manifest.get("name") or os.path.basename(dest_dir)
    except (OSError, json.JSONDecodeError):
        return os.path.basename(dest_dir)


def download_crx(store_id, chrome_version="148.0.7778.96"):
    url = (
        "https://clients2.google.com/service/update2/crx"
        "?response=redirect&acceptformat=crx2,crx3"
        f"&prodversion={chrome_version}&x=id%3D{store_id}%26uc"
    )
    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"Mozilla/5.0 Chrome/{chrome_version}"},
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        return resp.read()


def install_from_store_url(url, chrome_version="148.0.7778.96"):
    """Download and unpack a CWS extension. Returns the installed record."""
    store_id = parse_store_id(url)
    if not store_id:
        raise ValueError("Not a Chrome Web Store extension page.")
    dest = os.path.join(EXTENSIONS_DIR, store_id)
    data = download_crx(store_id, chrome_version)
    unpack_crx(data, dest)
    name = _manifest_name(dest)
    record = {"id": store_id, "name": name, "path": dest}
    items = [item for item in _load_list() if item.get("id") != store_id]
    items.append(record)
    _save_list(items)
    return record


def remove_extension(store_id):
    items = [item for item in _load_list() if item.get("id") != store_id]
    _save_list(items)
    dest = os.path.join(EXTENSIONS_DIR, store_id)
    if os.path.isdir(dest):
        shutil.rmtree(dest, ignore_errors=True)
