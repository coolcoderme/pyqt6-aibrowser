# AGENTS.md

## Cursor Cloud specific instructions

`browserdemo.py` is a single-file **PyQt6 + QtWebEngine desktop web browser** (with an optional LLM browsing agent). There is no server, database, port, or build step — it is run directly as a GUI application.

### Running the app

- Run it against the live VNC display, which is where computer-use / manual testing observes the GUI:
  - `DISPLAY=:1 python3 browserdemo.py`
- The window title becomes `<page title> - PyQt6 Browser` once loaded (home page is Google).
- On startup you will see non-fatal `Failed to create Vulkan instance` / GPU `ContextResult::kTransientFailure` messages. These are expected in this headless-GPU VM; QtWebEngine falls back to software rendering and pages still render correctly. Do not treat them as failures.
- Runtime state (bookmarks, history, settings, cookies, Qt profile) is written to `browser_data/` next to the script (git-ignored).

### Non-obvious gotchas

- The left "Library" sidebar's Bookmarks list does not always repaint immediately after adding a bookmark. The bookmark is still saved to `browser_data/bookmarks.json` right away — switching the sidebar between the History and Bookmarks tabs forces it to refresh. Prefer verifying persistence via `browser_data/bookmarks.json` / `history.json` rather than relying solely on the sidebar visual.

### Lint

- Two linters are configured; run them via the module form so they work regardless of PATH:
  - `python3 -m flake8 browserdemo.py` (config: `.flake8`)
  - `python3 -m pylint browserdemo.py` (config: `.pylintrc`)
- Note: the current committed code already has some pre-existing findings (e.g. `E501` long lines from flake8; pylint rates ~9.93/10 and exits non-zero due to convention/warning messages). These are baseline, not introduced by setup.

### Tests / build

- There is no test suite and no build/packaging step in this repo.

### Optional LLM agent

- The right-hand "AI Agent" panel calls external LLM APIs (OpenAI, Anthropic, Google, Groq, OpenRouter, Featherless, Azure) and requires an API key set in-app; it is optional and the browser works fully without it. The "Cursor SDK" provider additionally requires `pip install cursor-sdk`.
