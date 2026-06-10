#!/bin/sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
install_dir=${INSTALL_DIR:-"$HOME/.local/bin"}
python_bin=${PYTHON:-}
manifest_viewer_read_only=${MANIFEST_VIEWER_READ_ONLY:-0}

if [ -z "$python_bin" ]; then
  if command -v python3 >/dev/null 2>&1; then
    python_bin=$(command -v python3)
  else
    echo "python3 was not found. Set PYTHON=/path/to/python and retry." >&2
    exit 1
  fi
fi

python_bin=$("$python_bin" -c 'import sys; print(sys.executable)')
"$python_bin" -c 'import yaml' >/dev/null 2>&1 || {
  echo "PyYAML is not importable from: $python_bin" >&2
  echo "Install PyYAML in that environment or rerun with PYTHON=/path/to/python." >&2
  exit 1
}

mkdir -p "$install_dir"

case "$manifest_viewer_read_only" in
  1|true|yes)
    manifest_viewer_args=" --read-only"
    ;;
  0|false|no)
    manifest_viewer_args=""
    ;;
  *)
    echo "MANIFEST_VIEWER_READ_ONLY must be 1/true/yes or 0/false/no." >&2
    exit 1
    ;;
esac

write_wrapper() {
  name=$1
  script=$2
  extra_args=$3
  dest="$install_dir/$name"
  tmp="$dest.tmp.$$"

  {
    printf '%s\n' '#!/bin/sh'
    printf 'exec "%s" "%s"%s "$@"\n' "$python_bin" "$script" "$extra_args"
  } > "$tmp"

  chmod +x "$tmp"
  mv "$tmp" "$dest"
  echo "installed $dest -> $python_bin $script"
}

write_wrapper "pdf_triage.py" "$repo_dir/pdf_triage.py" ""
write_wrapper "manifest_viewer.py" "$repo_dir/manifest_viewer.py" "$manifest_viewer_args"
