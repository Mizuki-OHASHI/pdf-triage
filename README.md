# pdf-triage

Small macOS Folder Action workflow for moving recognized research PDFs from
`~/Downloads` into `~/Documents/Papers`.

Recognized PDFs are moved to:

- `~/Documents/Papers/arxiv`
- `~/Documents/Papers/journal`

Unrecognized PDFs are left untouched. Each run appends one JSON line to
`logs/pdf_triage.log`.

## Requirements

- macOS with Folder Actions
- Python matching the shebang in `pdf_triage.py`
- PyYAML in that Python environment
- Poppler tools (`pdftotext`, `pdfinfo`)

The checked-in script currently uses:

```text
#!/usr/bin/env /Users/m_ohashi/miniforge3/envs/py311/bin/python
```

Edit that first line if your Python environment lives elsewhere.

For Poppler:

```bash
brew install poppler
```

The script looks in `/opt/homebrew/bin`, `/usr/local/bin`, and the inherited
`PATH`, because macOS Folder Actions may run with a narrower shell environment
than an interactive terminal.

For PyYAML:

```bash
conda install -n py311 PyYAML
```

## Install the CLI

From this repository:

```bash
REPO="$(pwd)"
chmod +x ./pdf_triage.py
mkdir -p ~/.local/bin
ln -s "$REPO/pdf_triage.py" ~/.local/bin/pdf_triage.py
```

The symlink direction matters: `~/.local/bin/pdf_triage.py` should point to the
repository script.

Check it:

```bash
ls -l ~/.local/bin/pdf_triage.py
~/.local/bin/pdf_triage.py --help
```

For the manifest viewer, add a second symlink:

```bash
chmod +x ./manifest_viewer.py
ln -s "$REPO/manifest_viewer.py" ~/.local/bin/manifest_viewer.py
```

## Install the Folder Action

The source AppleScript lives at
`scripts/pdf_triage_folder_action.applescript`.

Compile it into the standard Folder Action Scripts directory. Do not copy the
text source as a `.scpt` file; the installed `.scpt` should be produced with
`osacompile`.

```bash
mkdir -p "$HOME/Library/Scripts/Folder Action Scripts"
osacompile \
  -o "$HOME/Library/Scripts/Folder Action Scripts/pdf_triage.scpt" \
  scripts/pdf_triage_folder_action.applescript
```

The compiled `.scpt` step is important. A plain-text file named `.scpt` may not
fire reliably as a Folder Action.

## Enable Downloads Auto-Trigger

Enable Folder Actions and attach `pdf_triage.scpt` to `~/Downloads`:

```bash
osascript \
  -e 'tell application "System Events"' \
  -e 'set folder actions enabled to true' \
  -e 'if not (exists folder action "Downloads") then make new folder action at end of folder actions with properties {path:POSIX file "'"$HOME"'/Downloads"}' \
  -e 'set enabled of folder action "Downloads" to true' \
  -e 'tell folder action "Downloads"' \
  -e 'if not (exists script "pdf_triage.scpt") then make new script at end of scripts with properties {name:"pdf_triage.scpt"}' \
  -e 'set enabled of script "pdf_triage.scpt" to true' \
  -e 'end tell' \
  -e 'end tell'
```

Verify:

```bash
osascript \
  -e 'tell application "System Events"' \
  -e 'set faEnabled to folder actions enabled' \
  -e 'set scriptNames to name of every script of folder action "Downloads"' \
  -e 'return "folder_actions_enabled=" & faEnabled & "; downloads_scripts=" & scriptNames' \
  -e 'end tell'
```

Expected:

```text
folder_actions_enabled=true; downloads_scripts=pdf_triage.scpt
```

## Debugging

Watch the execution log while downloading a PDF:

```bash
tail -f ./logs/pdf_triage.log
```

Test a file manually without moving it:

```bash
~/.local/bin/pdf_triage.py --dry-run ~/Downloads/example.pdf
```

Run the real move manually:

```bash
~/.local/bin/pdf_triage.py ~/Downloads/example.pdf
```

Folder Actions only trigger when a file is added. Files already in `~/Downloads`
must be processed manually.

## Manifest Viewer

Start the local viewer:

```bash
manifest_viewer.py
```

Then open:

```text
http://127.0.0.1:8765
```

Useful options:

```bash
manifest_viewer.py --viewer skim
manifest_viewer.py --viewer preview
manifest_viewer.py --viewer browser
manifest_viewer.py --papers-root ~/Documents/Papers --port 8765
```

The viewer preference is persisted at:

```text
~/Documents/Papers/.triage/viewer_config.json
```

The viewer UI lives in `web/manifest_viewer.html`; the Python script only serves
the page, manifest API, preferences API, and PDF open endpoint.

Keyboard:

- `/`: focus search
- `Up` / `Down`: select a row
- `Enter`: open the selected row
