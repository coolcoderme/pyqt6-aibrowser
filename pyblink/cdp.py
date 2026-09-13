"""Minimal WebSocket + Chrome DevTools Protocol client (stdlib only)."""

import base64
import hashlib
import json
import os
import queue
import socket
import ssl
import struct
import threading
import urllib.parse

from PyQt6.QtCore import QObject, pyqtSignal


def _mask_payload(payload):
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return mask + masked


def encode_ws_text(text):
    payload = text.encode("utf-8")
    header = bytearray([0x81])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    return bytes(header) + _mask_payload(payload)


def encode_ws_pong(payload=b""):
    header = bytearray([0x8A, 0x80 | len(payload)])
    if len(payload) >= 126:
        raise ValueError("pong payload too large")
    return bytes(header) + _mask_payload(payload)


class _FrameReader:
    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()

    def _recv_more(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("WebSocket closed")
        self.buf.extend(chunk)

    def read_frame(self):
        while True:
            if len(self.buf) < 2:
                self._recv_more()
                continue
            b0, b1 = self.buf[0], self.buf[1]
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            length = b1 & 0x7F
            idx = 2
            if length == 126:
                if len(self.buf) < 4:
                    self._recv_more()
                    continue
                length = struct.unpack("!H", self.buf[2:4])[0]
                idx = 4
            elif length == 127:
                if len(self.buf) < 10:
                    self._recv_more()
                    continue
                length = struct.unpack("!Q", self.buf[2:10])[0]
                idx = 10
            mask = b""
            if masked:
                if len(self.buf) < idx + 4:
                    self._recv_more()
                    continue
                mask = bytes(self.buf[idx : idx + 4])
                idx += 4
            if len(self.buf) < idx + length:
                self._recv_more()
                continue
            payload = bytes(self.buf[idx : idx + length])
            del self.buf[: idx + length]
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            return opcode, payload


def _connect_ws(ws_url):
    parsed = urllib.parse.urlparse(ws_url)
    host = parsed.hostname or "127.0.0.1"
    is_ssl = parsed.scheme == "wss"
    port = parsed.port or (443 if is_ssl else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    raw = socket.create_connection((host, port), timeout=15)
    raw.settimeout(None)
    sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host) if is_ssl else raw
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(req.encode("ascii"))
    header = b""
    while b"\r\n\r\n" not in header:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("No WebSocket handshake")
        header += chunk
    status_line = header.split(b"\r\n", 1)[0]
    if b"101" not in status_line:
        raise ConnectionError("WebSocket handshake failed: " + status_line.decode())
    accept = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    )
    if accept not in header:
        raise ConnectionError("WebSocket accept mismatch")
    leftover = header.split(b"\r\n\r\n", 1)[1]
    return sock, leftover


class CdpClient(QObject):
    """JSON-RPC CDP client running its socket loop on a worker thread."""

    event_received = pyqtSignal(str, object, str)
    disconnected = pyqtSignal()

    def __init__(self, ws_url, parent=None):
        super().__init__(parent)
        self._ws_url = ws_url
        self._out = queue.Queue()
        self._pending = {}
        self._lock = threading.Lock()
        self._next_id = 1
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._alive = True
        self._ready = threading.Event()
        self._connect_error = None

    def start(self):
        self._thread.start()
        if not self._ready.wait(12):
            raise TimeoutError("CDP WebSocket did not connect")
        if self._connect_error is not None:
            raise ConnectionError(str(self._connect_error))

    def stop(self):
        self._alive = False
        self._out.put(None)

    def call(self, method, params=None, session_id=None, timeout=15):
        msg_id, event = self._submit(method, params, session_id)
        if not event.wait(timeout):
            with self._lock:
                self._pending.pop(msg_id, None)
            raise TimeoutError(f"CDP timeout: {method}")
        with self._lock:
            box = self._pending.pop(msg_id, {})
        if "error" in box:
            err = box["error"]
            raise RuntimeError(f"{method}: {err.get('message', err)}")
        return box.get("result", {})

    def send(self, method, params=None, session_id=None, callback=None):
        self._submit(method, params, session_id, callback=callback)

    def _submit(self, method, params, session_id, callback=None):
        event = threading.Event()
        with self._lock:
            msg_id = self._next_id
            self._next_id += 1
            self._pending[msg_id] = {"event": event, "callback": callback}
        payload = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        self._out.put(json.dumps(payload, separators=(",", ":")))
        return msg_id, event

    def _loop(self):
        sock = None
        try:
            sock, leftover = _connect_ws(self._ws_url)
            reader = _FrameReader(sock)
            reader.buf.extend(leftover)
            sock.settimeout(0.25)
            self._ready.set()
            while self._alive:
                try:
                    raw = self._out.get(timeout=0.05)
                except queue.Empty:
                    raw = False
                if raw is None:
                    break
                if raw:
                    sock.sendall(encode_ws_text(raw))
                try:
                    opcode, payload = reader.read_frame()
                except socket.timeout:
                    continue
                except (ConnectionError, OSError):
                    break
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    sock.sendall(encode_ws_pong(payload))
                    continue
                if opcode != 0x1:
                    continue
                try:
                    message = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                self._handle_message(message)
        except Exception as exc:
            self._connect_error = exc
        finally:
            self._ready.set()
            self._alive = False
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            self.disconnected.emit()

    def _handle_message(self, message):
        if "id" in message:
            with self._lock:
                box = self._pending.get(message["id"])
            if not box:
                return
            if "error" in message:
                box["error"] = message["error"]
            else:
                box["result"] = message.get("result", {})
            callback = box.get("callback")
            box["event"].set()
            if callback is not None:
                callback(box.get("result"), box.get("error"))
            return
        method = message.get("method")
        if not method:
            return
        params = message.get("params") or {}
        session_id = message.get("sessionId") or params.get("sessionId") or ""
        self.event_received.emit(method, params, session_id)
