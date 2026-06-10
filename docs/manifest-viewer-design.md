# Manifest Viewer Plan

## Goal

Build a small local viewer for `~/Documents/Papers/manifest.yaml` that lets a
user browse papers, filter/search, sort columns, and open the selected PDF in a
configured viewer.

## MVP Scope

- Load `manifest.yaml`.
- Show a table with title, authors, category, DOI/arXiv ID, original filename,
  and moved date.
- Search across title, authors, DOI, arXiv ID, filename, and category.
- Sort by title, category, moved date, filename, and DOI/arXiv ID.
- Open PDF via one of:
  - Skim
  - Preview
  - default browser
  - system default PDF app
- Store viewer preference locally.
- Edit per-paper tags and store them in `manifest.yaml`.

No editing, annotation syncing, deduplication, or metadata repair in the MVP.

## Recommended Stack

Use a Python local web app with no Node dependency.

- Backend: Python standard library HTTP server.
- Manifest parsing: YAML via PyYAML.
- UI: plain HTML/CSS/JavaScript served by the Python process.
- Open command: macOS `open`.
- Config: `~/Documents/Papers/.triage/viewer_config.json`.

This fits the current project because the triage script is already Python, the
manifest is local, and opening PDFs is a macOS integration problem rather than a
web application problem.

## Proposed CLI

```bash
./manifest_viewer.py
./manifest_viewer.py --papers-root ~/Documents/Papers
./manifest_viewer.py --port 8765
./manifest_viewer.py --viewer skim
```

The script should print the local URL, for example:

```text
http://127.0.0.1:8765
```

## API Sketch

- `GET /` serves the viewer UI.
- `GET /api/papers` returns normalized manifest entries.
- `GET /api/config` returns the current viewer preference.
- `POST /api/config` updates the viewer preference.
- `POST /api/open` opens a PDF by manifest `id`.
- `POST /api/tags` updates a paper's `tags` list in `manifest.yaml`.

`POST /api/open` should never accept an arbitrary path directly from the
browser. It should accept a manifest `id`, look up the path server-side, and
only open files under the configured papers root.

## Viewer Modes

```json
{"viewer": "skim"}
{"viewer": "preview"}
{"viewer": "browser"}
{"viewer": "default"}
```

Mapping:

- `skim`: `open -a Skim <pdf>`
- `preview`: `open -a Preview <pdf>`
- `browser`: open the local `/pdf/<id>` route in the default browser
- `default`: `open <pdf>`

## Implementation Notes

- Keep all filtering and sorting client-side for the MVP.
- Normalize missing fields to empty strings in `/api/papers`.
- Keep vector embeddings and future LLM metadata out of this viewer initially.
- Add a `--dry-run-open` option only if needed for testing.

## Current Implementation

The MVP is implemented in `manifest_viewer.py`.

- Preferences are stored in `~/Documents/Papers/.triage/viewer_config.json`.
- The UI is intentionally compact: a toolbar, a table, and a status footer.
- No card layout or nested panels are used.
- The HTML lives in `web/manifest_viewer.html`.
- The favicon lives in `web/favicon.svg`.
- Keyboard behavior: `/` focuses search, arrow keys select rows, and Enter opens
  only when a row is selected.
- Search uses space-separated OR terms, and tags are included in search.
- Server management:
  - `manifest_viewer.py --restart` starts/restarts a background server.
  - `manifest_viewer.py --stop` stops a running server or LaunchAgent.
  - `manifest_viewer.py --install-launch-agent` installs a login-time macOS LaunchAgent.
  - `manifest_viewer.py --uninstall-launch-agent` removes it.
- Add a symlink to launch it from anywhere:

```bash
ln -s "$(pwd)/manifest_viewer.py" ~/.local/bin/manifest_viewer.py
```
