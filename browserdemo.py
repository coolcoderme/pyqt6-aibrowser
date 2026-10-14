"""A tabbed web browser with a Python Blink (Chromium) engine.

Features: tabs, automatic and manual tab groups by site type, navigation,
smart address bar, WebBoxes, browsing history, a library sidebar, download
handling, Chrome Web Store extensions, Insecret windows, and an optional
LLM-powered browsing agent.

The window is custom-decorated (Chrome-style): the tab strip lives in the
title-bar area with system caption buttons, and the tab strip can be docked
to any edge of the window.
"""

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime

from PyQt6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRect,
    QSettings,
    Qt,
    QThread,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import QAction, QColor, QCursor, QFont, QIcon, QKeySequence, QPainter, QPen
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTabBar,
    QTabWidget,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from pyblink import (
    BlinkHost,
    BlinkView,
    install_from_store_url,
    list_extensions,
    parse_store_id,
    remove_extension,
)
from pyblink.host import make_insecret_dir

HOME_URL = "https://www.google.com"
WEBBOX_FAVORITE = "favorite"
WEBBOX_MISC = "miscellaneous"

SEARCH_ENGINES = {
    "google": {
        "label": "Google",
        "home": "https://www.google.com",
        "search": "https://www.google.com/search?q={query}",
    },
    "duckduckgo": {
        "label": "DuckDuckGo",
        "home": "https://duckduckgo.com",
        "search": "https://duckduckgo.com/?q={query}",
    },
    "bing": {
        "label": "Bing",
        "home": "https://www.bing.com",
        "search": "https://www.bing.com/search?q={query}",
    },
    "yahoo": {
        "label": "Yahoo",
        "home": "https://www.yahoo.com",
        "search": "https://search.yahoo.com/search?p={query}",
    },
    "ecosia": {
        "label": "Ecosia",
        "home": "https://www.ecosia.org",
        "search": "https://www.ecosia.org/search?q={query}",
    },
    "startpage": {
        "label": "Startpage",
        "home": "https://www.startpage.com",
        "search": "https://www.startpage.com/sp/search?query={query}",
    },
    "brave": {
        "label": "Brave",
        "home": "https://search.brave.com",
        "search": "https://search.brave.com/search?q={query}",
    },
}
SEARCH_ENGINE_ORDER = [
    "google",
    "duckduckgo",
    "bing",
    "yahoo",
    "ecosia",
    "startpage",
    "brave",
]

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser_data")
BOOKMARKS_FILE = os.path.join(DATA_DIR, "bookmarks.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
PROFILE_DIR = os.path.join(DATA_DIR, "profile")
BLINK_PROFILE_DIR = os.path.join(DATA_DIR, "blink_profile")

APP_ORG_NAME = "PyQt6Browser"
APP_NAME = "browserdemo"
TAB_STRIP_POSITIONS = ("top", "left", "right", "bottom")
TAB_STRIP_LABELS = {
    "top": "Top",
    "left": "Left",
    "right": "Right",
    "bottom": "Bottom",
}
RESIZE_MARGIN = 6
DOCK_ZONE_MIN = 56

MAX_HISTORY = 1000
AGENT_MAX_STEPS = 10

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
FEATHERLESS_URL = "https://api.featherless.ai/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_AZURE_API_VERSION = "2024-02-15-preview"
ANTHROPIC_VERSION = "2023-06-01"

# Supported LLM providers and their default models. "openai", "groq" and
# "openrouter" share the OpenAI-compatible chat-completions schema; "azure"
# uses that schema on an Azure endpoint; "google" uses the Gemini REST API;
# "anthropic" uses the Claude Messages API.
PROVIDERS = {
    "openai": {"label": "OpenAI", "default_model": "gpt-4o-mini"},
    "anthropic": {"label": "Anthropic", "default_model": "claude-3-5-sonnet-latest"},
    "google": {"label": "Google AI", "default_model": "gemini-1.5-flash"},
    "groq": {"label": "Groq", "default_model": "llama-3.3-70b-versatile"},
    "openrouter": {"label": "OpenRouter", "default_model": "openai/gpt-4o-mini"},
    "featherless": {
        "label": "Featherless",
        "default_model": "Qwen/Qwen2.5-7B-Instruct",
    },
    "azure": {"label": "Azure OpenAI", "default_model": "gpt-4o-mini"},
    "cursor": {"label": "Cursor SDK", "default_model": "auto"},
}
PROVIDER_ORDER = [
    "openai",
    "anthropic",
    "google",
    "groq",
    "openrouter",
    "featherless",
    "azure",
    "cursor",
]
DEFAULT_PROVIDER = "openai"

# Scratch working directory for the Cursor SDK local agent. Kept empty/isolated
# so the agent never touches the browser's own source files.
CURSOR_SCRATCH_DIR = os.path.join(DATA_DIR, "cursor_scratch")

# The Cursor SDK launches a local bridge that uses asyncio subprocesses/sockets,
# which fails with WinError 10038 when driven from a Qt worker thread on Windows.
# We therefore run it in a dedicated child process (which gets a proper
# main-thread event loop). Config is read from argv[1]; result written to argv[2].
CURSOR_HELPER = r"""
import codecs, json, os, sys, time


def _install_windows_bridge_patch():
    # cursor-sdk's bridge reads its discovery line from a subprocess PIPE using
    # selectors.select(), but on Windows select() only works on sockets, raising
    # WinError 10038. The os.read() non-blocking path works fine, so we swap the
    # selector-based wait for a busy-poll. No-op on non-Windows platforms.
    if sys.platform != "win32":
        return
    import cursor_sdk._bridge as bridge

    def _read_discovery(process, timeout):
        if process.stderr is None:
            raise bridge.CursorSDKError("Bridge process stderr is unavailable")
        fd = process.stderr.fileno()
        was_blocking = os.get_blocking(fd)
        os.set_blocking(fd, False)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        deadline = time.monotonic() + timeout
        lines, pending = [], ""

        def drain():
            nonlocal pending
            while True:
                try:
                    chunk = os.read(fd, 8192)
                except (BlockingIOError, OSError):
                    return None
                if not chunk:
                    return None
                pending += decoder.decode(chunk)
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    line += "\n"
                    lines.append(line)
                    found = bridge.parse_discovery_line(line)
                    if found is not None:
                        return found

        try:
            while time.monotonic() < deadline:
                found = drain()
                if found is not None:
                    return found
                code = process.poll()
                if code is not None:
                    found = drain()
                    if found is not None:
                        return found
                    raise bridge.CursorSDKError(
                        "Bridge exited before discovery with status "
                        + str(code) + ": " + "".join(lines) + pending
                    )
                time.sleep(0.05)
            raise bridge.CursorSDKError("Timed out waiting for bridge discovery")
        finally:
            os.set_blocking(fd, was_blocking)

    bridge._read_discovery = _read_discovery


_install_windows_bridge_patch()

from cursor_sdk import Agent, AgentOptions, LocalAgentOptions

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    cfg = json.load(fh)
result = Agent.prompt(
    cfg["prompt"],
    AgentOptions(
        api_key=cfg.get("api_key") or None,
        model=cfg.get("model") or "auto",
        local=LocalAgentOptions(cwd=cfg["cwd"]),
    ),
)
text = result.result if result.result is not None else ""
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    json.dump({"result": text}, fh)
"""

ROLE_DATA = Qt.ItemDataRole.UserRole

CHROME_WEB_STORE_URL = "https://chromewebstore.google.com/"

# Shown when a page fails to load (network, SSL, timeout, etc.). Not always
# "no internet" — the site may be down or blocked. Includes a dino mini-game.
# "%%URL%%" is replaced with the failed address.
OFFLINE_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Site can't be reached</title>
<style>
  * { box-sizing: border-box; }
  body { background:#fff; color:#5f6368; font-family:'Segoe UI',Arial,sans-serif;
         margin:0; padding:28px 20px; transition:background .3s,color .3s; }
  body.night { background:#202124; color:#9aa0a6; }
  .wrap { max-width:960px; margin:0 auto; }
  h1 { color:#202124; font-weight:500; font-size:28px; margin:0 0 10px; }
  body.night h1 { color:#e8eaed; }
  ul { line-height:1.7; padding-left:20px; margin:12px 0; }
  code { color:#80868b; word-break:break-all; }
  .err { margin-top:14px; color:#80868b; font-size:13px; }
  a.retry { display:inline-block; margin-top:14px; color:#1a73e8;
            text-decoration:none; border:1px solid #dadce0;
            padding:8px 18px; border-radius:4px; }
  body.night a.retry { color:#8ab4f8; border-color:#5f6368; }
  a.retry:hover { background:#f1f3f4; }
  body.night a.retry:hover { background:#303134; }
  #gameBox { margin:18px 0 8px; position:relative; width:100%; }
  canvas { display:block; width:100%; max-width:960px; height:auto;
           background:#fff; outline:3px solid #dadce0; cursor:pointer;
           border-radius:4px; image-rendering:pixelated; touch-action:none; }
  body.night canvas { background:#202124; outline-color:#5f6368; }
  .hint { font-size:13px; color:#80868b; margin-top:6px; }
</style>
</head>
<body tabindex="-1">
<div class="wrap">
  <h1>This site can&rsquo;t be reached</h1>
  <p>Could not load <code id="u"></code></p>
  <ul>
    <li>Check that the address is correct</li>
    <li>Try again in a moment &mdash; the site may be down</li>
    <li>Your internet may still be working; this page failed for another reason</li>
  </ul>
  <a class="retry" id="retry">Reload</a>
  <p class="err">ERR_CONNECTION_FAILED &mdash; play while you wait:</p>
  <div id="gameBox">
    <canvas id="g" width="960" height="280" tabindex="0"></canvas>
  </div>
  <div class="hint">Press Space / &uarr; to jump &nbsp;&middot;&nbsp; &darr; to duck &nbsp;&middot;&nbsp; (click game if keys do nothing)</div>
</div>
<script>
(function () {
  var url = "%%URL%%";
  document.getElementById('u').textContent = url || '(page)';
  document.getElementById('retry').href = url || '#';

  var canvas = document.getElementById('g');
  var gameBox = document.getElementById('gameBox');
  var ctx = canvas.getContext('2d');
  var W = canvas.width, H = canvas.height;

  // ---------- tunables (Chrome dino, scaled up) ----------
  var GROUND_Y = H - 36;        // feet baseline
  var GRAVITY = 0.9;
  var JUMP_V = -16.5;
  var BASE_SPEED = 7;
  var MAX_SPEED = 18;
  var ACCEL = 0.0013;
  var TREX = { x: 70, w: 88, h: 94, duckW: 118, duckH: 60 };

  var state;                    // 'waiting' | 'running' | 'over'
  var trex, obstacles, clouds, ground, speed, distance, hi, frame;
  var night, lastNightToggle, nextObs;

  function loadHi() {
    try { return parseInt(localStorage.getItem('dinoHi') || '0', 10) || 0; }
    catch (e) { return 0; }
  }
  function saveHi(v) { try { localStorage.setItem('dinoHi', String(v)); } catch (e) {} }

  function INK() { return night ? '#f7f7f7' : '#535353'; }
  function PAPER() { return night ? '#202124' : '#ffffff'; }
  function pad5(n) { return ('00000' + n).slice(-5); }
  function curScore() { return Math.floor(distance / 10); }

  function focusGame() {
    try { canvas.focus({ preventScroll: true }); }
    catch (e) { try { canvas.focus(); } catch (_) {} }
  }

  function reset() {
    state = 'waiting';
    trex = { x: TREX.x, y: GROUND_Y, vy: 0, onGround: true, duck: false };
    obstacles = [];
    clouds = [];
    ground = 0;
    speed = BASE_SPEED;
    distance = 0;
    hi = loadHi();
    frame = 0;
    night = false;
    lastNightToggle = 0;
    nextObs = 60;
    document.body.classList.remove('night');
    for (var i = 0; i < 3; i++) {
      clouds.push({ x: 220 + i * 320, y: 26 + Math.random() * 60 });
    }
    focusGame();
  }

  // ---------- input actions (also called from the Python key bridge) ----------
  function action() {
    if (state === 'over') { reset(); state = 'running'; nextObs = 40; jumpNow(); return; }
    if (state === 'waiting') { state = 'running'; nextObs = 40; }
    jumpNow();
  }
  function jumpNow() {
    if (state === 'running' && trex.onGround) {
      trex.vy = JUMP_V;
      trex.onGround = false;
      trex.duck = false;
    }
  }
  function setDuck(on) {
    if (state !== 'running') return;
    trex.duck = !!on;
    if (on && !trex.onGround) trex.vy += 2.6;   // fast-fall
  }
  window.dinoJump = action;
  window.dinoAction = action;
  window.dinoDuck = setDuck;

  function isJumpKey(e) {
    return e.code === 'Space' || e.code === 'ArrowUp' ||
           e.key === ' ' || e.key === 'ArrowUp' || e.keyCode === 32 || e.keyCode === 38;
  }
  function isDuckKey(e) {
    return e.code === 'ArrowDown' || e.key === 'ArrowDown' || e.keyCode === 40;
  }
  function onKeyDown(e) {
    if (isJumpKey(e)) { e.preventDefault(); e.stopImmediatePropagation(); action(); return false; }
    if (isDuckKey(e)) { e.preventDefault(); e.stopImmediatePropagation(); setDuck(true); return false; }
  }
  function onKeyUp(e) {
    if (isDuckKey(e)) { e.preventDefault(); setDuck(false); }
  }
  window.addEventListener('keydown', onKeyDown, true);
  document.addEventListener('keydown', onKeyDown, true);
  canvas.addEventListener('keydown', onKeyDown);
  window.addEventListener('keyup', onKeyUp, true);
  canvas.addEventListener('mousedown', function (e) { e.preventDefault(); focusGame(); action(); });
  canvas.addEventListener('touchstart', function (e) { e.preventDefault(); focusGame(); action(); },
                          { passive: false });
  gameBox.addEventListener('click', focusGame);

  // ---------- obstacles ----------
  function spawnObstacle() {
    var r = Math.random();
    if (distance > 4500 && r < 0.22) {
      var hs = [GROUND_Y - 98, GROUND_Y - 64, GROUND_Y - 34];
      obstacles.push({ type: 'bird', x: W + 20, y: hs[(Math.random() * 3) | 0], w: 50, h: 32 });
    } else {
      var big = Math.random() < 0.5;
      var n = 1 + ((Math.random() * 3) | 0);
      var unit = big ? 24 : 17;
      var h = big ? 52 : 36;
      obstacles.push({ type: 'cactus', x: W + 20, y: GROUND_Y, n: n, unit: unit, h: h,
                       w: n * unit + (n - 1) * 6 });
    }
    var gap = (Math.random() * 55 + 55) * (BASE_SPEED / speed);
    nextObs = Math.round(gap) + 24;
  }

  // ---------- collision ----------
  function trexBox() {
    if (trex.duck) return { x: trex.x + 6, y: trex.y - TREX.duckH + 6, w: TREX.duckW - 20, h: TREX.duckH - 10 };
    return { x: trex.x + 12, y: trex.y - TREX.h + 6, w: TREX.w - 30, h: TREX.h - 12 };
  }
  function obBox(o) {
    if (o.type === 'bird') return { x: o.x + 6, y: o.y + 4, w: 38, h: 22 };
    return { x: o.x + 2, y: o.y - o.h, w: o.w - 4, h: o.h };
  }
  function overlap(a, b) {
    return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
  }

  // ---------- drawing ----------
  function drawTrex() {
    ctx.fillStyle = INK();
    var x = trex.x, fy = trex.y;
    if (trex.duck) {
      var dt = fy - TREX.duckH;
      ctx.fillRect(x - 12, dt + 12, 22, 14);              // tail
      ctx.fillRect(x, dt + 14, 86, 26);                   // body
      ctx.fillRect(x + 78, dt + 4, 40, 28);               // head
      ctx.fillRect(x + 112, dt + 16, 8, 5);               // snout
      ctx.fillStyle = PAPER(); ctx.fillRect(x + 104, dt + 10, 7, 7); ctx.fillStyle = INK();
      var df = (frame / 5 | 0) % 2;
      if (df) { ctx.fillRect(x + 22, fy - 14, 12, 14); ctx.fillRect(x + 56, fy - 7, 12, 7); }
      else    { ctx.fillRect(x + 22, fy - 7, 12, 7);  ctx.fillRect(x + 56, fy - 14, 12, 14); }
      return;
    }
    var top = fy - TREX.h;
    ctx.fillRect(x - 10, top + 36, 24, 16);               // tail
    ctx.fillRect(x + 6, top + 30, 44, 42);                // body
    ctx.fillRect(x + 40, top + 8, 30, 36);                // neck
    ctx.fillRect(x + 62, top + 2, 26, 24);                // head
    ctx.fillRect(x + 70, top + 26, 22, 8);                // jaw
    ctx.fillRect(x + 48, top + 46, 12, 6);                // arm
    ctx.fillStyle = PAPER(); ctx.fillRect(x + 78, top + 8, 7, 7); ctx.fillStyle = INK();
    if (state === 'over') { ctx.fillRect(x + 78, top + 8, 7, 3); }   // dead eye
    if (!trex.onGround) {
      ctx.fillRect(x + 18, top + 72, 13, 22);
      ctx.fillRect(x + 37, top + 72, 13, 22);
    } else if (state === 'running') {
      var f = (frame / 5 | 0) % 2;
      if (f) { ctx.fillRect(x + 18, top + 72, 13, 22); ctx.fillRect(x + 37, top + 80, 13, 14); }
      else   { ctx.fillRect(x + 18, top + 80, 13, 14); ctx.fillRect(x + 37, top + 72, 13, 22); }
    } else {
      ctx.fillRect(x + 18, top + 72, 13, 22);
      ctx.fillRect(x + 37, top + 72, 13, 22);
    }
  }

  function drawCactus(o) {
    ctx.fillStyle = INK();
    var top = o.y - o.h, u = o.unit;
    for (var i = 0; i < o.n; i++) {
      var cx = o.x + i * (u + 6);
      ctx.fillRect(cx + (u * 0.35 | 0), top, Math.max(4, u * 0.3 | 0), o.h);               // trunk
      ctx.fillRect(cx, top + (o.h * 0.4 | 0), Math.max(3, u * 0.3 | 0), o.h * 0.22 | 0);     // left arm
      ctx.fillRect(cx + (u * 0.7 | 0), top + (o.h * 0.28 | 0), Math.max(3, u * 0.3 | 0), o.h * 0.22 | 0); // right arm
    }
  }

  function drawBird(o) {
    ctx.fillStyle = INK();
    var x = o.x, y = o.y;
    ctx.fillRect(x + 8, y + 12, 30, 9);                   // body
    ctx.fillRect(x + 34, y + 7, 12, 9);                   // head
    ctx.fillRect(x + 45, y + 10, 6, 4);                   // beak
    var up = (frame / 9 | 0) % 2;
    if (up) ctx.fillRect(x, y, 26, 9); else ctx.fillRect(x + 2, y + 20, 26, 9);   // wing
  }

  function drawCloud(cl) {
    ctx.fillStyle = night ? '#5f6368' : '#e2e2e2';
    var x = cl.x, y = cl.y;
    ctx.fillRect(x + 10, y + 7, 44, 9);
    ctx.fillRect(x + 18, y, 26, 8);
    ctx.fillRect(x, y + 11, 62, 6);
  }

  function drawGround() {
    ctx.fillStyle = INK();
    ctx.fillRect(0, GROUND_Y + 2, W, 2);
    var off = Math.floor(ground) % 32;
    for (var i = -1; i < W / 16 + 2; i++) {
      var gx = i * 16 - off;
      if (i % 2 === 0) ctx.fillRect(gx, GROUND_Y + 9, 4, 2);
      else ctx.fillRect(gx + 7, GROUND_Y + 13, 2, 2);
    }
  }

  function drawNightSky() {
    if (!night) return;
    ctx.fillStyle = '#9aa0a6';
    ctx.fillRect(W - 120, 34, 16, 22);                    // crescent moon
    ctx.fillStyle = PAPER();
    ctx.fillRect(W - 116, 34, 12, 22);
    ctx.fillStyle = '#9aa0a6';
    var stars = [[120, 40], [260, 28], [430, 52], [600, 34], [760, 46]];
    for (var i = 0; i < stars.length; i++) ctx.fillRect(stars[i][0], stars[i][1], 3, 3);
  }

  function drawScore() {
    ctx.fillStyle = INK();
    ctx.font = 'bold 20px "Courier New", monospace';
    ctx.textBaseline = 'alphabetic';
    ctx.textAlign = 'left';
    var s = curScore();
    var hs = Math.max(hi, s);
    ctx.fillText('HI ' + pad5(hs), W - 232, 32);
    var blink = (s > 0 && s % 100 < 1);
    if (!(blink && (frame / 4 | 0) % 2)) ctx.fillText(pad5(s), W - 92, 32);
  }

  function drawRestartIcon() {
    var cx = W / 2, cy = H / 2 + 26, r = 16;
    ctx.strokeStyle = INK();
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.arc(cx, cy, r, Math.PI * 0.25, Math.PI * 1.9);
    ctx.stroke();
    ctx.fillStyle = INK();
    ctx.beginPath();
    ctx.moveTo(cx + r + 3, cy - 6);
    ctx.lineTo(cx + r - 5, cy - 9);
    ctx.lineTo(cx + r - 1, cy + 1);
    ctx.closePath();
    ctx.fill();
  }

  // ---------- update ----------
  function update() {
    frame++;
    if (state !== 'running') return;

    distance += speed * 0.6;
    speed = Math.min(MAX_SPEED, BASE_SPEED + distance * ACCEL);

    var s = curScore();
    if (s > 0 && s % 700 === 0 && s !== lastNightToggle) {
      night = !night;
      lastNightToggle = s;
      document.body.classList.toggle('night', night);
    }

    trex.vy += GRAVITY;
    trex.y += trex.vy;
    if (trex.y >= GROUND_Y) { trex.y = GROUND_Y; trex.vy = 0; trex.onGround = true; }

    ground += speed;
    for (var i = 0; i < clouds.length; i++) {
      clouds[i].x -= speed * 0.18;
      if (clouds[i].x < -70) { clouds[i].x = W + 40; clouds[i].y = 26 + Math.random() * 60; }
    }

    nextObs--;
    if (nextObs <= 0) spawnObstacle();
    for (var j = obstacles.length - 1; j >= 0; j--) {
      obstacles[j].x -= speed;
      if (obstacles[j].x + obstacles[j].w < -40) obstacles.splice(j, 1);
    }

    var tb = trexBox();
    for (var k = 0; k < obstacles.length; k++) {
      if (overlap(tb, obBox(obstacles[k]))) {
        state = 'over';
        hi = Math.max(hi, curScore());
        saveHi(hi);
        break;
      }
    }
  }

  // ---------- render ----------
  function render() {
    ctx.fillStyle = PAPER();
    ctx.fillRect(0, 0, W, H);

    drawNightSky();
    for (var i = 0; i < clouds.length; i++) drawCloud(clouds[i]);
    drawGround();
    for (var j = 0; j < obstacles.length; j++) {
      if (obstacles[j].type === 'bird') drawBird(obstacles[j]);
      else drawCactus(obstacles[j]);
    }
    drawTrex();
    drawScore();

    if (state === 'waiting') {
      ctx.fillStyle = night ? '#9aa0a6' : '#5f6368';
      ctx.font = '18px Arial';
      ctx.textAlign = 'center';
      ctx.fillText('Press Space or \u2191 to start', W / 2, H / 2 - 6);
      ctx.textAlign = 'left';
    }
    if (state === 'over') {
      ctx.fillStyle = INK();
      ctx.font = 'bold 24px Arial';
      ctx.textAlign = 'center';
      ctx.fillText('G A M E   O V E R', W / 2, H / 2 - 14);
      ctx.textAlign = 'left';
      drawRestartIcon();
    }
  }

  function loop() {
    update();
    render();
    requestAnimationFrame(loop);
  }

  reset();
  loop();
  setTimeout(focusGame, 50);
  setTimeout(focusGame, 250);
  setTimeout(focusGame, 600);
})();
</script>
</body>
</html>
"""


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------- tab site groups
# Open tabs are classified by hostname (and a few Google paths) into a small
# set of website types. Matching prefers exact domain suffixes, then a brand
# label in the host, then conservative keywords.

TAB_CATEGORY_INFO = {
    "search": ("Search", "#27AE60"),
    "shopping": ("Shopping", "#E67E22"),
    "travel": ("Travel", "#1ABC9C"),
    "food": ("Food", "#F39C12"),
    "video": ("Video", "#C0392B"),
    "social": ("Social", "#8E44AD"),
    "news": ("News", "#2980B9"),
    "sports": ("Sports", "#2ECC71"),
    "finance": ("Finance", "#16A085"),
    "email": ("Email", "#D35400"),
    "work": ("Work", "#34495E"),
    "tech": ("Tech", "#3498DB"),
    "other": ("Other", "#7F8C8D"),
}
TAB_CATEGORY_ORDER = [
    "search",
    "shopping",
    "travel",
    "food",
    "video",
    "social",
    "news",
    "sports",
    "finance",
    "email",
    "work",
    "tech",
    "other",
]

# Distinctive second-level names. "amazon.co.uk" matches the "amazon" label.
_BRAND_BLOBS = {
    "search": "google bing duckduckgo yahoo baidu yandex ecosia startpage kagi",
    "shopping": (
        "amazon ebay etsy walmart target aliexpress alibaba shein temu "
        "bestbuy newegg ikea costco wayfair overstock kohls macys nordstrom "
        "sephora ulta rakuten mercari poshmark depop stockx chewy petco "
        "homedepot lowes zara nike adidas shopify wish qvc zappos "
        "bloomingdales saksfifthavenue barneys"
    ),
    "travel": (
        "booking expedia airbnb kayak tripadvisor hotels marriott hilton "
        "hyatt delta united southwest jetblue ryanair easyjet airfrance "
        "lufthansa emirates vrbo agoda hotwire priceline skyscanner hopper "
        "orbitz travelocity uber lyft britishairways spirit"
    ),
    "food": (
        "doordash ubereats grubhub postmates seamless opentable instacart "
        "deliveroo justeat swiggy zomato resy allrecipes epicurious "
        "foodnetwork yelp"
    ),
    "video": (
        "youtube netflix hulu disneyplus twitch vimeo dailymotion "
        "crunchyroll peacock tubi rumble primevideo hbomax paramountplus "
        "pluto"
    ),
    "social": (
        "facebook instagram twitter reddit linkedin tiktok pinterest "
        "snapchat discord whatsapp telegram threads tumblr nextdoor quora "
        "mastodon messenger"
    ),
    "news": (
        "cnn bbc nytimes washingtonpost reuters npr foxnews nbcnews "
        "cbsnews abcnews bloomberg politico huffpost thehill latimes "
        "usatoday forbes axios vice vox aljazeera economist newsweek "
        "theguardian wsj time"
    ),
    "sports": (
        "espn nfl nba mlb nhl fifa uefa bleacherreport theathletic "
        "cbssports foxsports skysports olympics pga atptour ufc wwe"
    ),
    "finance": (
        "paypal chase bankofamerica wellsfargo capitalone americanexpress "
        "fidelity schwab vanguard coinbase binance kraken robinhood etrade "
        "sofi venmo cashapp revolut stripe chime ally discover wise"
    ),
    "email": "gmail protonmail proton fastmail zoho aol hotmail outlook",
    "work": (
        "github gitlab bitbucket atlassian asana trello notion slack zoom "
        "dropbox evernote clickup figma miro canva sharepoint office365 "
        "monday linear confluence jira"
    ),
    "tech": (
        "stackoverflow stackexchange arxiv wikipedia producthunt wired "
        "techcrunch theverge arstechnica engadget zdnet xda mdn "
        "hackernews"
    ),
}

SITE_BRANDS = {}
for _cat, _blob in _BRAND_BLOBS.items():
    for _brand in _blob.split():
        SITE_BRANDS[_brand] = _cat

# Multi-part hosts and short domains that brand-label matching would miss.
SITE_DOMAINS = {
    "youtu.be": "video",
    "youtube.com": "video",
    "music.youtube.com": "video",
    "primevideo.com": "video",
    "max.com": "video",
    "hbomax.com": "video",
    "disneyplus.com": "video",
    "plus.disney.com": "video",
    "twitch.tv": "video",
    "x.com": "social",
    "twitter.com": "social",
    "fb.com": "social",
    "t.me": "social",
    "wa.me": "social",
    "news.ycombinator.com": "tech",
    "ycombinator.com": "tech",
    "stackoverflow.com": "tech",
    "stackexchange.com": "tech",
    "wikipedia.org": "tech",
    "developer.mozilla.org": "tech",
    "github.com": "work",
    "gitlab.com": "work",
    "office.com": "work",
    "office365.com": "work",
    "live.com": "email",
    "outlook.com": "email",
    "outlook.live.com": "email",
    "outlook.office.com": "work",
    "gmail.com": "email",
    "mail.google.com": "email",
    "docs.google.com": "work",
    "sheets.google.com": "work",
    "slides.google.com": "work",
    "drive.google.com": "work",
    "calendar.google.com": "work",
    "meet.google.com": "work",
    "chat.google.com": "work",
    "classroom.google.com": "work",
    "maps.google.com": "travel",
    "flights.google.com": "travel",
    "travel.google.com": "travel",
    "news.google.com": "news",
    "shopping.google.com": "shopping",
    "finance.google.com": "finance",
    "google.com": "search",
    "bbc.co.uk": "news",
    "bbc.com": "news",
    "theguardian.com": "news",
    "nytimes.com": "news",
    "wsj.com": "news",
    "si.com": "sports",
    "apple.com": "tech",
    "icloud.com": "email",
    "amazon.com": "shopping",
    "amazon.co.uk": "shopping",
    "amazon.de": "shopping",
    "amazon.co.jp": "shopping",
}

_HOST_KEYWORDS = (
    ("shopping", ("shop", "store", "boutique", "outlet", "marketplace")),
    ("travel", ("hotel", "flight", "airline", "airport", "vacation",
                "booking", "travel", "hostel", "cruise")),
    ("food", ("recipe", "restaurant", "grocery", "pizza")),
    ("video", ("video", "stream", "movie")),
    ("news", ("news", "gazette", "herald", "tribune")),
    ("sports", ("sport", "football", "soccer")),
    ("finance", ("bank", "creditunion", "invest", "trading", "crypto")),
    ("email", ("mail", "inbox")),
)


def _is_google_host(host):
    return (
        host == "google.com"
        or host.startswith("google.")
        or ".google." in host
        or host.endswith(".google.com")
    )


def _google_path_category(host, path):
    if not _is_google_host(host):
        return None
    if path.startswith(("/maps", "/travel", "/flights")):
        return "travel"
    if path.startswith("/shopping"):
        return "shopping"
    if path.startswith("/finance"):
        return "finance"
    if path.startswith("/news"):
        return "news"
    if path.startswith("/mail"):
        return "email"
    return None


def classify_host(host):
    """Return a category id for a hostname (www. prefix already stripped)."""
    if not host:
        return "other"
    labels = host.split(".")
    for i in range(len(labels)):
        suffix = ".".join(labels[i:])
        mapped = SITE_DOMAINS.get(suffix)
        if mapped:
            return mapped
    for label in labels:
        mapped = SITE_BRANDS.get(label)
        if mapped:
            return mapped
    for cat, keys in _HOST_KEYWORDS:
        if any(key in host for key in keys):
            return cat
    return "other"


def classify_url(url):
    """Return a tab-group category id for a page URL."""
    if not url:
        return "other"
    qurl = QUrl(url)
    host = (qurl.host() or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (qurl.path() or "").lower()
    google_cat = _google_path_category(host, path)
    if google_cat:
        return google_cat
    return classify_host(host)


def category_color(cat):
    return TAB_CATEGORY_INFO.get(cat, TAB_CATEGORY_INFO["other"])[1]


def category_label(cat):
    return TAB_CATEGORY_INFO.get(cat, TAB_CATEGORY_INFO["other"])[0]


class _GroupDot(QWidget):
    """Tiny colored disc shown on the left of a grouped tab."""

    def __init__(self, color, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self.setFixedSize(10, 10)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._color)
        painter.drawEllipse(1, 1, 8, 8)


class GroupedTabBar(QTabBar):
    """Tab bar that paints a category-colored bar along consecutive groups."""

    def paintEvent(self, event):
        super().paintEvent(event)
        count = self.count()
        if count == 0:
            return
        painter = QPainter(self)
        painter.setPen(Qt.PenStyle.NoPen)
        shape = self.shape()
        vertical = shape in (
            QTabBar.Shape.RoundedWest,
            QTabBar.Shape.RoundedEast,
            QTabBar.Shape.TriangularWest,
            QTabBar.Shape.TriangularEast,
        )
        east = shape in (QTabBar.Shape.RoundedEast, QTabBar.Shape.TriangularEast)
        south = shape in (QTabBar.Shape.RoundedSouth, QTabBar.Shape.TriangularSouth)
        index = 0
        while index < count:
            cat = self.tabData(index) or "other"
            end = index + 1
            while end < count and (self.tabData(end) or "other") == cat:
                end += 1
            first = self.tabRect(index)
            last = self.tabRect(end - 1)
            painter.setBrush(QColor(category_color(cat)))
            if vertical:
                top = min(first.top(), last.top())
                bottom = max(first.bottom(), last.bottom())
                x = first.x() if east else first.right() - 3
                painter.drawRect(x, top + 2, 3, max(0, bottom - top - 3))
            elif south:
                painter.drawRect(
                    first.x() + 2,
                    first.y(),
                    max(0, last.right() - first.x() - 3),
                    3,
                )
            else:
                painter.drawRect(
                    first.x() + 2,
                    first.bottom() - 3,
                    max(0, last.right() - first.x() - 3),
                    3,
                )
            index = end


class TabGroupStrip(QWidget):
    """Row of colored chips for the site-type groups present in open tabs."""

    group_selected = pyqtSignal(str)
    close_group_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._buttons = []
        self.setObjectName("TabGroupStrip")
        self.setStyleSheet(
            "#TabGroupStrip { background: #f4f5f7; border-bottom: 1px solid #e4e7ec; }"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)
        hint = QLabel("Groups")
        hint.setStyleSheet("color: #667085; font-size: 11px; background: transparent;")
        layout.addWidget(hint)
        layout.addStretch(1)
        self._layout = layout
        self.hide()

    def update_groups(self, groups, current_cat):
        for btn in self._buttons:
            btn.deleteLater()
        self._buttons = []
        if not groups:
            self.hide()
            return
        self.show()
        for offset, (cat, count) in enumerate(groups):
            label, color = TAB_CATEGORY_INFO.get(cat, TAB_CATEGORY_INFO["other"])
            btn = QPushButton(f"{label} ({count})")
            btn.setCheckable(True)
            btn.setChecked(cat == current_cat)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setProperty("category", cat)
            btn.setToolTip(
                f"Jump to {label} tabs. Right-click to close this group."
            )
            btn.setStyleSheet(
                "QPushButton { background: #ffffff; border: 1px solid #d0d5dd;"
                f" border-left: 4px solid {color}; border-radius: 4px;"
                " padding: 3px 10px; font-size: 11px; }"
                f"QPushButton:checked {{ background: {color}22; font-weight: 600; }}"
                f"QPushButton:hover {{ background: {color}18; }}"
            )
            btn.clicked.connect(lambda _checked=False, c=cat: self.group_selected.emit(c))
            btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            btn.customContextMenuRequested.connect(
                lambda pos, c=cat, b=btn: self._chip_menu(b, pos, c)
            )
            self._layout.insertWidget(offset + 1, btn)
            self._buttons.append(btn)

    def set_current(self, cat):
        for btn in self._buttons:
            btn.setChecked(btn.property("category") == cat)

    def _chip_menu(self, btn, pos, cat):
        menu = QMenu(self)
        jump = menu.addAction("Jump to group")
        close = menu.addAction("Close tabs in this group")
        chosen = menu.exec(btn.mapToGlobal(pos))
        if chosen is jump:
            self.group_selected.emit(cat)
        elif chosen is close:
            self.close_group_requested.emit(cat)


def _ui_settings():
    return QSettings(APP_ORG_NAME, APP_NAME)


def _controls_on_left():
    return sys.platform == "darwin"


def _normalize_tab_position(value, default="top"):
    if isinstance(value, bytes):
        value = value.decode("utf-8", "ignore")
    if not isinstance(value, str):
        return default
    key = value.strip().lower()
    return key if key in TAB_STRIP_POSITIONS else default


def _load_tab_strip_position(app_settings=None):
    if isinstance(app_settings, dict):
        stored = app_settings.get("tab_strip_position")
        if stored:
            return _normalize_tab_position(stored)
    return _normalize_tab_position(_ui_settings().value("tab_strip_position", "top"))


def _save_tab_strip_position(position, app_settings=None):
    position = _normalize_tab_position(position)
    _ui_settings().setValue("tab_strip_position", position)
    if isinstance(app_settings, dict):
        app_settings["tab_strip_position"] = position
    return position


def _resize_edges_at(pos, size, margin=RESIZE_MARGIN):
    """Return Qt.Edge flags for a point in a widget of the given size, or 0."""
    if size.width() < margin * 2 or size.height() < margin * 2:
        return Qt.Edge(0)
    edges = Qt.Edge(0)
    if pos.x() <= margin:
        edges |= Qt.Edge.LeftEdge
    elif pos.x() >= size.width() - margin:
        edges |= Qt.Edge.RightEdge
    if pos.y() <= margin:
        edges |= Qt.Edge.TopEdge
    elif pos.y() >= size.height() - margin:
        edges |= Qt.Edge.BottomEdge
    return edges


def _cursor_for_edges(edges):
    left = bool(edges & Qt.Edge.LeftEdge)
    right = bool(edges & Qt.Edge.RightEdge)
    top = bool(edges & Qt.Edge.TopEdge)
    bottom = bool(edges & Qt.Edge.BottomEdge)
    if (left and top) or (right and bottom):
        return Qt.CursorShape.SizeFDiagCursor
    if (right and top) or (left and bottom):
        return Qt.CursorShape.SizeBDiagCursor
    if left or right:
        return Qt.CursorShape.SizeHorCursor
    if top or bottom:
        return Qt.CursorShape.SizeVerCursor
    return Qt.CursorShape.ArrowCursor


def _dock_zone_rects(size, thickness=None):
    width, height = size.width(), size.height()
    if thickness is None:
        thickness = max(DOCK_ZONE_MIN, int(min(width, height) * 0.2))
    thickness = min(thickness, max(1, width // 2), max(1, height // 2))
    return {
        "top": QRect(0, 0, width, thickness),
        "bottom": QRect(0, height - thickness, width, thickness),
        "left": QRect(0, 0, thickness, height),
        "right": QRect(width - thickness, 0, thickness, height),
    }


def _dock_zone_at(pos, size):
    """Pick the nearest window-edge dock zone, or None if outside all zones."""
    rects = _dock_zone_rects(size)
    hits = [name for name, rect in rects.items() if rect.contains(pos)]
    if not hits:
        return None
    dists = {
        "top": pos.y(),
        "bottom": size.height() - pos.y(),
        "left": pos.x(),
        "right": size.width() - pos.x(),
    }
    return min(hits, key=dists.get)


def _tab_bar_stylesheet(position):
    """Chrome-like tab chrome; radius follows the docked edge."""
    if position in ("left", "right"):
        radius = (
            "border-top-right-radius: 8px; border-bottom-right-radius: 8px;"
            if position == "left"
            else "border-top-left-radius: 8px; border-bottom-left-radius: 8px;"
        )
        padding = "padding: 10px 12px; min-height: 28px; max-width: 200px;"
        margin = "margin: 2px 0;"
    elif position == "bottom":
        radius = "border-bottom-left-radius: 8px; border-bottom-right-radius: 8px;"
        padding = "padding: 7px 16px; min-width: 60px; max-width: 240px;"
        margin = "margin: 0 2px 4px 2px;"
    else:
        radius = "border-top-left-radius: 8px; border-top-right-radius: 8px;"
        padding = "padding: 7px 16px; min-width: 60px; max-width: 240px;"
        margin = "margin: 4px 2px 0 2px;"
    return f"""
        QTabBar {{
            background: #dee1e6;
            border: none;
        }}
        QTabBar::tab {{
            background: #cfd3d8;
            color: #3c4043;
            border: none;
            {radius}
            {padding}
            {margin}
        }}
        QTabBar::tab:selected {{
            background: #ffffff;
            color: #202124;
        }}
        QTabBar::tab:hover:!selected {{
            background: #e8eaed;
        }}
        QTabBar::close-button {{
            subcontrol-position: right;
        }}
    """


class _CaptionButton(QPushButton):
    """Minimize / maximize-restore / close control for a custom title bar."""

    # pylint: disable=invalid-name

    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self._kind = kind
        self._restored = False
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFlat(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if _controls_on_left():
            self.setFixedSize(14, 14)
            self.setToolTip({"min": "Minimize", "max": "Zoom", "close": "Close"}[kind])
        else:
            self.setFixedSize(46, 32)
            self.setToolTip(
                {"min": "Minimize", "max": "Maximize", "close": "Close"}[kind]
            )
        self._apply_style()

    def set_maximized(self, maximized):
        self._restored = bool(maximized)
        if _controls_on_left():
            self.setToolTip("Restore" if maximized else "Zoom")
        else:
            self.setToolTip("Restore" if maximized else "Maximize")
        self.update()

    def _apply_style(self):
        if _controls_on_left():
            colors = {"close": "#ff5f57", "min": "#febc2e", "max": "#28c840"}
            self.setStyleSheet(
                "QPushButton { background: %s; border: 0.5px solid rgba(0,0,0,0.15);"
                " border-radius: 7px; }"
                "QPushButton:hover { background: %s; }"
                % (colors[self._kind], colors[self._kind])
            )
            return
        if self._kind == "close":
            self.setStyleSheet(
                "QPushButton { background: transparent; border: none; color: #202124;"
                " font-size: 16px; }"
                "QPushButton:hover { background: #e81123; color: white; }"
                "QPushButton:pressed { background: #c50f1f; color: white; }"
            )
            return
        self.setStyleSheet(
            "QPushButton { background: transparent; border: none; color: #202124;"
            " font-size: 14px; }"
            "QPushButton:hover { background: #d0d3d8; }"
            "QPushButton:pressed { background: #c0c4c8; }"
        )

    def paintEvent(self, event):
        if _controls_on_left():
            super().paintEvent(event)
            return
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(self.palette().buttonText().color(), 1.2))
        cx, cy = self.width() / 2, self.height() / 2
        if self._kind == "min":
            painter.drawLine(int(cx - 6), int(cy), int(cx + 6), int(cy))
        elif self._kind == "max":
            if self._restored:
                painter.drawRect(int(cx - 4), int(cy - 6), 9, 9)
                painter.drawLine(int(cx - 1), int(cy - 6), int(cx - 1), int(cy - 8))
                painter.drawLine(int(cx - 1), int(cy - 8), int(cx + 8), int(cy - 8))
                painter.drawLine(int(cx + 8), int(cy - 8), int(cx + 8), int(cy + 1))
                painter.drawLine(int(cx + 8), int(cy + 1), int(cx + 5), int(cy + 1))
            else:
                painter.drawRect(int(cx - 6), int(cy - 6), 12, 12)
        else:
            painter.drawLine(int(cx - 5), int(cy - 5), int(cx + 5), int(cy + 5))
            painter.drawLine(int(cx + 5), int(cy - 5), int(cx - 5), int(cy + 5))


class WindowControlButtons(QWidget):
    """Platform-ordered window caption buttons."""

    minimize_requested = pyqtSignal()
    maximize_requested = pyqtSignal()
    close_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("WindowControlButtons")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8 if _controls_on_left() else 0)
        self.min_btn = _CaptionButton("min", self)
        self.max_btn = _CaptionButton("max", self)
        self.close_btn = _CaptionButton("close", self)
        self.min_btn.clicked.connect(self.minimize_requested.emit)
        self.max_btn.clicked.connect(self.maximize_requested.emit)
        self.close_btn.clicked.connect(self.close_requested.emit)
        if _controls_on_left():
            layout.addWidget(self.close_btn)
            layout.addWidget(self.min_btn)
            layout.addWidget(self.max_btn)
            layout.setContentsMargins(8, 0, 8, 0)
        else:
            layout.addWidget(self.min_btn)
            layout.addWidget(self.max_btn)
            layout.addWidget(self.close_btn)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)

    def set_maximized(self, maximized):
        self.max_btn.set_maximized(maximized)


class TabDockHandle(QWidget):
    """Grip used to drag the whole tab strip onto a new window edge."""

    # pylint: disable=invalid-name

    dock_drag_started = pyqtSignal()
    dock_drag_moved = pyqtSignal(QPoint)
    dock_drag_finished = pyqtSignal(QPoint)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("TabDockHandle")
        self.setToolTip("Drag to dock the tab bar at the top, side, or bottom")
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "#TabDockHandle { background: #d0d5dd; border-radius: 4px; }"
            "#TabDockHandle:hover { background: #b8c0cc; }"
        )
        self.setFixedSize(22, 32)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self._press_global = None
        self._dragging = False

    def set_vertical(self, vertical):
        if vertical:
            self.setFixedSize(32, 22)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        else:
            self.setFixedSize(22, 32)
            self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#344054"))
        vertical = self.width() > self.height()
        if vertical:
            y = self.height() // 2
            for i in range(3):
                painter.drawEllipse(4 + i * 7, y - 5, 3, 3)
                painter.drawEllipse(4 + i * 7, y + 2, 3, 3)
        else:
            x = self.width() // 2
            for i in range(3):
                painter.drawEllipse(x - 5, 6 + i * 7, 3, 3)
                painter.drawEllipse(x + 2, 6 + i * 7, 3, 3)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_global = event.globalPosition()
            self._dragging = False
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press_global is None:
            super().mouseMoveEvent(event)
            return
        current = event.globalPosition()
        if not self._dragging:
            distance = (current - self._press_global).manhattanLength()
            if distance < QApplication.startDragDistance():
                return
            self._dragging = True
            self.dock_drag_started.emit()
        self.dock_drag_moved.emit(current.toPoint())
        event.accept()

    def mouseReleaseEvent(self, event):
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self.dock_drag_finished.emit(event.globalPosition().toPoint())
            event.accept()
        elif event.button() == Qt.MouseButton.LeftButton:
            event.accept()
        else:
            super().mouseReleaseEvent(event)
        self._press_global = None
        self._dragging = False


class DockDropOverlay(QWidget):
    """Edge highlights shown while dragging the tab strip.

    Implemented as a tool window so QMainWindow's layout cannot bury it.
    """

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Tool)
        self._zone = None
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowOpacity(0.88)
        self.hide()

    def set_zone(self, zone):
        if zone == self._zone:
            return
        self._zone = zone
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(15, 23, 42, 40))
        font = QFont(self.font())
        font.setPointSize(11)
        font.setBold(True)
        painter.setFont(font)
        for name, rect in _dock_zone_rects(self.size()).items():
            active = name == self._zone
            fill = QColor(37, 99, 235, 150) if active else QColor(255, 255, 255, 70)
            border = QColor(37, 99, 235, 230) if active else QColor(255, 255, 255, 140)
            painter.setPen(QPen(border, 2))
            painter.setBrush(fill)
            painter.drawRoundedRect(rect.adjusted(6, 6, -6, -6), 8, 8)
            painter.setPen(QColor("#ffffff") if active else QColor("#1f2937"))
            painter.drawText(
                rect,
                Qt.AlignmentFlag.AlignCenter,
                "Tabs: " + TAB_STRIP_LABELS[name],
            )


class CaptionDragArea(QWidget):
    """Empty caption space: drag to move, double-click to maximize/restore."""

    # pylint: disable=invalid-name

    move_requested = pyqtSignal()
    toggle_max_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CaptionDragArea")
        self.setMinimumWidth(24)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._label = QLabel(self)
        self._label.setStyleSheet("color: #344054; font-size: 12px; background: transparent;")
        self._label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.addWidget(self._label)
        self._pressing = False

    def set_title(self, title):
        self._label.setText(title or "")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressing = True
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pressing and event.buttons() & Qt.MouseButton.LeftButton:
            self._pressing = False
            self.move_requested.emit()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._pressing = False
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_max_requested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class ChromeTabBar(GroupedTabBar):
    """Grouped tab bar that also acts as a caption when clicking empty space."""

    # pylint: disable=invalid-name

    empty_space_move_requested = pyqtSignal()
    empty_space_double_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTabsClosable(True)
        self.setExpanding(False)
        self.setDocumentMode(True)
        self.setDrawBase(False)
        self.setElideMode(Qt.TextElideMode.ElideRight)
        self.setUsesScrollButtons(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        # Built-in QTabBar moving is unreliable once ::tab stylesheets are set,
        # so ChromeTabBar performs moveTab() itself after the drag threshold.
        self.setMovable(False)
        self._window_drag = False
        self._drag_index = -1
        self._press_pos = None

    def mousePressEvent(self, event):
        self._window_drag = False
        self._drag_index = -1
        self._press_pos = event.position()
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            index = self.tabAt(pos)
            if index < 0:
                self._window_drag = True
            else:
                self._drag_index = index
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._window_drag:
            if event.buttons() & Qt.MouseButton.LeftButton:
                self._window_drag = False
                self.empty_space_move_requested.emit()
            event.accept()
            return
        if self._drag_index >= 0 and event.buttons() & Qt.MouseButton.LeftButton:
            if (
                self._press_pos is not None
                and (event.position() - self._press_pos).manhattanLength()
                >= QApplication.startDragDistance()
            ):
                over = self.tabAt(event.position().toPoint())
                if over >= 0 and over != self._drag_index:
                    self.moveTab(self._drag_index, over)
                    self.setCurrentIndex(over)
                    self._drag_index = over
                    self._press_pos = event.position()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._window_drag = False
        self._drag_index = -1
        self._press_pos = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            if self.tabAt(pos) < 0:
                self.empty_space_double_clicked.emit()
                event.accept()
                return
        super().mouseDoubleClickEvent(event)


class BrowserTabs(QStackedWidget):
    """QTabWidget-like host whose tab bar can be reparented into the chrome."""

    # pylint: disable=invalid-name

    tabCloseRequested = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tab_bar = ChromeTabBar()
        self.tab_bar.tabCloseRequested.connect(self.tabCloseRequested.emit)
        self.tab_bar.currentChanged.connect(self._sync_stack_from_bar)
        self.tab_bar.tabMoved.connect(self._on_tab_moved)
        super().currentChanged.connect(self._sync_bar_from_stack)

    def tabBar(self):
        return self.tab_bar

    def addTab(self, widget, icon_or_text, text=None):
        if text is None:
            icon, label = QIcon(), icon_or_text
        else:
            icon, label = icon_or_text, text
        index = self.addWidget(widget)
        self.tab_bar.insertTab(index, icon, label)
        return index

    def removeTab(self, index):
        widget = self.widget(index)
        blocked = self.blockSignals(True)
        self.tab_bar.removeTab(index)
        if widget is not None:
            self.removeWidget(widget)
        self.blockSignals(blocked)
        current = self.tab_bar.currentIndex()
        if 0 <= current < self.count() and self.currentIndex() != current:
            self.setCurrentIndex(current)
        return widget

    def setTabText(self, index, text):
        self.tab_bar.setTabText(index, text)

    def tabText(self, index):
        return self.tab_bar.tabText(index)

    def setTabIcon(self, index, icon):
        self.tab_bar.setTabIcon(index, icon)

    def tabIcon(self, index):
        return self.tab_bar.tabIcon(index)

    def setTabToolTip(self, index, tip):
        self.tab_bar.setTabToolTip(index, tip)

    def tabToolTip(self, index):
        return self.tab_bar.tabToolTip(index)

    def setCurrentWidget(self, widget):
        index = self.indexOf(widget)
        if index >= 0:
            self.setCurrentIndex(index)

    def _sync_stack_from_bar(self, index):
        if 0 <= index < self.count() and self.currentIndex() != index:
            self.setCurrentIndex(index)

    def _sync_bar_from_stack(self, index):
        if self.tab_bar.currentIndex() != index and 0 <= index < self.tab_bar.count():
            self.tab_bar.setCurrentIndex(index)

    def _on_tab_moved(self, source, dest):
        widget = self.widget(source)
        if widget is None:
            return
        blocked = self.blockSignals(True)
        bar_blocked = self.tab_bar.blockSignals(True)
        self.removeWidget(widget)
        self.insertWidget(dest, widget)
        self.tab_bar.blockSignals(bar_blocked)
        self.blockSignals(blocked)
        current = self.tab_bar.currentIndex()
        if 0 <= current < self.count():
            self.setCurrentIndex(current)

    def blockSignals(self, block):
        self.tab_bar.blockSignals(block)
        return super().blockSignals(block)


class _EdgeGrip(QWidget):
    """Thin strip along a window edge that starts a system (or manual) resize."""

    # pylint: disable=invalid-name

    def __init__(self, edges, parent=None):
        super().__init__(parent)
        self._edges = edges
        self.setCursor(_cursor_for_edges(edges))
        self.setMouseTracking(True)
        self.setStyleSheet("background: transparent;")

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        window = self.window()
        starter = getattr(window, "_begin_edge_resize", None)
        if callable(starter):
            starter(self._edges, event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)


# --------------------------------------------------------------------- agent
AGENT_SYSTEM_PROMPT = """You are an autonomous web-browsing agent that controls a \
real web browser to accomplish a user's goal.

Each turn you receive the current page state as JSON: the URL, page title, a short \
excerpt of visible text, and a list of interactive elements. Each element has an \
integer "id", a "tag", a "type", and a "label".

Respond with a SINGLE JSON object and nothing else. Schema:
  {"thought": "<brief reasoning>", "action": "<name>", ...params}

Available actions:
  - {"action": "navigate", "url": "https://..."}        open a URL directly
  - {"action": "click", "id": <int>}                     click an element by id
  - {"action": "type", "id": <int>, "text": "...",        type into a field;
       "enter": true}                                     set enter=true to submit
  - {"action": "scroll", "direction": "down"|"up"}        scroll the page
  - {"action": "finish", "message": "<summary>"}          goal done / cannot proceed

Rules:
  - Prefer navigating to a known site or using its search box.
  - Only use element ids that exist in the current state.
  - Take ONE action per turn. Keep "thought" to one sentence. \
Do not repeat the same action over 3 times.
  - You cannot complete real purchases/payments; gather options and then finish \
with a summary of what you found and what the user should do next. You may stop \
at checkout and ask the user to enter payment details.
"""

CAPTURE_JS = r"""
(function() {
  var out = {url: location.href, title: document.title, elements: []};
  var sel = 'a, button, input, textarea, select, [role="button"], [onclick]';
  var nodes = document.querySelectorAll(sel);
  var i = 0;
  for (var k = 0; k < nodes.length && i <= 80; k++) {
    var el = nodes[k];
    var r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    if (r.bottom < 0 || r.top > (window.innerHeight + 800)) continue;
    el.setAttribute('data-agent-id', i);
    var label = (el.innerText || el.value || el.placeholder ||
                 el.getAttribute('aria-label') || el.name || '').trim();
    label = label.replace(/\s+/g, ' ').slice(0, 120);
    out.elements.push({id: i, tag: el.tagName.toLowerCase(),
                       type: (el.type || ''), label: label});
    i++;
  }
  out.text = (document.body ? document.body.innerText : '')
               .replace(/\s+/g, ' ').slice(0, 2500);
  return out;
})();
"""


def _is_fixed_temperature_model(model: str) -> bool:
    """True for models that only accept the default temperature (not 0)."""
    name = (model or "").lower().rsplit("/", 1)[-1]
    return name.startswith(("o1", "o3", "o4", "gpt-5"))


class LlmThread(QThread):
    """Runs a blocking chat-completion request off the UI thread.

    Supports multiple providers (OpenAI, Groq, Azure OpenAI, Google Gemini)
    via a single ``config`` dict produced by ``BrowserWindow.llm_config``.
    """

    succeeded = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, config, messages, parent=None):
        super().__init__(parent)
        self.config = config
        self.messages = messages

    def run(self):
        if self.config.get("provider") == "cursor":
            self._run_cursor()
            return
        try:
            url, data, headers = self._build_request()
        except ValueError as exc:
            self.failed.emit(str(exc))
            return
        # Several provider APIs (e.g. Groq) sit behind Cloudflare, which blocks
        # the default "Python-urllib" User-Agent with a 403 (error code 1010).
        headers.setdefault("User-Agent", USER_AGENT)
        headers.setdefault("Accept", "application/json")
        req = urllib.request.Request(
            url, data=data, headers=headers, method="POST"
        )
        omit_temp = self.config.get("_force_omit_temperature", False)
        for attempt in range(2):
            try:
                if attempt == 1:
                    self.config = {**self.config, "_force_omit_temperature": True}
                    omit_temp = True
                    url, data, headers = self._build_request()
                    headers.setdefault("User-Agent", USER_AGENT)
                    headers.setdefault("Accept", "application/json")
                req = urllib.request.Request(
                    url, data=data, headers=headers, method="POST"
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                self.succeeded.emit(self._parse_response(payload))
                return
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "ignore")
                if (
                    attempt == 0
                    and exc.code == 400
                    and not omit_temp
                    and "temperature" in detail
                    and "unsupported" in detail.lower()
                ):
                    continue
                self.failed.emit(f"HTTP {exc.code}: {detail[:300]}")
                return
            except Exception as exc:  # noqa: BLE001 - surface failures to the UI
                self.failed.emit(str(exc))
                return

    # -- Cursor SDK --------------------------------------------------------
    def _run_cursor(self):
        os.makedirs(CURSOR_SCRATCH_DIR, exist_ok=True)
        cfg = {
            "prompt": self._messages_to_prompt(),
            "api_key": self.config.get("api_key", ""),
            "model": self.config.get("model") or "auto",
            "cwd": CURSOR_SCRATCH_DIR,
        }
        in_path = out_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False, encoding="utf-8", dir=CURSOR_SCRATCH_DIR
            ) as in_file:
                json.dump(cfg, in_file)
                in_path = in_file.name
            out_path = in_path + ".out"
            proc = subprocess.run(
                [sys.executable, "-c", CURSOR_HELPER, in_path, out_path],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=600,
                cwd=CURSOR_SCRATCH_DIR,
            )
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()
                if "No module named" in err and "cursor_sdk" in err:
                    self.failed.emit(
                        "cursor-sdk not installed. Run: pip install cursor-sdk"
                    )
                else:
                    self.failed.emit(f"Cursor SDK failed: {err[-400:]}")
                return
            with open(out_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.succeeded.emit(str(data.get("result", "")))
        except subprocess.TimeoutExpired:
            self.failed.emit("Cursor SDK timed out.")
        except Exception as exc:  # noqa: BLE001 - surface SDK/auth failures to UI
            self.failed.emit(str(exc))
        finally:
            for path in (in_path, out_path):
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass

    def _messages_to_prompt(self):
        """Flatten the chat messages into one prompt for the one-shot SDK call."""
        system = "\n\n".join(
            m["content"] for m in self.messages if m.get("role") == "system"
        )
        lines = []
        for m in self.messages:
            role = m.get("role")
            if role == "system":
                continue
            lines.append(f"[{role}] {m.get('content', '')}")
        transcript = "\n".join(lines)
        return (
            f"{system}\n\nConversation so far:\n{transcript}\n\n"
            "Reply with ONLY the next single JSON action object, no other text."
        )

    # -- request building --------------------------------------------------
    def _build_request(self):
        provider = self.config.get("provider", DEFAULT_PROVIDER)
        if provider == "google":
            return self._build_gemini_request()
        if provider == "anthropic":
            return self._build_anthropic_request()
        return self._build_openai_request(provider)

    def _build_openai_request(self, provider):
        api_key = self.config.get("api_key", "")
        model = self.config.get("model", "")
        temp_model = (
            self.config.get("azure_deployment", "")
            if provider == "azure"
            else model
        )
        omit_temp = (
            self.config.get("_force_omit_temperature")
            or _is_fixed_temperature_model(temp_model)
        )
        body = {"messages": self.messages}
        if not omit_temp:
            body["temperature"] = 0
        if not _is_fixed_temperature_model(temp_model):
            body["response_format"] = {"type": "json_object"}
        if provider == "azure":
            endpoint = self.config.get("azure_endpoint", "").rstrip("/")
            deployment = self.config.get("azure_deployment", "")
            version = self.config.get("azure_api_version") or DEFAULT_AZURE_API_VERSION
            if not endpoint or not deployment:
                raise ValueError("Azure endpoint and deployment are required.")
            url = (
                f"{endpoint}/openai/deployments/{deployment}"
                f"/chat/completions?api-version={version}"
            )
            headers = {"Content-Type": "application/json", "api-key": api_key}
        else:
            url = {
                "groq": GROQ_URL,
                "openrouter": OPENROUTER_URL,
                "featherless": FEATHERLESS_URL,
            }.get(provider, OPENAI_URL)
            body["model"] = model
            # Featherless serves arbitrary open models, many of which do not
            # implement structured JSON output; drop the unsupported field.
            if provider == "featherless":
                body.pop("response_format", None)
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            }
            if provider in ("openrouter", "featherless"):
                headers["HTTP-Referer"] = "https://localhost/pyqt6-browser"
                headers["X-Title"] = "PyQt6 Browser Agent"
        return url, json.dumps(body).encode("utf-8"), headers

    def _build_anthropic_request(self):
        api_key = self.config.get("api_key", "")
        model = self.config.get("model", "")
        system_parts = []
        messages = []
        for msg in self.messages:
            role = msg.get("role")
            text = msg.get("content", "")
            if role == "system":
                system_parts.append(text)
            else:
                messages.append({"role": role, "content": text})
        body = {
            "model": model,
            "max_tokens": 1024,
            "temperature": 0,
            "messages": messages,
        }
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        return ANTHROPIC_URL, json.dumps(body).encode("utf-8"), headers

    def _build_gemini_request(self):
        api_key = self.config.get("api_key", "")
        model = self.config.get("model", "")
        system_parts = []
        contents = []
        for msg in self.messages:
            role = msg.get("role")
            text = msg.get("content", "")
            if role == "system":
                system_parts.append(text)
            else:
                gemini_role = "model" if role == "assistant" else "user"
                contents.append({"role": gemini_role, "parts": [{"text": text}]})
        body = {
            "contents": contents,
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
            },
        }
        if system_parts:
            body["system_instruction"] = {
                "parts": [{"text": "\n\n".join(system_parts)}]
            }
        url = GEMINI_URL.format(model=model) + f"?key={api_key}"
        headers = {"Content-Type": "application/json"}
        return url, json.dumps(body).encode("utf-8"), headers

    # -- response parsing --------------------------------------------------
    def _parse_response(self, payload):
        provider = self.config.get("provider")
        if provider == "google":
            candidates = payload.get("candidates", [])
            if not candidates:
                raise KeyError(f"No candidates in response: {str(payload)[:200]}")
            parts = candidates[0].get("content", {}).get("parts", [{}])
            return parts[0].get("text", "")
        if provider == "anthropic":
            blocks = payload.get("content", [])
            return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        return payload["choices"][0]["message"]["content"]


class BrowserAgent(QObject):
    """Drives the active web view using LLM-chosen actions in a step loop."""

    log = pyqtSignal(str, str)  # (role, message)
    running_changed = pyqtSignal(bool)

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.running = False
        self.view = None
        self.messages = []
        self.step = 0
        self._thread = None

    def start(self, instruction):
        if self.running:
            return
        cfg = self.window.llm_config()
        if not cfg.get("api_key"):
            label = PROVIDERS.get(cfg["provider"], {}).get("label", cfg["provider"])
            self.log.emit("error", f"No {label} API key set. Click 'Set API Key'.")
            return
        if cfg["provider"] == "azure" and (
            not cfg.get("azure_endpoint") or not cfg.get("azure_deployment")
        ):
            self.log.emit("error", "Azure needs endpoint + deployment (Azure Setup).")
            return
        self.view = self.window.current_view()
        self.messages = [
            {"role": "system", "content": AGENT_SYSTEM_PROMPT},
            {"role": "user", "content": f"Goal: {instruction}"},
        ]
        self.step = 0
        self.running = True
        self.running_changed.emit(True)
        self.log.emit("user", instruction)
        self._capture()

    def stop(self, reason="Stopped."):
        if not self.running:
            return
        self.running = False
        self.running_changed.emit(False)
        self.log.emit("system", reason)

    # -- step loop ---------------------------------------------------------
    def _capture(self):
        if not self.running or self.view is None:
            return
        self.view.page().runJavaScript(CAPTURE_JS, self._on_state)

    def _on_state(self, state):
        if not self.running:
            return
        if not state:
            state = {"url": self.view.url().toString(), "title": "", "elements": []}
        self.messages.append(
            {"role": "user", "content": "Current page state:\n" + json.dumps(state)}
        )
        self._thread = LlmThread(self.window.llm_config(), list(self.messages), self)
        self._thread.succeeded.connect(self._on_llm)
        self._thread.failed.connect(lambda msg: self.stop(f"Agent error: {msg}"))
        self._thread.start()

    def _on_llm(self, content):
        if not self.running:
            return
        self.messages.append({"role": "assistant", "content": content})
        action = self._parse(content)
        if action is None:
            self.stop("Could not parse the model response as JSON.")
            return

        thought = action.get("thought", "")
        name = action.get("action", "")
        if thought:
            self.log.emit("thought", thought)

        self.step += 1
        if self.step > AGENT_MAX_STEPS and name != "finish":
            self.stop(f"Reached step limit ({AGENT_MAX_STEPS}).")
            return

        if name == "finish":
            self.stop(f"Done: {action.get('message', '')}")
        elif name == "navigate":
            url = action.get("url", "")
            self.log.emit("action", f"navigate -> {url}")
            self.view.setUrl(QUrl(url))
            self._later(1800)
        elif name == "click":
            self.log.emit("action", f"click #{action.get('id')}")
            self._run_js(self._click_js(action.get("id")), 1500)
        elif name == "type":
            self.log.emit(
                "action",
                f"type into #{action.get('id')}: {action.get('text', '')[:40]}",
            )
            self._run_js(
                self._type_js(
                    action.get("id"), action.get("text", ""), action.get("enter")
                ),
                1800,
            )
        elif name == "scroll":
            self.log.emit("action", f"scroll {action.get('direction', 'down')}")
            amount = -700 if action.get("direction") == "up" else 700
            self._run_js(f"window.scrollBy(0, {amount});", 600)
        else:
            self.stop(f"Unknown action: {name}")

    def _run_js(self, script, delay):
        if not self.running or self.view is None:
            return
        self.view.page().runJavaScript(script, lambda _result=None: None)
        self._later(delay)

    def _later(self, ms):
        QTimer.singleShot(ms, self._capture)

    @staticmethod
    def _parse(content):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            start, end = content.find("{"), content.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(content[start : end + 1])
                except json.JSONDecodeError:
                    return None
            return None

    @staticmethod
    def _click_js(element_id):
        return (
            "(function(){var el=document.querySelector('[data-agent-id=\"%s\"]');"
            "if(!el)return 'not_found';el.scrollIntoView({block:'center'});"
            "el.click();return 'ok';})();" % element_id
        )

    @staticmethod
    def _type_js(element_id, text, enter):
        enter_js = ""
        if enter:
            enter_js = (
                "el.dispatchEvent(new KeyboardEvent('keydown',"
                "{key:'Enter',keyCode:13,which:13,bubbles:true}));"
                "el.dispatchEvent(new KeyboardEvent('keyup',"
                "{key:'Enter',keyCode:13,which:13,bubbles:true}));"
                "if(el.form){if(el.form.requestSubmit){el.form.requestSubmit();}"
                "else{el.form.submit();}}"
            )
        return (
            "(function(){var el=document.querySelector('[data-agent-id=\"%s\"]');"
            "if(!el)return 'not_found';el.focus();el.value=%s;"
            "el.dispatchEvent(new Event('input',{bubbles:true}));"
            "el.dispatchEvent(new Event('change',{bubbles:true}));%s"
            "return 'ok';})();" % (element_id, json.dumps(text), enter_js)
        )


class WebBoxDialog(QDialog):
    """Create or classify a WebBox (favorite or miscellaneous)."""

    def __init__(self, parent, title="", url="", kind=WEBBOX_FAVORITE):
        super().__init__(parent)
        self.setWindowTitle("New WebBox")
        layout = QFormLayout(self)
        self.title_edit = QLineEdit(title)
        self.url_edit = QLineEdit(url)
        self.kind_combo = QComboBox()
        self.kind_combo.addItem("Favorite WebBox", WEBBOX_FAVORITE)
        self.kind_combo.addItem("Miscellaneous WebBox", WEBBOX_MISC)
        idx = self.kind_combo.findData(kind)
        if idx >= 0:
            self.kind_combo.setCurrentIndex(idx)
        layout.addRow("Title:", self.title_edit)
        layout.addRow("Address:", self.url_edit)
        layout.addRow("Type:", self.kind_combo)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def values(self):
        return (
            self.title_edit.text().strip(),
            self.url_edit.text().strip(),
            self.kind_combo.currentData() or WEBBOX_MISC,
        )


class SettingsDialog(QDialog):
    """Application preferences (search engine)."""

    def __init__(self, parent, current_engine="google"):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        layout = QFormLayout(self)
        self.engine_combo = QComboBox()
        for key in SEARCH_ENGINE_ORDER:
            self.engine_combo.addItem(SEARCH_ENGINES[key]["label"], key)
        idx = self.engine_combo.findData(current_engine)
        if idx >= 0:
            self.engine_combo.setCurrentIndex(idx)
        layout.addRow("Search engine:", self.engine_combo)
        hint = QLabel("Used for Home, new tabs, and the address-bar search box.")
        hint.setStyleSheet("color: #667085;")
        hint.setWordWrap(True)
        layout.addRow(hint)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def search_engine(self):
        return self.engine_combo.currentData() or "google"


class BrowserWindow(QMainWindow):
    """Main browser window with a custom caption and a dockable tab strip."""

    # pylint: disable=too-many-public-methods,invalid-name
    _windows = []
    _shared_host = None

    def __init__(self, insecret=False):
        super().__init__()
        self.insecret = insecret
        self.setWindowTitle("PyQt6 Browser" + (" - Insecret" if insecret else ""))
        self.setMinimumSize(640, 400)
        self.resize(1280, 820)
        # Only Frameless + Customize: extra min/max/system-menu hints make many
        # X11 WMs (and GNOME CSD) draw a native title bar on top of our chrome.
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.CustomizeWindowHint
        )
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        BrowserWindow._windows.append(self)

        os.makedirs(DATA_DIR, exist_ok=True)
        self._start_blink_host()

        self.bookmarks = [] if insecret else _load_json(BOOKMARKS_FILE, [])
        self.history = [] if insecret else _load_json(HISTORY_FILE, [])
        self.settings = _load_json(SETTINGS_FILE, {})
        self._migrate_settings()
        self._migrate_webboxes()
        self._tab_position = _load_tab_strip_position(self.settings)
        self._tab_dock_actions = {}
        self._resize_edges = Qt.Edge(0)
        self._resize_origin = None
        self._resize_geom = None
        self._move_origin = None
        self._move_offset = None
        self._manual_interacting = False

        self.tabs = BrowserTabs()
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self.on_tab_changed)
        self.tabs.tabBar().customContextMenuRequested.connect(self._tab_bar_menu)
        self.tabs.tabBar().empty_space_move_requested.connect(self._begin_window_move)
        self.tabs.tabBar().empty_space_double_clicked.connect(self._toggle_maximized)

        self.group_strip = TabGroupStrip()
        self.group_strip.group_selected.connect(self._jump_to_tab_group)
        self.group_strip.close_group_requested.connect(self._close_tab_group)

        self._page_host = QWidget()
        page_layout = QVBoxLayout(self._page_host)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)
        page_layout.addWidget(self.group_strip)
        page_layout.addWidget(self.tabs, 1)

        self._strip_host = QWidget()
        self._strip_host.setObjectName("TabStripHost")
        self._strip_layout = QHBoxLayout(self._strip_host)
        self._strip_layout.setContentsMargins(4, 0, 4, 0)
        self._strip_layout.setSpacing(2)

        self._dock_handle = TabDockHandle()
        self._dock_handle.dock_drag_started.connect(self._on_dock_drag_started)
        self._dock_handle.dock_drag_moved.connect(self._on_dock_drag_moved)
        self._dock_handle.dock_drag_finished.connect(self._on_dock_drag_finished)
        self._dock_handle.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._dock_handle.customContextMenuRequested.connect(self._dock_handle_menu)

        self._controls = WindowControlButtons()
        self._controls.minimize_requested.connect(self.showMinimized)
        self._controls.maximize_requested.connect(self._toggle_maximized)
        self._controls.close_requested.connect(self.close)

        self._caption = CaptionDragArea()
        self._caption.set_title(self.windowTitle())
        self._caption.move_requested.connect(self._begin_window_move)
        self._caption.toggle_max_requested.connect(self._toggle_maximized)
        self.windowTitleChanged.connect(self._caption.set_title)

        self._menu_bar = QMenuBar()
        self._menu_bar.setNativeMenuBar(False)

        self._title_row = QWidget()
        self._title_row.setObjectName("TitleRow")
        self._title_row_layout = QHBoxLayout(self._title_row)
        self._title_row_layout.setContentsMargins(0, 0, 0, 0)
        self._title_row_layout.setSpacing(0)
        self._title_row.setMinimumHeight(36)

        self._chrome = QWidget()
        self._chrome.setObjectName("WindowChrome")
        chrome_layout = QVBoxLayout(self._chrome)
        chrome_layout.setContentsMargins(0, 0, 0, 0)
        chrome_layout.setSpacing(0)
        chrome_layout.addWidget(self._title_row)
        chrome_layout.addWidget(self._menu_bar)
        for widget in (self._chrome, self._title_row, self._strip_host, self._caption):
            widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._chrome.setStyleSheet(
            "#WindowChrome, #TitleRow, #TabStripHost, #CaptionDragArea {"
            " background: #dee1e6; }"
            "#TitleRow { border-bottom: 1px solid #c5c9d0; }"
        )
        self.setObjectName("BrowserWindow")
        self.setStyleSheet("#BrowserWindow { border: 1px solid #9aa0a6; background: #dee1e6; }")
        self.setMenuWidget(self._chrome)

        self._central = QWidget()
        self._central.setObjectName("BrowserCentral")
        self._central_layout = QVBoxLayout(self._central)
        self._central_layout.setContentsMargins(0, 0, 0, 0)
        self._central_layout.setSpacing(0)
        self.setCentralWidget(self._central)

        self._dock_overlay = DockDropOverlay(self)
        self._edge_grips = self._create_edge_grips()

        self._organize_timer = QTimer(self)
        self._organize_timer.setSingleShot(True)
        self._organize_timer.timeout.connect(self._organize_tabs)

        self.agent = BrowserAgent(self)

        self._build_toolbar()
        self._build_menus()
        self._build_statusbar()
        self._build_library_dock()
        self._build_agent_dock()
        self._build_inspector_dock()
        self._apply_tab_strip_position(self._tab_position, persist=False)
        QApplication.instance().installEventFilter(self)
        # QtWebEngine recreates its native render widget (the view's focusProxy)
        # on load/focus changes; keep re-installing our key filter on it so the
        # dino game keeps receiving Space/Up/Down even when the web view has
        # native keyboard focus.
        self._dino_filter_timer = QTimer(self)
        self._dino_filter_timer.timeout.connect(self._refresh_dino_filter)
        self._dino_filter_timer.start(300)
        self.add_tab(QUrl(self.home_url()))

    def _start_blink_host(self):
        try:
            if self.insecret:
                self.host = BlinkHost(make_insecret_dir(), self, insecret=True)
                self.host.start()
            else:
                host = BrowserWindow._shared_host
                if host is None:
                    host = BlinkHost(BLINK_PROFILE_DIR)
                    host.start()
                    BrowserWindow._shared_host = host
                self.host = host
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Blink engine",
                "Could not start Chromium:\n%s" % exc,
            )
            raise
        self.host.download_requested.connect(self._on_download_requested)
        self.host.download_finished.connect(self._on_host_download_finished)

    # ------------------------------------------------------------------ UI
    def _build_toolbar(self):
        nav = QToolBar("Navigation")
        nav.setMovable(False)
        self.addToolBar(nav)

        back = QAction("Back", self)
        back.setShortcut(QKeySequence.StandardKey.Back)
        back.triggered.connect(lambda: self.current_view().back())
        nav.addAction(back)

        forward = QAction("Forward", self)
        forward.setShortcut(QKeySequence.StandardKey.Forward)
        forward.triggered.connect(lambda: self.current_view().forward())
        nav.addAction(forward)

        reload = QAction("Reload", self)
        reload.setShortcut(QKeySequence("Ctrl+R"))
        reload.setShortcuts([QKeySequence("Ctrl+R"), QKeySequence.StandardKey.Refresh])
        reload.triggered.connect(lambda: self.current_view().reload())
        nav.addAction(reload)

        home = QAction("Home", self)
        home.setShortcut(QKeySequence("Alt+Home"))
        home.triggered.connect(self.go_home)
        nav.addAction(home)

        self.url_bar = QLineEdit()
        self.url_bar.setPlaceholderText("Search or enter address")
        self.url_bar.setClearButtonEnabled(True)
        self.url_bar.returnPressed.connect(self.navigate_to_url)
        nav.addWidget(self.url_bar)

        star = QAction("\u2606 WebBox", self)
        star.setShortcut(QKeySequence("Ctrl+D"))
        star.triggered.connect(lambda: self.add_webbox(WEBBOX_FAVORITE))
        nav.addAction(star)

        new_tab = QAction("New Tab", self)
        new_tab.setShortcut(QKeySequence("Ctrl+T"))
        new_tab.triggered.connect(lambda: self.add_tab(QUrl(self.home_url())))
        nav.addAction(new_tab)

        inspect = QAction("Inspect", self)
        inspect.setShortcut(QKeySequence("F12"))
        inspect.triggered.connect(self.toggle_inspector)
        nav.addAction(inspect)

    def _build_menus(self):
        menubar = self._menu_bar

        file_menu = QMenu("File", self)
        menubar.addMenu(file_menu)
        new_tab = file_menu.addAction("New Tab")
        new_tab.setShortcut(QKeySequence("Ctrl+T"))
        new_tab.triggered.connect(lambda: self.add_tab(QUrl(self.home_url())))
        new_win = file_menu.addAction("New Window")
        new_win.setShortcut(QKeySequence("Ctrl+N"))
        new_win.triggered.connect(self._new_window)
        insecret = file_menu.addAction("New Insecret Window")
        insecret.setShortcut(QKeySequence("Ctrl+Shift+N"))
        insecret.triggered.connect(self._new_insecret_window)
        close_tab = file_menu.addAction("Close Tab")
        close_tab.setShortcut(QKeySequence("Ctrl+W"))
        close_tab.triggered.connect(lambda: self.close_tab(self.tabs.currentIndex()))
        file_menu.addSeparator()
        focus_url = file_menu.addAction("Focus Address Bar")
        focus_url.setShortcut(QKeySequence("Ctrl+L"))
        focus_url.triggered.connect(self.url_bar.setFocus)

        self.bookmarks_menu = QMenu("WebBoxes", self)
        menubar.addMenu(self.bookmarks_menu)
        self.bookmarks_menu.aboutToShow.connect(self._rebuild_bookmarks_menu)

        self.history_menu = QMenu("History", self)
        menubar.addMenu(self.history_menu)
        self.history_menu.aboutToShow.connect(self._rebuild_history_menu)

        self.view_menu = QMenu("View", self)
        menubar.addMenu(self.view_menu)

        self.auto_organize_action = QAction("Auto-organize tabs by site type", self)
        self.auto_organize_action.setCheckable(True)
        self.auto_organize_action.setChecked(
            self.settings.get("auto_organize_tabs", True)
        )
        self.auto_organize_action.toggled.connect(self._toggle_auto_organize)
        self.view_menu.addAction(self.auto_organize_action)

        organize_now = QAction("Organize Tabs Now", self)
        organize_now.setShortcut(QKeySequence("Ctrl+Shift+O"))
        organize_now.triggered.connect(lambda: self._organize_tabs_now())
        self.view_menu.addAction(organize_now)

        assign_menu = self.view_menu.addMenu("Assign current tab to")
        for cat in TAB_CATEGORY_ORDER:
            action = assign_menu.addAction(category_label(cat))
            action.triggered.connect(
                lambda _checked=False, c=cat: self._assign_tab_group(c)
            )
        clear_group = self.view_menu.addAction("Clear manual group on this tab")
        clear_group.triggered.connect(self._clear_manual_group)
        self.view_menu.addSeparator()
        self._add_tab_dock_menu(self.view_menu)
        self.view_menu.addSeparator()

        next_tab = self.view_menu.addAction("Next Tab")
        next_tab.setShortcut(QKeySequence("Ctrl+Tab"))
        next_tab.triggered.connect(self._next_tab)
        prev_tab = self.view_menu.addAction("Previous Tab")
        prev_tab.setShortcut(QKeySequence("Ctrl+Shift+Tab"))
        prev_tab.triggered.connect(self._prev_tab)
        self.view_menu.addSeparator()

        self.settings_menu = QMenu("Settings", self)
        menubar.addMenu(self.settings_menu)
        prefs = self.settings_menu.addAction("Preferences…")
        prefs.triggered.connect(self._open_settings)

        self.apps_menu = QMenu("Apps", self)
        menubar.addMenu(self.apps_menu)
        self.apps_menu.aboutToShow.connect(self._rebuild_apps_menu)

    def _add_tab_dock_menu(self, parent_menu):
        dock_menu = parent_menu.addMenu("Dock tab bar")
        self._tab_dock_actions = {}
        for key in TAB_STRIP_POSITIONS:
            action = dock_menu.addAction(TAB_STRIP_LABELS[key])
            action.setCheckable(True)
            action.setChecked(key == self._tab_position)
            action.triggered.connect(
                lambda _checked=False, pos=key: self._set_tab_strip_position(pos)
            )
            self._tab_dock_actions[key] = action
        return dock_menu

    def _build_statusbar(self):
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.statusBar().addPermanentWidget(self.progress)

    def _build_library_dock(self):
        dock = QDockWidget("Library", self)
        dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
        )
        tabs = QTabWidget()

        self.bookmarks_list = QListWidget()
        self.bookmarks_list.itemActivated.connect(self._open_list_item)
        tabs.addTab(self.bookmarks_list, "WebBoxes")

        self.history_list = QListWidget()
        self.history_list.itemActivated.connect(self._open_list_item)
        tabs.addTab(self.history_list, "History")

        dock.setWidget(tabs)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        self.view_menu.addAction(dock.toggleViewAction())
        self.library_dock = dock
        self.refresh_library()

    def _build_agent_dock(self):
        dock = QDockWidget("AI Agent", self)
        dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
        )
        container = QWidget()
        layout = QVBoxLayout(container)

        provider_row = QHBoxLayout()
        provider_row.addWidget(QLabel("Provider:"))
        self.provider_combo = QComboBox()
        for pid in PROVIDER_ORDER:
            self.provider_combo.addItem(PROVIDERS[pid]["label"], pid)
        current = self.settings.get("provider", DEFAULT_PROVIDER)
        idx = self.provider_combo.findData(current)
        if idx >= 0:
            self.provider_combo.setCurrentIndex(idx)
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        provider_row.addWidget(self.provider_combo, 1)
        layout.addLayout(provider_row)

        key_row = QHBoxLayout()
        self.key_button = QPushButton("Set API Key")
        self.key_button.clicked.connect(self._set_api_key)
        key_row.addWidget(self.key_button)
        self.azure_button = QPushButton("Azure Setup")
        self.azure_button.clicked.connect(self._azure_setup)
        key_row.addWidget(self.azure_button)
        layout.addLayout(key_row)

        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("Model:"))
        self.model_edit = QLineEdit()
        self.model_edit.setToolTip("Model / deployment name for the selected provider")
        self.model_edit.editingFinished.connect(self._save_model)
        model_row.addWidget(self.model_edit, 1)
        layout.addLayout(model_row)

        self.key_status = QLabel()
        self.key_status.setStyleSheet("color: gray;")
        layout.addWidget(self.key_status)

        self.agent_input = QLineEdit()
        self.agent_input.setPlaceholderText('e.g. "book a flight to paris"')
        self.agent_input.returnPressed.connect(self._run_agent)
        layout.addWidget(self.agent_input)

        button_row = QHBoxLayout()
        self.run_button = QPushButton("Run")
        self.run_button.clicked.connect(self._run_agent)
        button_row.addWidget(self.run_button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(lambda: self.agent.stop())
        button_row.addWidget(self.stop_button)
        layout.addLayout(button_row)

        self.agent_log = QTextEdit()
        self.agent_log.setReadOnly(True)
        layout.addWidget(self.agent_log, 1)

        dock.setWidget(container)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self.view_menu.addAction(dock.toggleViewAction())
        self.agent_dock = dock

        self.agent.log.connect(self._append_agent_log)
        self.agent.running_changed.connect(self._on_agent_running)
        self._sync_provider_ui()

    def _build_inspector_dock(self):
        dock = QDockWidget("Inspector", self)
        dock.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
        )
        container = QWidget()
        layout = QVBoxLayout(container)
        self.inspector_log = QTextEdit()
        self.inspector_log.setReadOnly(True)
        self.inspector_log.setPlaceholderText("JavaScript results appear here.")
        self.inspector_input = QLineEdit()
        self.inspector_input.setPlaceholderText("Evaluate JavaScript on this page")
        self.inspector_input.returnPressed.connect(self._run_inspector_js)
        layout.addWidget(self.inspector_log, 1)
        layout.addWidget(self.inspector_input)
        dock.setWidget(container)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        dock.hide()
        self.inspector_dock = dock
        self.view_menu.addAction(dock.toggleViewAction())

    def toggle_inspector(self):
        if self.inspector_dock.isVisible():
            self.inspector_dock.hide()
        else:
            self.inspector_dock.show()
            self.inspector_input.setFocus()

    def _on_inspector_visibility(self, visible):
        if visible:
            self.inspector_input.setFocus()

    def _attach_inspector(self):
        return

    def _run_inspector_js(self):
        view = self.current_view()
        script = self.inspector_input.text().strip()
        if view is None or not script:
            return

        def done(result):
            self.inspector_log.append(f">>> {script}\n{result!r}")

        view.page().runJavaScript(script, done)

    # -------------------------------------------------------- window chrome
    def _create_edge_grips(self):
        specs = (
            Qt.Edge.TopEdge,
            Qt.Edge.BottomEdge,
            Qt.Edge.LeftEdge,
            Qt.Edge.RightEdge,
            Qt.Edge.TopEdge | Qt.Edge.LeftEdge,
            Qt.Edge.TopEdge | Qt.Edge.RightEdge,
            Qt.Edge.BottomEdge | Qt.Edge.LeftEdge,
            Qt.Edge.BottomEdge | Qt.Edge.RightEdge,
        )
        grips = []
        for edges in specs:
            grip = _EdgeGrip(edges, self)
            grip.hide()
            grips.append(grip)
        return grips

    def _layout_edge_grips(self):
        margin = RESIZE_MARGIN
        width, height = self.width(), self.height()
        hide = self.isMaximized() or self.isFullScreen()
        geom = [
            QRect(margin, 0, max(0, width - 2 * margin), margin),
            QRect(margin, height - margin, max(0, width - 2 * margin), margin),
            QRect(0, margin, margin, max(0, height - 2 * margin)),
            QRect(width - margin, margin, margin, max(0, height - 2 * margin)),
            QRect(0, 0, margin, margin),
            QRect(width - margin, 0, margin, margin),
            QRect(0, height - margin, margin, margin),
            QRect(width - margin, height - margin, margin, margin),
        ]
        for grip, rect in zip(self._edge_grips, geom):
            grip.setGeometry(rect)
            grip.setVisible(not hide)
            if not hide:
                grip.raise_()

    def _clear_layout(self, layout):
        if layout is None:
            return
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(self)

    def _replace_box_layout(self, host, vertical):
        self._clear_layout(host.layout())
        if host.layout() is not None:
            QWidget().setLayout(host.layout())
        layout = QVBoxLayout(host) if vertical else QHBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        return layout

    def _apply_tab_strip_position(self, position, persist=True):
        position = _normalize_tab_position(position)
        self._tab_position = position
        vertical = position in ("left", "right")
        shapes = {
            "top": QTabBar.Shape.RoundedNorth,
            "bottom": QTabBar.Shape.RoundedSouth,
            "left": QTabBar.Shape.RoundedWest,
            "right": QTabBar.Shape.RoundedEast,
        }
        tab_bar = self.tabs.tabBar()
        tab_bar.setShape(shapes[position])
        tab_bar.setStyleSheet(_tab_bar_stylesheet(position))
        self._dock_handle.set_vertical(vertical)

        self._clear_layout(self._title_row_layout)
        # Strip internals are vertical on the sides; the window's central
        # layout is the opposite so a side strip sits beside the page.
        self._strip_layout = self._replace_box_layout(self._strip_host, vertical)
        self._central_layout = self._replace_box_layout(self._central, not vertical)
        self._configure_strip_metrics(vertical)

        self._strip_layout.addWidget(self._dock_handle)
        self._strip_layout.addWidget(tab_bar, 1)

        if _controls_on_left():
            self._title_row_layout.addWidget(self._controls)
        if position == "top":
            self._caption.hide()
            self._title_row_layout.addWidget(self._strip_host, 1)
        else:
            self._caption.show()
            self._title_row_layout.addWidget(self._caption, 1)
        if not _controls_on_left():
            self._title_row_layout.addWidget(self._controls)

        if position == "left":
            self._central_layout.addWidget(self._strip_host)
            self._central_layout.addWidget(self._page_host, 1)
        elif position == "right":
            self._central_layout.addWidget(self._page_host, 1)
            self._central_layout.addWidget(self._strip_host)
        elif position == "bottom":
            self._central_layout.addWidget(self._page_host, 1)
            self._central_layout.addWidget(self._strip_host)
        else:
            self._central_layout.addWidget(self._page_host, 1)

        self._caption.set_title("" if position == "top" else self.windowTitle())
        self._sync_tab_dock_actions()
        if persist:
            _save_tab_strip_position(position, self.settings)
            _save_json(SETTINGS_FILE, self.settings)
        QTimer.singleShot(0, self._layout_edge_grips)

    def _configure_strip_metrics(self, vertical):
        tab_bar = self.tabs.tabBar()
        if vertical:
            self._strip_layout.setContentsMargins(2, 4, 2, 4)
            tab_bar.setMinimumWidth(132)
            tab_bar.setMaximumWidth(220)
            tab_bar.setMinimumHeight(0)
            tab_bar.setMaximumHeight(16777215)
            self._strip_host.setMinimumWidth(150)
            self._strip_host.setMaximumWidth(228)
            self._strip_host.setMinimumHeight(0)
            self._strip_host.setMaximumHeight(16777215)
            self._strip_host.setSizePolicy(
                QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding
            )
            return
        self._strip_layout.setContentsMargins(4, 0, 4, 0)
        tab_bar.setMinimumWidth(0)
        tab_bar.setMaximumWidth(16777215)
        tab_bar.setMinimumHeight(0)
        tab_bar.setMaximumHeight(16777215)
        self._strip_host.setMinimumWidth(0)
        self._strip_host.setMaximumWidth(16777215)
        self._strip_host.setMinimumHeight(0)
        self._strip_host.setMaximumHeight(16777215)
        self._strip_host.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )

    def _set_tab_strip_position(self, position):
        if _normalize_tab_position(position) == self._tab_position:
            self._sync_tab_dock_actions()
            return
        self._apply_tab_strip_position(position)

    def _sync_tab_dock_actions(self):
        actions = getattr(self, "_tab_dock_actions", None)
        if not actions:
            return
        for key, action in actions.items():
            action.setChecked(key == self._tab_position)

    def _overlay_screen_rect(self):
        return QRect(self.mapToGlobal(QPoint(0, 0)), self.size())

    def _on_dock_drag_started(self):
        self._dock_overlay.setGeometry(self._overlay_screen_rect())
        self._dock_overlay.set_zone(self._tab_position)
        self._dock_overlay.show()
        self._dock_overlay.raise_()

    def _on_dock_drag_moved(self, global_pos):
        local = self.mapFromGlobal(global_pos)
        self._dock_overlay.setGeometry(self._overlay_screen_rect())
        self._dock_overlay.set_zone(_dock_zone_at(local, self.size()))
        self._dock_overlay.raise_()

    def _on_dock_drag_finished(self, global_pos):
        zone = _dock_zone_at(self.mapFromGlobal(global_pos), self.size())
        self._dock_overlay.hide()
        self._dock_overlay.set_zone(None)
        if zone:
            self._set_tab_strip_position(zone)

    def _toggle_maximized(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        self._controls.set_maximized(self.isMaximized())
        self._layout_edge_grips()

    def _begin_window_move(self):
        if self.isFullScreen():
            return
        handle = self.windowHandle()
        if handle is None:
            self._ensure_window_handle()
            handle = self.windowHandle()
        if self.isMaximized() and sys.platform != "win32":
            # Restoring first lets X11/Xfce follow the cursor; Windows does this
            # itself inside startSystemMove.
            cursor = QCursor.pos()
            geom = self.geometry()
            width = max(geom.width(), 1)
            ratio = (cursor.x() - geom.x()) / width
            self.showNormal()
            self._controls.set_maximized(False)
            restored = self.geometry()
            self.move(cursor.x() - int(restored.width() * ratio), cursor.y() - 12)
        if handle is not None and handle.startSystemMove():
            return
        origin = QCursor.pos()
        self._move_origin = origin
        self._move_offset = origin - self.frameGeometry().topLeft()
        self._manual_interacting = True
        self.grabMouse()

    def _begin_edge_resize(self, edges, global_pos):
        if self.isMaximized() or self.isFullScreen() or not edges:
            return
        handle = self.windowHandle()
        if handle is None:
            self._ensure_window_handle()
            handle = self.windowHandle()
        if handle is not None and handle.startSystemResize(edges):
            return
        self._resize_edges = edges
        self._resize_origin = QPoint(global_pos)
        self._resize_geom = QRect(self.geometry())
        self._manual_interacting = True
        self.grabMouse()

    def _ensure_window_handle(self):
        if self.windowHandle() is None:
            self.winId()

    def _update_manual_move(self, global_pos):
        if self._move_offset is None:
            return
        self.move(global_pos - self._move_offset)

    def _update_manual_resize(self, global_pos):
        if not self._resize_edges or self._resize_geom is None:
            return
        delta = global_pos - self._resize_origin
        geom = QRect(self._resize_geom)
        min_w = self.minimumWidth()
        min_h = self.minimumHeight()
        if self._resize_edges & Qt.Edge.LeftEdge:
            geom.setLeft(min(geom.right() - min_w + 1, geom.left() + delta.x()))
        if self._resize_edges & Qt.Edge.RightEdge:
            geom.setRight(max(geom.left() + min_w - 1, geom.right() + delta.x()))
        if self._resize_edges & Qt.Edge.TopEdge:
            geom.setTop(min(geom.bottom() - min_h + 1, geom.top() + delta.y()))
        if self._resize_edges & Qt.Edge.BottomEdge:
            geom.setBottom(max(geom.top() + min_h - 1, geom.bottom() + delta.y()))
        self.setGeometry(geom)

    def _end_manual_interaction(self):
        if not self._manual_interacting:
            return
        self._manual_interacting = False
        self._move_origin = None
        self._move_offset = None
        self._resize_edges = Qt.Edge(0)
        self._resize_origin = None
        self._resize_geom = None
        if QWidget.mouseGrabber() is self:
            self.releaseMouse()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._controls.set_maximized(self.isMaximized())
            self._layout_edge_grips()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_edge_grips()
        if self._dock_overlay.isVisible():
            self._dock_overlay.setGeometry(self.rect())
            self._dock_overlay.raise_()

    def mouseMoveEvent(self, event):
        if self._manual_interacting:
            pos = event.globalPosition().toPoint()
            if self._move_offset is not None:
                self._update_manual_move(pos)
            elif self._resize_edges:
                self._update_manual_resize(pos)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._manual_interacting:
            self._end_manual_interaction()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self._ensure_window_handle()
        self._layout_edge_grips()
        self._controls.set_maximized(self.isMaximized())

    # ----------------------------------------------------------------- tabs
    def add_tab(self, url: QUrl):
        view = BlinkView(self.host)
        view.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        view._dino_page = False
        view.manual_group = None
        view.setUrl(url)
        index = self.tabs.addTab(view, "New Tab")
        self.tabs.setCurrentIndex(index)

        view.urlChanged.connect(lambda qurl, v=view: self.on_url_changed(qurl, v))
        view.titleChanged.connect(lambda title, v=view: self.on_title_changed(title, v))
        view.loadStarted.connect(lambda v=view: self._on_load_started(v))
        view.loadProgress.connect(lambda p, v=view: self._on_load_progress(p, v))
        view.loadFinished.connect(lambda ok, v=view: self._on_load_finished(ok, v))
        self._schedule_organize()

    def current_view(self) -> BlinkView:
        return self.tabs.currentWidget()

    def close_tab(self, index: int):
        if self.tabs.count() <= 1:
            self.close()
            return
        widget = self.tabs.widget(index)
        self.tabs.removeTab(index)
        if widget is not None:
            widget.shutdown()
            widget.deleteLater()
        self._schedule_organize()

    def _schedule_organize(self):
        self._organize_timer.start(250)

    def _toggle_auto_organize(self, enabled):
        self.settings["auto_organize_tabs"] = bool(enabled)
        _save_json(SETTINGS_FILE, self.settings)
        if enabled:
            self._organize_tabs(force=True)
        else:
            self._refresh_tab_groups()

    def _organize_tabs_now(self):
        self._organize_tabs(force=True)
        self.statusBar().showMessage("Tabs organized by site type", 2500)

    def _tab_categories(self):
        cats = []
        for i in range(self.tabs.count()):
            view = self.tabs.widget(i)
            manual = getattr(view, "manual_group", None) if view is not None else None
            if manual:
                cats.append(manual)
                continue
            url = view.url().toString() if view is not None else ""
            cats.append(classify_url(url))
        return cats

    def _organize_tabs(self, force=False):
        if self.tabs.count() == 0:
            self.group_strip.update_groups([], None)
            return
        cats = self._tab_categories()
        auto = self.settings.get("auto_organize_tabs", True)
        if auto or force:
            rank = {name: idx for idx, name in enumerate(TAB_CATEGORY_ORDER)}
            current = self.tabs.currentWidget()
            original = list(range(len(cats)))
            desired = sorted(
                original, key=lambda i: rank.get(cats[i], len(rank))
            )
            if desired != original:
                self._reorder_tabs(desired, current)
                cats = [cats[i] for i in desired]
        self._refresh_tab_groups(cats)

    def _reorder_tabs(self, desired_indices, current_widget):
        snapshots = []
        for i in range(self.tabs.count()):
            snapshots.append(
                (
                    self.tabs.widget(i),
                    self.tabs.tabText(i),
                    self.tabs.tabIcon(i),
                    self.tabs.tabToolTip(i),
                )
            )
        self.tabs.blockSignals(True)
        while self.tabs.count():
            self.tabs.removeTab(0)
        for i in desired_indices:
            widget, text, icon, tip = snapshots[i]
            index = self.tabs.addTab(widget, icon, text)
            self.tabs.setTabToolTip(index, tip)
        if current_widget is not None:
            self.tabs.setCurrentWidget(current_widget)
        self.tabs.blockSignals(False)

    def _refresh_tab_groups(self, cats=None):
        if cats is None:
            cats = self._tab_categories()
        tab_bar = self.tabs.tabBar()
        counts = {}
        for i, cat in enumerate(cats):
            meta_label = category_label(cat)
            color = category_color(cat)
            tab_bar.setTabData(i, cat)
            tab_bar.setTabTextColor(i, QColor(color))
            title = self.tabs.tabText(i)
            self.tabs.setTabToolTip(i, f"{meta_label} · {title}")
            tab_bar.setTabButton(
                i, QTabBar.ButtonPosition.LeftSide, _GroupDot(color)
            )
            counts[cat] = counts.get(cat, 0) + 1
        groups = [(c, counts[c]) for c in TAB_CATEGORY_ORDER if c in counts]
        current = None
        index = self.tabs.currentIndex()
        if 0 <= index < len(cats):
            current = cats[index]
        self.group_strip.update_groups(groups, current)
        tab_bar.update()

    def _highlight_current_group(self):
        index = self.tabs.currentIndex()
        if index < 0:
            return
        cat = self.tabs.tabBar().tabData(index)
        if not cat:
            view = self.tabs.widget(index)
            url = view.url().toString() if view is not None else ""
            cat = classify_url(url)
        self.group_strip.set_current(cat)

    def _jump_to_tab_group(self, cat):
        count = self.tabs.count()
        if count <= 0:
            return
        current = self.tabs.currentIndex()
        current_cat = self.tabs.tabBar().tabData(current) or "other"
        if current_cat == cat:
            start = current + 1
            for offset in range(count):
                index = (start + offset) % count
                if (self.tabs.tabBar().tabData(index) or "other") == cat:
                    self.tabs.setCurrentIndex(index)
                    return
            return
        for index in range(count):
            if (self.tabs.tabBar().tabData(index) or "other") == cat:
                self.tabs.setCurrentIndex(index)
                return

    def _close_tab_group(self, cat):
        indices = []
        for i in range(self.tabs.count()):
            data = self.tabs.tabBar().tabData(i)
            if not data:
                view = self.tabs.widget(i)
                url = view.url().toString() if view is not None else ""
                data = classify_url(url)
            if data == cat:
                indices.append(i)
        if not indices:
            return
        if self.tabs.count() - len(indices) < 1:
            indices = indices[:-1]
        if not indices:
            return
        self._organize_timer.stop()
        for i in reversed(indices):
            if self.tabs.count() <= 1:
                break
            widget = self.tabs.widget(i)
            self.tabs.removeTab(i)
            widget.deleteLater()
        self._organize_tabs()

    # ----------------------------------------------------------- navigation
    def home_url(self):
        engine = self.settings.get("search_engine", "google")
        meta = SEARCH_ENGINES.get(engine) or SEARCH_ENGINES["google"]
        return meta["home"]

    def search_url(self, text):
        engine = self.settings.get("search_engine", "google")
        meta = SEARCH_ENGINES.get(engine) or SEARCH_ENGINES["google"]
        query = QUrl.toPercentEncoding(text).data().decode()
        return meta["search"].format(query=query)

    def go_home(self):
        self.current_view().setUrl(QUrl(self.home_url()))

    def navigate_to_url(self):
        text = self.url_bar.text().strip()
        if not text:
            return
        if "." in text and " " not in text:
            url = text if "://" in text else f"https://{text}"
        else:
            url = self.search_url(text)
        self.current_view().setUrl(QUrl(url))

    # -------------------------------------------------------------- signals
    def on_tab_changed(self, index: int):
        view = self.tabs.widget(index)
        if view is not None:
            self.url_bar.setText(view.url().toString())
            self.update_window_title(view.title())
        self._attach_inspector()
        self._highlight_current_group()

    def on_url_changed(self, qurl: QUrl, view: BlinkView):
        if qurl.toString().startswith(("http://", "https://")):
            view._dino_page = False
        if view is self.current_view():
            self.url_bar.setText(qurl.toString())
            self.url_bar.setCursorPosition(0)
        self._schedule_organize()

    def on_title_changed(self, title: str, view: BlinkView):
        index = self.tabs.indexOf(view)
        if index != -1:
            label = title if title else "New Tab"
            self.tabs.setTabText(index, label[:24])
            cat = self.tabs.tabBar().tabData(index) or classify_url(
                view.url().toString()
            )
            self.tabs.setTabToolTip(index, f"{category_label(cat)} · {label}")
        if view is self.current_view():
            self.update_window_title(title)

    def _on_load_started(self, view):
        if view is self.current_view():
            self.progress.setValue(0)
            self.progress.show()

    def _on_load_progress(self, progress, view):
        if view is self.current_view():
            self.progress.setValue(progress)

    def _on_load_finished(self, ok, view):
        if view is self.current_view():
            self.progress.hide()
        if ok:
            self._record_history(view.url().toString(), view.title())
        else:
            self._show_offline_page(view)
        self.on_title_changed(view.title(), view)

    def _show_offline_page(self, view):
        # Replace failed http/https loads with our error page + dino game.
        failed_url = view.url().toString()
        if not failed_url.startswith(("http://", "https://")):
            return
        safe = failed_url.replace("\\", "\\\\").replace('"', '\\"')
        view._dino_page = True
        view.setHtml(OFFLINE_HTML.replace("%%URL%%", safe), QUrl("about:blank"))
        self.url_bar.clearFocus()
        view.setFocus()
        if view is self.current_view():
            QTimer.singleShot(50, view.setFocus)
            QTimer.singleShot(200, view.setFocus)
            QTimer.singleShot(50, lambda: self._install_dino_filter(view))
            QTimer.singleShot(300, lambda: self._install_dino_filter(view))
            QTimer.singleShot(800, lambda: self._install_dino_filter(view))

    def _refresh_dino_filter(self):
        view = self.current_view()
        if view is not None and getattr(view, "_dino_page", False):
            self._install_dino_filter(view)

    def _install_dino_filter(self, view):
        # Install ourselves as an event filter on the web view's actual input
        # widget(s). On Windows the keys go to a native child render widget, so
        # the QApplication-level filter alone never sees them.
        if view is None or not getattr(view, "_dino_page", False):
            return
        targets = []
        proxy = view.focusProxy()
        if proxy is not None:
            targets.append(proxy)
        targets.extend(view.findChildren(QWidget))
        for w in targets:
            w.installEventFilter(self)

    _DINO_JUMP_KEYS = (Qt.Key.Key_Space, Qt.Key.Key_Up)

    def eventFilter(self, watched, event):
        et = event.type()
        if self._manual_interacting:
            if et == QEvent.Type.MouseMove:
                pos = event.globalPosition().toPoint()
                if self._move_offset is not None:
                    self._update_manual_move(pos)
                elif self._resize_edges:
                    self._update_manual_resize(pos)
                return True
            if et in (QEvent.Type.MouseButtonRelease, QEvent.Type.Leave):
                if et == QEvent.Type.MouseButtonRelease:
                    self._end_manual_interaction()
                    return True
        # ShortcutOverride reaches the app-level filter even while QtWebEngine
        # holds native keyboard focus, so we act on it as well as KeyPress.
        if et in (
            QEvent.Type.KeyPress,
            QEvent.Type.KeyRelease,
            QEvent.Type.ShortcutOverride,
        ):
            view = self.current_view()
            if view is not None and getattr(view, "_dino_page", False):
                fw = QApplication.focusWidget()
                if isinstance(fw, (QLineEdit, QTextEdit, QComboBox)):
                    return super().eventFilter(watched, event)
                key = event.key()
                if key in self._DINO_JUMP_KEYS:
                    if et != QEvent.Type.KeyRelease:
                        self._run_dino_js("window.dinoJump")
                    event.accept()
                    return True
                if key == Qt.Key.Key_Down:
                    on = "true" if et != QEvent.Type.KeyRelease else "false"
                    self._run_dino_js("window.dinoDuck", on)
                    event.accept()
                    return True
        return super().eventFilter(watched, event)

    def _run_dino_js(self, fn, *args):
        view = self.current_view()
        if view is None or not getattr(view, "_dino_page", False):
            return
        call = "{}({})".format(fn, ",".join(args))
        view.page().runJavaScript(
            "if(typeof {0}==='function'){{{1};}}".format(fn, call)
        )

    def update_window_title(self, title: str):
        suffix = " - Insecret" if self.insecret else ""
        if title:
            self.setWindowTitle(f"{title} - PyQt6 Browser{suffix}")
        else:
            self.setWindowTitle(f"PyQt6 Browser{suffix}")

    # ------------------------------------------------------------ webboxes
    def _migrate_webboxes(self):
        changed = False
        for item in self.bookmarks:
            if "kind" not in item:
                item["kind"] = WEBBOX_MISC
                changed = True
        if changed and not self.insecret:
            _save_json(BOOKMARKS_FILE, self.bookmarks)

    def add_webbox(self, kind=WEBBOX_FAVORITE):
        if self.insecret:
            self.statusBar().showMessage("WebBoxes are not saved in Insecret mode", 2500)
            return
        view = self.current_view()
        url = view.url().toString() if view is not None else ""
        if not url or url == "about:blank":
            return
        title = (view.title() if view is not None else "") or url
        if any(b["url"] == url for b in self.bookmarks):
            self.statusBar().showMessage("Already a WebBox", 2000)
            return
        self.bookmarks.append({"title": title, "url": url, "kind": kind})
        _save_json(BOOKMARKS_FILE, self.bookmarks)
        self.refresh_library()
        label = "Favorite" if kind == WEBBOX_FAVORITE else "Miscellaneous"
        self.statusBar().showMessage(f"{label} WebBox: {title}", 2000)

    def _new_webbox_dialog(self):
        view = self.current_view()
        title = view.title() if view is not None else ""
        url = view.url().toString() if view is not None else ""
        dialog = WebBoxDialog(self, title, url, WEBBOX_FAVORITE)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        title, url, kind = dialog.values()
        if not url:
            return
        if "://" not in url:
            url = "https://" + url
        if any(b["url"] == url for b in self.bookmarks):
            self.statusBar().showMessage("Already a WebBox", 2000)
            return
        self.bookmarks.append(
            {"title": title or url, "url": url, "kind": kind or WEBBOX_MISC}
        )
        if not self.insecret:
            _save_json(BOOKMARKS_FILE, self.bookmarks)
        self.refresh_library()

    def _rebuild_bookmarks_menu(self):
        self.bookmarks_menu.clear()
        create = self.bookmarks_menu.addAction("New WebBox…")
        create.triggered.connect(self._new_webbox_dialog)
        fav = self.bookmarks_menu.addAction("Add current as Favorite WebBox")
        fav.setShortcut(QKeySequence("Ctrl+D"))
        fav.triggered.connect(lambda: self.add_webbox(WEBBOX_FAVORITE))
        misc = self.bookmarks_menu.addAction("Add current as Miscellaneous WebBox")
        misc.triggered.connect(lambda: self.add_webbox(WEBBOX_MISC))
        favorites = [b for b in self.bookmarks if b.get("kind") == WEBBOX_FAVORITE]
        others = [b for b in self.bookmarks if b.get("kind") != WEBBOX_FAVORITE]
        self.bookmarks_menu.addSeparator()
        self._fill_webbox_submenu(
            self.bookmarks_menu.addMenu("Favorite WebBoxes"), favorites
        )
        self._fill_webbox_submenu(
            self.bookmarks_menu.addMenu("Miscellaneous WebBoxes"), others
        )

    def _fill_webbox_submenu(self, menu, items):
        if not items:
            empty = menu.addAction("(empty)")
            empty.setEnabled(False)
            return
        for bm in items:
            entry = menu.addMenu(bm["title"][:40] or bm["url"])
            open_action = entry.addAction("Open")
            open_action.triggered.connect(
                lambda _checked, u=bm["url"]: self.current_view().setUrl(QUrl(u))
            )
            new_tab_action = entry.addAction("Open in new tab")
            new_tab_action.triggered.connect(
                lambda _checked, u=bm["url"]: self.add_tab(QUrl(u))
            )
            remove_action = entry.addAction("Remove")
            remove_action.triggered.connect(
                lambda _checked, u=bm["url"]: self._remove_bookmark(u)
            )

    def _remove_bookmark(self, url):
        self.bookmarks = [b for b in self.bookmarks if b["url"] != url]
        if not self.insecret:
            _save_json(BOOKMARKS_FILE, self.bookmarks)
        self.refresh_library()

    # -------------------------------------------------------------- history
    def _record_history(self, url, title):
        if self.insecret:
            return
        if not url or url == "about:blank":
            return
        if self.history and self.history[-1].get("url") == url:
            return
        self.history.append(
            {
                "url": url,
                "title": title or url,
                "visited": datetime.now().isoformat(timespec="seconds"),
            }
        )
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[-MAX_HISTORY:]
        _save_json(HISTORY_FILE, self.history)
        self.refresh_library()

    def _rebuild_history_menu(self):
        self.history_menu.clear()
        clear = self.history_menu.addAction("Clear history")
        clear.triggered.connect(self._clear_history)
        self.history_menu.addSeparator()
        if not self.history:
            empty = self.history_menu.addAction("(empty)")
            empty.setEnabled(False)
            return
        for item in reversed(self.history[-25:]):
            label = item.get("title") or item.get("url")
            action = self.history_menu.addAction(label[:60])
            action.setToolTip(item.get("url", ""))
            action.triggered.connect(
                lambda _checked, u=item["url"]: self.current_view().setUrl(QUrl(u))
            )

    def _clear_history(self):
        self.history = []
        _save_json(HISTORY_FILE, self.history)
        self.refresh_library()
        self.statusBar().showMessage("History cleared", 2000)

    # -------------------------------------------------------------- library
    def refresh_library(self):
        if not hasattr(self, "bookmarks_list"):
            return
        self.bookmarks_list.clear()
        favorites = [b for b in self.bookmarks if b.get("kind") == WEBBOX_FAVORITE]
        others = [b for b in self.bookmarks if b.get("kind") != WEBBOX_FAVORITE]
        if favorites:
            header = QListWidgetItem("Favorite WebBoxes")
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            self.bookmarks_list.addItem(header)
        for bm in favorites:
            item = QListWidgetItem("  " + (bm.get("title") or bm.get("url")))
            item.setToolTip(bm.get("url", ""))
            item.setData(ROLE_DATA, bm.get("url", ""))
            self.bookmarks_list.addItem(item)
        if others:
            header = QListWidgetItem("Miscellaneous WebBoxes")
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            self.bookmarks_list.addItem(header)
        for bm in others:
            item = QListWidgetItem("  " + (bm.get("title") or bm.get("url")))
            item.setToolTip(bm.get("url", ""))
            item.setData(ROLE_DATA, bm.get("url", ""))
            self.bookmarks_list.addItem(item)

        self.history_list.clear()
        for entry in reversed(self.history[-200:]):
            label = entry.get("title") or entry.get("url")
            item = QListWidgetItem(label)
            item.setToolTip(f"{entry.get('url', '')}\n{entry.get('visited', '')}")
            item.setData(ROLE_DATA, entry.get("url", ""))
            self.history_list.addItem(item)

    def _open_list_item(self, item: QListWidgetItem):
        url = item.data(ROLE_DATA)
        if url:
            self.current_view().setUrl(QUrl(url))

    # ------------------------------------------------------------ downloads
    def _on_download_requested(self, download):
        suggested = download.downloadFileName()
        default_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        os.makedirs(default_dir, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(
            self, "Save File", os.path.join(default_dir, suggested)
        )
        if not path:
            download.cancel()
            return
        download.setDownloadDirectory(os.path.dirname(path))
        download.setDownloadFileName(os.path.basename(path))
        download.accept()
        self.statusBar().showMessage(f"Downloading {os.path.basename(path)}\u2026")

    def _on_host_download_finished(self):
        self.statusBar().showMessage("Download finished", 4000)

    # ---------------------------------------------------------------- agent
    def _migrate_settings(self):
        self.settings.setdefault("provider", DEFAULT_PROVIDER)
        self.settings.setdefault("keys", {})
        self.settings.setdefault("models", {})
        self.settings.setdefault("auto_organize_tabs", True)
        self.settings.setdefault("search_engine", "google")
        self.settings.setdefault(
            "tab_strip_position", _load_tab_strip_position(self.settings)
        )
        # Migrate the old single-provider schema (api_key/model -> OpenAI).
        if "api_key" in self.settings:
            self.settings["keys"].setdefault("openai", self.settings.pop("api_key"))
        if "model" in self.settings and isinstance(self.settings["model"], str):
            self.settings["models"].setdefault("openai", self.settings.pop("model"))
        self.settings.setdefault("azure_endpoint", "")
        self.settings.setdefault("azure_deployment", "")
        self.settings.setdefault("azure_api_version", DEFAULT_AZURE_API_VERSION)
        _save_json(SETTINGS_FILE, self.settings)

    def _new_window(self):
        window = BrowserWindow(insecret=False)
        window.show()

    def _new_insecret_window(self):
        window = BrowserWindow(insecret=True)
        window.show()

    def _open_settings(self):
        dialog = SettingsDialog(self, self.settings.get("search_engine", "google"))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.settings["search_engine"] = dialog.search_engine()
        _save_json(SETTINGS_FILE, self.settings)
        self.statusBar().showMessage(
            "Search engine: " + SEARCH_ENGINES[self.settings["search_engine"]]["label"],
            2500,
        )

    def _assign_tab_group(self, cat):
        view = self.current_view()
        if view is None:
            return
        view.manual_group = cat
        self._organize_tabs(force=True)

    def _clear_manual_group(self):
        view = self.current_view()
        if view is None:
            return
        view.manual_group = None
        self._organize_tabs(force=True)

    def _next_tab(self):
        count = self.tabs.count()
        if count:
            self.tabs.setCurrentIndex((self.tabs.currentIndex() + 1) % count)

    def _prev_tab(self):
        count = self.tabs.count()
        if count:
            self.tabs.setCurrentIndex((self.tabs.currentIndex() - 1) % count)

    def _tab_bar_menu(self, pos):
        index = self.tabs.tabBar().tabAt(pos)
        menu = QMenu(self)
        if index >= 0:
            self.tabs.setCurrentIndex(index)
            assign = menu.addMenu("Assign to group")
            for cat in TAB_CATEGORY_ORDER:
                action = assign.addAction(category_label(cat))
                action.triggered.connect(
                    lambda _checked=False, c=cat: self._assign_tab_group(c)
                )
            menu.addAction("Clear manual group").triggered.connect(
                self._clear_manual_group
            )
            menu.addSeparator()
        dock = menu.addMenu("Dock tab bar")
        for key in TAB_STRIP_POSITIONS:
            action = dock.addAction(TAB_STRIP_LABELS[key])
            action.setCheckable(True)
            action.setChecked(key == self._tab_position)
            action.triggered.connect(
                lambda _checked=False, p=key: self._set_tab_strip_position(p)
            )
        menu.exec(self.tabs.tabBar().mapToGlobal(pos))

    def _dock_handle_menu(self, pos):
        menu = QMenu(self)
        dock = menu.addMenu("Dock tab bar")
        for key in TAB_STRIP_POSITIONS:
            action = dock.addAction(TAB_STRIP_LABELS[key])
            action.setCheckable(True)
            action.setChecked(key == self._tab_position)
            action.triggered.connect(
                lambda _checked=False, p=key: self._set_tab_strip_position(p)
            )
        menu.exec(self._dock_handle.mapToGlobal(pos))

    def _rebuild_apps_menu(self):
        self.apps_menu.clear()
        store = self.apps_menu.addAction("Open Chrome Web Store")
        store.setToolTip(
            "Blink loads unpacked Manifest extensions. Store-only APIs may differ."
        )
        store.triggered.connect(lambda: self.add_tab(QUrl(CHROME_WEB_STORE_URL)))
        install = self.apps_menu.addAction("Install this extension")
        current = ""
        view = self.current_view()
        if view is not None:
            current = view.url().toString()
        install.setEnabled(bool(parse_store_id(current)))
        install.triggered.connect(self._install_current_extension)
        self.apps_menu.addSeparator()
        installed = list_extensions()
        if not installed:
            empty = self.apps_menu.addAction("(no extensions installed)")
            empty.setEnabled(False)
            return
        for item in installed:
            entry = self.apps_menu.addMenu(item.get("name") or item.get("id"))
            remove = entry.addAction("Remove")
            remove.triggered.connect(
                lambda _checked=False, sid=item.get("id"): self._remove_extension(sid)
            )

    def _install_current_extension(self):
        view = self.current_view()
        if view is None:
            return
        url = view.url().toString()
        try:
            record = install_from_store_url(url)
        except Exception as exc:
            QMessageBox.warning(self, "Extension install failed", str(exc))
            return
        self._reload_blink_host()
        self.statusBar().showMessage("Installed " + record.get("name", "extension"), 4000)

    def _remove_extension(self, store_id):
        if not store_id:
            return
        remove_extension(store_id)
        self._reload_blink_host()
        self.statusBar().showMessage("Extension removed", 2500)

    def _reload_blink_host(self):
        urls = []
        current = self.tabs.currentIndex()
        for i in range(self.tabs.count()):
            view = self.tabs.widget(i)
            urls.append(view.url() if view is not None else QUrl(self.home_url()))
            if view is not None:
                view.shutdown()
        if self.host is BrowserWindow._shared_host:
            self.host.restart()
        else:
            self.host.restart()
        for i in range(self.tabs.count()):
            view = self.tabs.widget(i)
            if view is None:
                continue
            view.attach_target()
            view.setUrl(urls[i])
        self.tabs.setCurrentIndex(current)

    def closeEvent(self, event):
        for i in range(self.tabs.count()):
            view = self.tabs.widget(i)
            if view is not None:
                view.shutdown()
        if self in BrowserWindow._windows:
            BrowserWindow._windows.remove(self)
        if self.insecret:
            self.host.stop()
        elif not any(not w.insecret for w in BrowserWindow._windows):
            if BrowserWindow._shared_host is not None:
                BrowserWindow._shared_host.stop()
                BrowserWindow._shared_host = None
        event.accept()

    def _current_provider(self):
        return self.provider_combo.currentData() or DEFAULT_PROVIDER

    def llm_config(self):
        provider = self.settings.get("provider", DEFAULT_PROVIDER)
        model = (
            self.settings.get("models", {}).get(provider)
            or PROVIDERS[provider]["default_model"]
        )
        api_key = self.settings.get("keys", {}).get(provider, "")
        if provider == "cursor" and not api_key:
            api_key = os.environ.get("CURSOR_API_KEY", "")
        return {
            "provider": provider,
            "api_key": api_key,
            "model": model,
            "azure_endpoint": self.settings.get("azure_endpoint", ""),
            "azure_deployment": self.settings.get("azure_deployment", ""),
            "azure_api_version": self.settings.get(
                "azure_api_version", DEFAULT_AZURE_API_VERSION
            ),
        }

    def _on_provider_changed(self):
        self.settings["provider"] = self._current_provider()
        _save_json(SETTINGS_FILE, self.settings)
        self._sync_provider_ui()

    def _sync_provider_ui(self):
        provider = self._current_provider()
        model = (
            self.settings.get("models", {}).get(provider)
            or PROVIDERS[provider]["default_model"]
        )
        self.model_edit.setText(model)
        self.azure_button.setVisible(provider == "azure")
        self._update_key_status()

    def _set_api_key(self):
        provider = self._current_provider()
        label = PROVIDERS[provider]["label"]
        current = self.settings.get("keys", {}).get(provider, "")
        key, ok = QInputDialog.getText(
            self,
            f"{label} API Key",
            f"Enter your {label} API key (stored locally in plain text):",
            QLineEdit.EchoMode.Password,
            current,
        )
        if ok:
            self.settings.setdefault("keys", {})[provider] = key.strip()
            _save_json(SETTINGS_FILE, self.settings)
            self._update_key_status()

    def _azure_setup(self):
        endpoint, ok = QInputDialog.getText(
            self,
            "Azure endpoint",
            "Resource endpoint (e.g. https://my-resource.openai.azure.com):",
            QLineEdit.EchoMode.Normal,
            self.settings.get("azure_endpoint", ""),
        )
        if not ok:
            return
        deployment, ok = QInputDialog.getText(
            self,
            "Azure deployment",
            "Deployment name:",
            QLineEdit.EchoMode.Normal,
            self.settings.get("azure_deployment", ""),
        )
        if not ok:
            return
        version, ok = QInputDialog.getText(
            self,
            "Azure API version",
            "API version:",
            QLineEdit.EchoMode.Normal,
            self.settings.get("azure_api_version", DEFAULT_AZURE_API_VERSION),
        )
        if not ok:
            return
        self.settings["azure_endpoint"] = endpoint.strip().rstrip("/")
        self.settings["azure_deployment"] = deployment.strip()
        self.settings["azure_api_version"] = (
            version.strip() or DEFAULT_AZURE_API_VERSION
        )
        _save_json(SETTINGS_FILE, self.settings)
        self._update_key_status()

    def _save_model(self):
        provider = self._current_provider()
        model = self.model_edit.text().strip() or PROVIDERS[provider]["default_model"]
        self.settings.setdefault("models", {})[provider] = model
        _save_json(SETTINGS_FILE, self.settings)

    def _update_key_status(self):
        provider = self._current_provider()
        label = PROVIDERS[provider]["label"]
        has_key = bool(self.settings.get("keys", {}).get(provider))
        if provider == "cursor" and not has_key and os.environ.get("CURSOR_API_KEY"):
            has_key = True
        parts = [f"{label}: API key set \u2713" if has_key else f"{label}: no key"]
        if provider == "azure":
            if self.settings.get("azure_endpoint") and self.settings.get(
                "azure_deployment"
            ):
                parts.append("endpoint \u2713")
            else:
                parts.append("endpoint not set")
        self.key_status.setText("  |  ".join(parts))

    def _run_agent(self):
        instruction = self.agent_input.text().strip()
        if not instruction:
            return
        provider = self._current_provider()
        if not self.llm_config().get("api_key"):
            extra = (
                " (or set the CURSOR_API_KEY environment variable)"
                if provider == "cursor"
                else ""
            )
            QMessageBox.information(
                self,
                "API Key needed",
                f"Set your {PROVIDERS[provider]['label']} API key first{extra}.",
            )
            return
        self.agent_input.clear()
        self.agent.start(instruction)

    def _append_agent_log(self, role, message):
        colors = {
            "user": "#2563eb",
            "thought": "#6b7280",
            "action": "#059669",
            "system": "#9333ea",
            "error": "#dc2626",
        }
        color = colors.get(role, "#111827")
        self.agent_log.append(
            f'<span style="color:{color};"><b>{role}:</b> {message}</span>'
        )

    def _on_agent_running(self, running):
        self.run_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.agent_input.setEnabled(not running)


def main():
    app = QApplication(sys.argv)
    app.setOrganizationName(APP_ORG_NAME)
    app.setApplicationName(APP_NAME)
    # Fusion avoids GTK client-side decorations fighting a frameless window.
    if sys.platform.startswith("linux"):
        app.setStyle("Fusion")
    window = BrowserWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
