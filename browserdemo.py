"""A tabbed web browser built with PyQt6 and QtWebEngine.

Features: tabs, automatic tab groups by site type (shopping, travel, …),
navigation, smart address bar, persistent cookies, bookmarks, browsing
history, a page-load progress bar, a library sidebar, download handling,
and an LLM-powered browsing agent.
"""

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime

from PyQt6.QtCore import QEvent, QObject, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QKeySequence, QPainter
from PyQt6.QtWebEngineCore import (
    QWebEngineDownloadRequest,
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineSettings,
)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTabBar,
    QTabWidget,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

HOME_URL = "https://www.google.com"

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser_data")
BOOKMARKS_FILE = os.path.join(DATA_DIR, "bookmarks.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
PROFILE_DIR = os.path.join(DATA_DIR, "profile")

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
    """Tab bar that paints a category-colored bar under consecutive groups."""

    def paintEvent(self, event):
        super().paintEvent(event)
        count = self.count()
        if count == 0:
            return
        painter = QPainter(self)
        painter.setPen(Qt.PenStyle.NoPen)
        index = 0
        while index < count:
            cat = self.tabData(index) or "other"
            end = index + 1
            while end < count and (self.tabData(end) or "other") == cat:
                end += 1
            left = self.tabRect(index)
            right = self.tabRect(end - 1)
            painter.setBrush(QColor(category_color(cat)))
            painter.drawRect(
                left.x() + 2,
                left.bottom() - 3,
                max(0, right.right() - left.x() - 3),
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


class BrowserWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PyQt6 Browser")
        self.resize(1280, 820)

        os.makedirs(PROFILE_DIR, exist_ok=True)

        # Persistent profile so cookies, cache and logins survive restarts.
        self.profile = QWebEngineProfile("pyqt6-browser", self)
        self.profile.setPersistentStoragePath(PROFILE_DIR)
        self.profile.setCachePath(os.path.join(PROFILE_DIR, "cache"))
        self.profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
        )
        self.profile.downloadRequested.connect(self._on_download_requested)

        self.bookmarks = _load_json(BOOKMARKS_FILE, [])
        self.history = _load_json(HISTORY_FILE, [])
        self.settings = _load_json(SETTINGS_FILE, {})
        self._migrate_settings()

        self.tabs = QTabWidget()
        self.tabs.setTabBar(GroupedTabBar(self.tabs))
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self.on_tab_changed)

        self.group_strip = TabGroupStrip()
        self.group_strip.group_selected.connect(self._jump_to_tab_group)
        self.group_strip.close_group_requested.connect(self._close_tab_group)

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self.group_strip)
        central_layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

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
        QApplication.instance().installEventFilter(self)
        # QtWebEngine recreates its native render widget (the view's focusProxy)
        # on load/focus changes; keep re-installing our key filter on it so the
        # dino game keeps receiving Space/Up/Down even when the web view has
        # native keyboard focus.
        self._dino_filter_timer = QTimer(self)
        self._dino_filter_timer.timeout.connect(self._refresh_dino_filter)
        self._dino_filter_timer.start(300)
        self.add_tab(QUrl(HOME_URL))

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
        reload.setShortcut(QKeySequence.StandardKey.Refresh)
        reload.triggered.connect(lambda: self.current_view().reload())
        nav.addAction(reload)

        home = QAction("Home", self)
        home.triggered.connect(self.go_home)
        nav.addAction(home)

        self.url_bar = QLineEdit()
        self.url_bar.setPlaceholderText("Search or enter address")
        self.url_bar.setClearButtonEnabled(True)
        self.url_bar.returnPressed.connect(self.navigate_to_url)
        nav.addWidget(self.url_bar)

        star = QAction("\u2606 Bookmark", self)
        star.setShortcut(QKeySequence("Ctrl+D"))
        star.triggered.connect(self.add_bookmark)
        nav.addAction(star)

        new_tab = QAction("New Tab", self)
        new_tab.setShortcut(QKeySequence.StandardKey.AddTab)
        new_tab.triggered.connect(lambda: self.add_tab(QUrl(HOME_URL)))
        nav.addAction(new_tab)

        inspect = QAction("Inspect", self)
        inspect.setShortcut(QKeySequence("F12"))
        inspect.triggered.connect(self.toggle_inspector)
        nav.addAction(inspect)

    def _build_menus(self):
        menubar = self.menuBar()

        self.bookmarks_menu = QMenu("Bookmarks", self)
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
        self.view_menu.addSeparator()

        self.apps_menu = QMenu("Apps", self)
        menubar.addMenu(self.apps_menu)
        store = self.apps_menu.addAction("Open Chrome Web Store")
        store.triggered.connect(
            lambda: self.add_tab(QUrl(CHROME_WEB_STORE_URL))
        )

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
        tabs.addTab(self.bookmarks_list, "Bookmarks")

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
        self.devtools_view = QWebEngineView()
        self.devtools_view.setPage(QWebEnginePage(self.profile, self.devtools_view))
        dock.setWidget(self.devtools_view)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        dock.hide()
        self.inspector_dock = dock
        self.view_menu.addAction(dock.toggleViewAction())
        dock.visibilityChanged.connect(self._on_inspector_visibility)

    def toggle_inspector(self):
        if self.inspector_dock.isVisible():
            self.inspector_dock.hide()
        else:
            self.inspector_dock.show()
            self._attach_inspector()

    def _on_inspector_visibility(self, visible):
        if visible:
            self._attach_inspector()

    def _attach_inspector(self):
        if not getattr(self, "inspector_dock", None) or not self.inspector_dock.isVisible():
            return
        view = self.current_view()
        if view is not None and view is not self.devtools_view:
            view.page().setDevToolsPage(self.devtools_view.page())

    # ----------------------------------------------------------------- tabs
    def add_tab(self, url: QUrl):
        view = QWebEngineView()
        view.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        view._dino_page = False
        view.setPage(QWebEnginePage(self.profile, view))
        settings = view.settings()
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.ErrorPageEnabled, False
        )
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.FocusOnNavigationEnabled, True
        )
        view.setUrl(url)
        index = self.tabs.addTab(view, "New Tab")
        self.tabs.setCurrentIndex(index)

        view.urlChanged.connect(lambda qurl, v=view: self.on_url_changed(qurl, v))
        view.titleChanged.connect(lambda title, v=view: self.on_title_changed(title, v))
        view.loadStarted.connect(lambda v=view: self._on_load_started(v))
        view.loadProgress.connect(lambda p, v=view: self._on_load_progress(p, v))
        view.loadFinished.connect(lambda ok, v=view: self._on_load_finished(ok, v))
        self._schedule_organize()

    def current_view(self) -> QWebEngineView:
        return self.tabs.currentWidget()

    def close_tab(self, index: int):
        if self.tabs.count() <= 1:
            self.close()
            return
        widget = self.tabs.widget(index)
        self.tabs.removeTab(index)
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
    def go_home(self):
        self.current_view().setUrl(QUrl(HOME_URL))

    def navigate_to_url(self):
        text = self.url_bar.text().strip()
        if not text:
            return
        if "." in text and " " not in text:
            url = text if "://" in text else f"https://{text}"
        else:
            query = QUrl.toPercentEncoding(text).data().decode()
            url = f"https://www.google.com/search?q={query}"
        self.current_view().setUrl(QUrl(url))

    # -------------------------------------------------------------- signals
    def on_tab_changed(self, index: int):
        view = self.tabs.widget(index)
        if view is not None:
            self.url_bar.setText(view.url().toString())
            self.update_window_title(view.title())
        self._attach_inspector()
        self._highlight_current_group()

    def on_url_changed(self, qurl: QUrl, view: QWebEngineView):
        if qurl.toString().startswith(("http://", "https://")):
            view._dino_page = False
        if view is self.current_view():
            self.url_bar.setText(qurl.toString())
            self.url_bar.setCursorPosition(0)
        self._schedule_organize()

    def on_title_changed(self, title: str, view: QWebEngineView):
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
        self.setWindowTitle(f"{title} - PyQt6 Browser" if title else "PyQt6 Browser")

    # ------------------------------------------------------------ bookmarks
    def add_bookmark(self):
        view = self.current_view()
        url = view.url().toString()
        if not url or url == "about:blank":
            return
        title = view.title() or url
        if any(b["url"] == url for b in self.bookmarks):
            self.statusBar().showMessage("Already bookmarked", 2000)
            return
        self.bookmarks.append({"title": title, "url": url})
        _save_json(BOOKMARKS_FILE, self.bookmarks)
        self.refresh_library()
        self.statusBar().showMessage(f"Bookmarked: {title}", 2000)

    def _rebuild_bookmarks_menu(self):
        self.bookmarks_menu.clear()
        add = self.bookmarks_menu.addAction("Add current page  (Ctrl+D)")
        add.triggered.connect(self.add_bookmark)
        if self.bookmarks:
            self.bookmarks_menu.addSeparator()
        for bm in self.bookmarks:
            entry = self.bookmarks_menu.addMenu(bm["title"][:40] or bm["url"])
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
        _save_json(BOOKMARKS_FILE, self.bookmarks)
        self.refresh_library()

    # -------------------------------------------------------------- history
    def _record_history(self, url, title):
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
        for bm in self.bookmarks:
            item = QListWidgetItem(bm.get("title") or bm.get("url"))
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
        name = os.path.basename(path)
        self.statusBar().showMessage(f"Downloading {name}\u2026")
        download.isFinishedChanged.connect(
            lambda d=download, n=name: self._on_download_finished(d, n)
        )

    def _on_download_finished(self, download, name):
        state = download.state()
        if state == QWebEngineDownloadRequest.DownloadState.DownloadCompleted:
            self.statusBar().showMessage(f"Downloaded {name}", 4000)
        elif state == QWebEngineDownloadRequest.DownloadState.DownloadInterrupted:
            self.statusBar().showMessage(f"Download failed: {name}", 4000)

    # ---------------------------------------------------------------- agent
    def _migrate_settings(self):
        self.settings.setdefault("provider", DEFAULT_PROVIDER)
        self.settings.setdefault("keys", {})
        self.settings.setdefault("models", {})
        self.settings.setdefault("auto_organize_tabs", True)
        # Migrate the old single-provider schema (api_key/model -> OpenAI).
        if "api_key" in self.settings:
            self.settings["keys"].setdefault("openai", self.settings.pop("api_key"))
        if "model" in self.settings and isinstance(self.settings["model"], str):
            self.settings["models"].setdefault("openai", self.settings.pop("model"))
        self.settings.setdefault("azure_endpoint", "")
        self.settings.setdefault("azure_deployment", "")
        self.settings.setdefault("azure_api_version", DEFAULT_AZURE_API_VERSION)
        _save_json(SETTINGS_FILE, self.settings)

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
    window = BrowserWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
