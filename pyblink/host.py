"""Chromium process owner and browser-level CDP session."""

import os
import shutil
import tempfile

from PyQt6.QtCore import QObject, pyqtSignal

from pyblink.cdp import CdpClient
from pyblink.chromium import launch_chromium, unused_port, wait_for_devtools
from pyblink.extensions import extension_paths


class BlinkDownload:
    """Download request that mirrors the QWebEngineDownloadRequest bits we use."""

    def __init__(self, host, guid, suggested, url):
        self._host = host
        self.guid = guid
        self._suggested = suggested or "download"
        self.url = url
        self._directory = os.path.join(os.path.expanduser("~"), "Downloads")
        self._filename = self._suggested
        self._accepted = False
        self._cancelled = False
        self._finished = False
        self._state = "pending"
        self._dest = ""
        self.isFinishedChanged = host.download_finished

    def downloadFileName(self):
        return self._suggested

    def cancel(self):
        self._cancelled = True
        self._state = "cancelled"
        try:
            self._host.cdp.call(
                "Browser.cancelDownload",
                {"guid": self.guid},
            )
        except Exception:
            pass

    def setDownloadDirectory(self, directory):
        self._directory = directory

    def setDownloadFileName(self, name):
        self._filename = name

    def accept(self):
        self._accepted = True
        dest = os.path.join(self._directory, self._filename)
        os.makedirs(self._directory, exist_ok=True)
        try:
            self._host.cdp.call(
                "Browser.setDownloadBehavior",
                {
                    "behavior": "allowAndName",
                    "downloadPath": self._directory,
                    "eventsEnabled": True,
                },
            )
        except Exception:
            pass
        self._dest = dest

    def state(self):
        return self._state

    def mark_finished(self, ok):
        self._finished = True
        self._state = "completed" if ok else "interrupted"


class BlinkHost(QObject):
    """One Chromium process + flattened CDP sessions for page targets."""

    download_requested = pyqtSignal(object)
    download_finished = pyqtSignal()
    event_received = pyqtSignal(str, object, str)
    crashed = pyqtSignal(str)

    def __init__(self, user_data_dir, parent=None, insecret=False):
        super().__init__(parent)
        self.user_data_dir = user_data_dir
        self.insecret = insecret
        self.proc = None
        self.port = None
        self.cdp = None
        self._downloads = {}
        self._owns_dir = insecret
        self._claimed = set()

    def start(self):
        if self.cdp is not None:
            return
        os.makedirs(self.user_data_dir, exist_ok=True)
        self.port = unused_port()
        paths = [] if self.insecret else extension_paths()
        self.proc = launch_chromium(self.user_data_dir, self.port, paths)
        info = wait_for_devtools(self.port, self.proc)
        ws_url = info.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError("Chromium did not expose a DevTools WebSocket")
        self.cdp = CdpClient(ws_url, self)
        self.cdp.event_received.connect(self._on_event)
        self.cdp.disconnected.connect(self._on_disconnect)
        self.cdp.start()
        self.cdp.call("Target.setDiscoverTargets", {"discover": True})
        self.cdp.call(
            "Browser.setDownloadBehavior",
            {
                "behavior": "allowAndName",
                "downloadPath": os.path.join(self.user_data_dir, "downloads"),
                "eventsEnabled": True,
            },
        )

    def restart(self):
        urls = []
        self.stop()
        self.start()
        return urls

    def stop(self):
        cdp = self.cdp
        self.cdp = None
        if cdp is not None:
            try:
                cdp.stop()
            except Exception:
                pass
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=4)
            except Exception:
                self.proc.kill()
        self.proc = None
        if self._owns_dir and self.user_data_dir and os.path.isdir(self.user_data_dir):
            shutil.rmtree(self.user_data_dir, ignore_errors=True)

    def acquire_target(self, url="about:blank"):
        try:
            listing = self.cdp.call("Target.getTargets", timeout=8)
        except Exception:
            listing = {}
        for info in listing.get("targetInfos") or []:
            tid = info.get("targetId")
            if info.get("type") == "page" and tid and tid not in self._claimed:
                self._claimed.add(tid)
                return tid
        return self.create_target(url)

    def create_target(self, url="about:blank"):
        result = self.cdp.call(
            "Target.createTarget",
            {"url": url or "about:blank"},
        )
        tid = result.get("targetId")
        if tid:
            self._claimed.add(tid)
        return tid

    def close_target(self, target_id):
        self._claimed.discard(target_id)
        if not self.cdp or not target_id:
            return
        try:
            self.cdp.call("Target.closeTarget", {"targetId": target_id}, timeout=5)
        except Exception:
            pass

    def attach(self, target_id):
        result = self.cdp.call(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
        )
        return result.get("sessionId")

    def call(self, method, params=None, session_id=None, timeout=15):
        return self.cdp.call(method, params, session_id, timeout)

    def send(self, method, params=None, session_id=None, callback=None):
        self.cdp.send(method, params, session_id, callback)

    def _on_event(self, method, params, session_id):
        if method == "Browser.downloadWillBegin":
            guid = params.get("guid")
            suggested = params.get("suggestedFilename") or "download"
            item = BlinkDownload(self, guid, suggested, params.get("url", ""))
            self._downloads[guid] = item
            self.download_requested.emit(item)
            return
        if method == "Browser.downloadProgress":
            guid = params.get("guid")
            item = self._downloads.get(guid)
            if item and params.get("state") in ("completed", "canceled", "interrupted"):
                item.mark_finished(params.get("state") == "completed")
                self.download_finished.emit()
            return
        self.event_received.emit(method, params, session_id)

    def _on_disconnect(self):
        if self.proc is not None and self.proc.poll() is not None:
            self.crashed.emit("Chromium process disconnected")


def make_insecret_dir():
    return tempfile.mkdtemp(prefix="blink-insecret-")
