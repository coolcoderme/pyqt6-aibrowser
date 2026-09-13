"""QWidget that paints a Chromium target via CDP screencast."""

# Qt override names (paintEvent, setUrl, …) are not snake_case.
# pylint: disable=invalid-name,unused-argument

from PyQt6.QtCore import QByteArray, QTimer, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QWidget

from pyblink.host import BlinkHost


class BlinkPage:
    """Small stand-in for QWebEnginePage (runJavaScript + inspector hook)."""

    def __init__(self, view):
        self._view = view
        self._devtools = None

    def runJavaScript(self, script, callback=None):
        self._view.run_javascript(script, callback)

    def setDevToolsPage(self, page):
        self._devtools = page


class BlinkView(QWidget):
    """Tab contents: Blink page rendered into this widget."""

    urlChanged = pyqtSignal(QUrl)
    titleChanged = pyqtSignal(str)
    loadStarted = pyqtSignal()
    loadProgress = pyqtSignal(int)
    loadFinished = pyqtSignal(bool)

    def __init__(self, host: BlinkHost, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.host = host
        self.target_id = None
        self.session_id = None
        self._url = "about:blank"
        self._title = ""
        self._frame = None
        self._page = BlinkPage(self)
        self._closing = False
        self._pending_html = None
        self.manual_group = None
        self._dino_page = False
        self._cast_timer = QTimer(self)
        self._cast_timer.setSingleShot(True)
        self._cast_timer.timeout.connect(self._sync_viewport)
        host.event_received.connect(self._on_host_event)
        self.attach_target()

    def page(self):
        return self._page

    def settings(self):
        return self

    def setAttribute(self, *_args, **_kwargs):
        return None

    def url(self):
        return QUrl(self._url)

    def title(self):
        return self._title

    def setUrl(self, url):
        if isinstance(url, QUrl):
            text = url.toString()
        else:
            text = str(url or "")
        if not text:
            text = "about:blank"
        self._url = text
        self._dino_page = False
        self.loadStarted.emit()
        self.loadProgress.emit(8)
        try:
            self.host.call("Page.navigate", {"url": text}, self.session_id)
        except Exception:
            self.loadFinished.emit(False)

    def setHtml(self, html, base_url=None):
        self._pending_html = html
        base = base_url.toString() if isinstance(base_url, QUrl) else (base_url or "about:blank")
        self._url = base or "about:blank"
        try:
            self.host.call("Page.navigate", {"url": "about:blank"}, self.session_id)
        except Exception:
            self._apply_html(html)

    def back(self):
        self._history_delta(-1)

    def forward(self):
        self._history_delta(1)

    def reload(self):
        self.loadStarted.emit()
        try:
            self.host.call("Page.reload", {}, self.session_id)
        except Exception:
            self.loadFinished.emit(False)

    def shutdown(self):
        self._closing = True
        if self.target_id:
            self.host.close_target(self.target_id)
        self.target_id = None
        self.session_id = None

    def run_javascript(self, script, callback=None):
        def done(result, error):
            if callback is None:
                return
            if error:
                QTimer.singleShot(0, lambda: callback(None))
                return
            value = None
            if isinstance(result, dict):
                inner = result.get("result") or {}
                value = inner.get("value")
            QTimer.singleShot(0, lambda: callback(value))

        try:
            self.host.send(
                "Runtime.evaluate",
                {
                    "expression": script,
                    "returnByValue": True,
                    "awaitPromise": False,
                },
                self.session_id,
                callback=done,
            )
        except Exception:
            if callback is not None:
                callback(None)

    def open_devtools(self):
        try:
            self.host.call("Target.activateTarget", {"targetId": self.target_id})
            self.host.send(
                "Runtime.evaluate",
                {"expression": "debugger", "returnByValue": True},
                self.session_id,
            )
        except Exception:
            pass

    def attach_target(self, url="about:blank"):
        self.target_id = self.host.acquire_target(url)
        self.session_id = self.host.attach(self.target_id)
        for method in (
            "Page.enable",
            "Runtime.enable",
            "Network.enable",
            "Page.setLifecycleEventsEnabled",
        ):
            params = {"enabled": True} if method.endswith("Enabled") else {}
            try:
                self.host.call(method, params, self.session_id, timeout=8)
            except Exception:
                pass
        self._sync_viewport()

    def _sync_viewport(self):
        if self._closing or not self.session_id:
            return
        width = max(self.width(), 320)
        height = max(self.height(), 200)
        try:
            self.host.send(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": width,
                    "height": height,
                    "deviceScaleFactor": 1,
                    "mobile": False,
                },
                self.session_id,
            )
            self.host.send(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": 55,
                    "maxWidth": width,
                    "maxHeight": height,
                    "everyNthFrame": 1,
                },
                self.session_id,
            )
        except Exception:
            pass

    def _history_delta(self, delta):
        try:
            hist = self.host.call("Page.getNavigationHistory", {}, self.session_id)
        except Exception:
            return
        index = hist.get("currentIndex", 0) + delta
        entries = hist.get("entries") or []
        if 0 <= index < len(entries):
            try:
                self.host.call(
                    "Page.navigateToHistoryEntry",
                    {"entryId": entries[index]["id"]},
                    self.session_id,
                )
            except Exception:
                pass

    def _apply_html(self, html):
        try:
            self.host.call(
                "Page.setDocumentContent",
                {"html": html, "frameId": self._main_frame_id()},
                self.session_id,
            )
        except Exception:
            try:
                self.host.call(
                    "Runtime.evaluate",
                    {
                        "expression": "document.open();document.write(%s);document.close();"
                        % _js_string(html),
                        "returnByValue": True,
                    },
                    self.session_id,
                )
            except Exception:
                pass

    def _main_frame_id(self):
        try:
            tree = self.host.call("Page.getFrameTree", {}, self.session_id)
            return ((tree.get("frameTree") or {}).get("frame") or {}).get("id")
        except Exception:
            return None

    def _on_host_event(self, method, params, session_id):
        if self._closing or (session_id and session_id != self.session_id):
            return
        if method == "Page.screencastFrame":
            self._on_frame(params)
        elif method == "Page.frameNavigated":
            frame = params.get("frame") or {}
            if frame.get("parentId"):
                return
            url = frame.get("url") or self._url
            if url.startswith("http") or url in ("about:blank",) or url.startswith("data:"):
                self._url = url
                self.urlChanged.emit(QUrl(url))
        elif method == "Page.navigatedWithinDocument":
            url = params.get("url")
            if url:
                self._url = url
                self.urlChanged.emit(QUrl(url))
        elif method == "Page.loadEventFired":
            self.loadProgress.emit(100)
            self.loadFinished.emit(True)
            if self._pending_html:
                html = self._pending_html
                self._pending_html = None
                self._apply_html(html)
            self._refresh_title()
        elif method == "Page.javascriptDialogOpening":
            try:
                self.host.send(
                    "Page.handleJavaScriptDialog",
                    {"accept": True},
                    self.session_id,
                )
            except Exception:
                pass
        elif method == "Target.targetCrashed":
            self.loadFinished.emit(False)

    def _refresh_title(self):
        def done(result, _error):
            value = None
            if isinstance(result, dict):
                value = (result.get("result") or {}).get("value")
            if isinstance(value, str) and value != self._title:
                self._title = value
                self.titleChanged.emit(value)

        self.host.send(
            "Runtime.evaluate",
            {"expression": "document.title || ''", "returnByValue": True},
            self.session_id,
            callback=done,
        )

    def _on_frame(self, params):
        data = params.get("data")
        session_id = params.get("sessionId")
        if session_id is not None:
            try:
                self.host.send(
                    "Page.screencastFrameAck",
                    {"sessionId": session_id},
                    self.session_id,
                )
            except Exception:
                pass
        if not data:
            return
        raw = QByteArray.fromBase64(data.encode("ascii") if isinstance(data, str) else data)
        image = QImage.fromData(raw, "JPG")
        if image.isNull():
            return
        self._frame = image
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.white)
        if self._frame is not None:
            painter.drawImage(self.rect(), self._frame)
        painter.end()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._cast_timer.start(120)

    def showEvent(self, event):
        super().showEvent(event)
        self._cast_timer.start(80)

    def mousePressEvent(self, event):
        self.setFocus()
        self._mouse(event, "mousePressed")

    def mouseReleaseEvent(self, event):
        self._mouse(event, "mouseReleased")

    def mouseMoveEvent(self, event):
        self._mouse(event, "mouseMoved")

    def wheelEvent(self, event):
        delta = event.angleDelta()
        self._dispatch(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseWheel",
                "x": event.position().x(),
                "y": event.position().y(),
                "deltaX": delta.x(),
                "deltaY": delta.y(),
            },
        )

    def keyPressEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            event.ignore()
            return
        self._key(event, down=True)

    def keyReleaseEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            event.ignore()
            return
        self._key(event, down=False)

    def _mouse(self, event, kind):
        button = {
            Qt.MouseButton.LeftButton: "left",
            Qt.MouseButton.RightButton: "right",
            Qt.MouseButton.MiddleButton: "middle",
        }.get(event.button(), "none")
        params = {
            "type": kind,
            "x": event.position().x(),
            "y": event.position().y(),
            "button": button if kind != "mouseMoved" else "none",
            "clickCount": 1 if kind != "mouseMoved" else 0,
            "modifiers": _qt_modifiers(event.modifiers()),
        }
        self._dispatch("Input.dispatchMouseEvent", params)

    def _key(self, event, down):
        key, code, vk, text = _qt_key(event)
        payload = {
            "type": "keyDown" if down else "keyUp",
            "key": key,
            "code": code,
            "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk,
            "modifiers": _qt_modifiers(event.modifiers()),
        }
        if down and text:
            payload["text"] = text
            payload["unmodifiedText"] = text
        self._dispatch("Input.dispatchKeyEvent", payload)
        if down and text and text.isprintable() and not event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
        ):
            char = dict(payload)
            char["type"] = "char"
            self._dispatch("Input.dispatchKeyEvent", char)

    def _dispatch(self, method, params):
        if not self.session_id or self._closing:
            return
        try:
            self.host.send(method, params, self.session_id)
        except Exception:
            pass


def _js_string(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _qt_modifiers(mods):
    value = 0
    if mods & Qt.KeyboardModifier.AltModifier:
        value |= 1
    if mods & Qt.KeyboardModifier.ControlModifier:
        value |= 2
    if mods & Qt.KeyboardModifier.MetaModifier:
        value |= 4
    if mods & Qt.KeyboardModifier.ShiftModifier:
        value |= 8
    return value


_SPECIAL = {
    Qt.Key.Key_Return: ("Enter", "Enter", 13),
    Qt.Key.Key_Enter: ("Enter", "Enter", 13),
    Qt.Key.Key_Backspace: ("Backspace", "Backspace", 8),
    Qt.Key.Key_Tab: ("Tab", "Tab", 9),
    Qt.Key.Key_Escape: ("Escape", "Escape", 27),
    Qt.Key.Key_Left: ("ArrowLeft", "ArrowLeft", 37),
    Qt.Key.Key_Up: ("ArrowUp", "ArrowUp", 38),
    Qt.Key.Key_Right: ("ArrowRight", "ArrowRight", 39),
    Qt.Key.Key_Down: ("ArrowDown", "ArrowDown", 40),
    Qt.Key.Key_Delete: ("Delete", "Delete", 46),
    Qt.Key.Key_Home: ("Home", "Home", 36),
    Qt.Key.Key_End: ("End", "End", 35),
    Qt.Key.Key_PageUp: ("PageUp", "PageUp", 33),
    Qt.Key.Key_PageDown: ("PageDown", "PageDown", 34),
    Qt.Key.Key_Space: (" ", "Space", 32),
}


def _qt_key(event):
    spec = _SPECIAL.get(event.key())
    if spec:
        key, code, vk = spec
        text = event.text() or (key if len(key) == 1 else "")
        return key, code, vk, text
    text = event.text() or ""
    key = text or chr(event.key()) if event.key() < 256 else text
    code = f"Key{key.upper()}" if key and key.isalpha() else key
    vk = event.key() if event.key() < 256 else 0
    return key, code, vk, text
