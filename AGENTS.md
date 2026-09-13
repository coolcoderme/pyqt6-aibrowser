# AGENTS.md

## Cursor Cloud specific instructions

`browserdemo.py` is a **PyQt6 desktop browser** whose pages are rendered by a
local **Blink (Chromium)** process in [`pyblink/`](pyblink/). There is
no HTTP server, database, or build step.

### Running the app

- Chromium/Chrome must be installed (`google-chrome` is present in this
  environment). Override the binary with `CHROME_PATH` if needed.
- Run it against the live VNC display:
  - `DISPLAY=:1 python3 browserdemo.py`
- The window title becomes `<page title> - PyQt6 Browser` once loaded.
- Startup may print Chromium GPU warnings; the engine is launched headless
  with `--disable-gpu` and pages still render via CDP screencast. Do not
  treat those messages as failures.
- Runtime state (WebBoxes, history, settings, Blink profile, extensions)
  is written to `browser_data/` next to the script (git-ignored).

### Features

- Tabs are auto-grouped by site type. Manual assignment is under
  **View → Assign current tab to** and the tab context menu.
- Bookmarks are called **WebBoxes** (Favorite / Miscellaneous).
- **Settings → Preferences** chooses the search engine (Home / new tab /
  address-bar search).
- **File → New Insecret Window** (`Ctrl+Shift+N`) is a private session.
- **Apps** can open the Chrome Web Store and install an extension from a
  store detail page (CRX unpack + `--load-extension`).

### Non-obvious gotchas

- The left Library sidebar's WebBoxes list may not repaint immediately
  after adding one. Persistence is in `browser_data/bookmarks.json` —
  switching Library tabs forces a refresh.
- Installing or removing an extension restarts the Chromium process and
  reloads open tabs.

### Lint

- `python3 -m flake8 browserdemo.py pyblink` (config: `.flake8`)
- `python3 -m pylint browserdemo.py pyblink` (config: `.pylintrc`)
- Baseline: flake8 `E501` on the offline-page HTML; pylint ~9.9/10 and
  may exit non-zero on convention messages.

### Tests / build

- There is no test suite and no build/packaging step in this repo.

### Optional LLM agent

- The right-hand "AI Agent" panel calls external LLM APIs and needs an
  API key set in-app. The "Cursor SDK" provider additionally requires
  `pip install cursor-sdk`.
