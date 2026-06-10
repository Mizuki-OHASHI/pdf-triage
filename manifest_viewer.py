#!/usr/bin/env python3

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import mimetypes
import os
import plistlib
import posixpath
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml


DEFAULT_PAPERS_ROOT = Path.home() / "Documents" / "Papers"
MANIFEST_NAME = "manifest.yaml"
CONFIG_NAME = "viewer_config.json"
VIEWERS = {"default", "skim", "preview", "browser"}
LAUNCH_AGENT_LABEL = "com.pdf-triage.manifest-viewer"


SCRIPT_DIR = Path(__file__).resolve().parent
INDEX_HTML = SCRIPT_DIR / "web" / "manifest_viewer.html"
FAVICON_SVG = SCRIPT_DIR / "web" / "favicon.svg"
LOG_DIR = SCRIPT_DIR / "logs"


def load_index_html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def load_yaml_or_json(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = yaml.safe_load(raw)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must contain a mapping")
    return data


def write_yaml_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(
                data,
                handle,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
                width=100,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()


@contextlib.contextmanager
def manifest_lock(root: Path):
    lock_dir = root / ".triage"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "manifest.lock"
    with lock_path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def pid_file_for(papers_root: Path) -> Path:
    return papers_root / ".triage" / "manifest_viewer.pid"


def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_pid_file(pid_file: Path) -> bool:
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid_file.unlink(missing_ok=True)
        return False

    if not pid_is_running(pid):
        pid_file.unlink(missing_ok=True)
        return False

    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        if not pid_is_running(pid):
            pid_file.unlink(missing_ok=True)
            return True
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    pid_file.unlink(missing_ok=True)
    return True


def write_pid_file(pid_file: Path) -> None:
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")


def remove_pid_file(pid_file: Path) -> None:
    with contextlib.suppress(OSError, ValueError):
        if int(pid_file.read_text(encoding="utf-8").strip()) == os.getpid():
            pid_file.unlink(missing_ok=True)


def launchctl(args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=check)


def launch_agent_program_args(args: argparse.Namespace, paths: dict[str, Path]) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--papers-root",
        str(paths["papers_root"]),
        "--manifest",
        str(paths["manifest"]),
        "--config",
        str(paths["config"]),
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--viewer",
        args.viewer,
        "--pid-file",
        str(paths["pid_file"]),
    ]
    if args.read_only:
        command.append("--read-only")
    return command


def install_launch_agent(args: argparse.Namespace, paths: dict[str, Path]) -> None:
    plist_path = launch_agent_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": launch_agent_program_args(args, paths),
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": str(SCRIPT_DIR),
        "StandardOutPath": str(LOG_DIR / "manifest_viewer.launchd.out.log"),
        "StandardErrorPath": str(LOG_DIR / "manifest_viewer.launchd.err.log"),
    }
    with plist_path.open("wb") as handle:
        plistlib.dump(payload, handle)

    domain = f"gui/{os.getuid()}"
    launchctl(["bootout", domain, str(plist_path)], check=False)
    stop_pid_file(paths["pid_file"])
    launchctl(["bootstrap", domain, str(plist_path)], check=True)
    launchctl(["kickstart", "-k", f"{domain}/{LAUNCH_AGENT_LABEL}"], check=False)
    print(f"Installed and started LaunchAgent: {plist_path}")


def uninstall_launch_agent(paths: dict[str, Path]) -> None:
    plist_path = launch_agent_path()
    domain = f"gui/{os.getuid()}"
    if plist_path.exists():
        launchctl(["bootout", domain, str(plist_path)], check=False)
        plist_path.unlink()
    stop_pid_file(paths["pid_file"])
    print(f"Uninstalled LaunchAgent: {plist_path}")


def restart_launch_agent(paths: dict[str, Path]) -> bool:
    plist_path = launch_agent_path()
    if not plist_path.exists():
        return False
    domain = f"gui/{os.getuid()}"
    launchctl(["bootout", domain, str(plist_path)], check=False)
    stop_pid_file(paths["pid_file"])
    launchctl(["bootstrap", domain, str(plist_path)], check=True)
    launchctl(["kickstart", "-k", f"{domain}/{LAUNCH_AGENT_LABEL}"], check=False)
    print(f"Restarted LaunchAgent: {plist_path}")
    return True


def start_background(args: argparse.Namespace, paths: dict[str, Path]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    command = launch_agent_program_args(args, paths)
    stdout = (LOG_DIR / "manifest_viewer.out.log").open("a", encoding="utf-8")
    stderr = (LOG_DIR / "manifest_viewer.err.log").open("a", encoding="utf-8")
    subprocess.Popen(command, cwd=str(SCRIPT_DIR), stdout=stdout, stderr=stderr, start_new_session=True)
    print(f"Started in background: http://{args.host}:{args.port}")


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store, max-age=0")
    handler.send_header("Pragma", "no-cache")
    handler.send_header("Expires", "0")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler: BaseHTTPRequestHandler, status: int, body: str, content_type: str) -> None:
    encoded = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Cache-Control", "no-store, max-age=0")
    handler.send_header("Pragma", "no-cache")
    handler.send_header("Expires", "0")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


def safe_join(root: Path, candidate: str) -> Path:
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        path = root / path
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
        raise ValueError(f"Path is outside papers root: {resolved_path}")
    return resolved_path


def normalize_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = [value]
    elif isinstance(value, list):
        raw_items = value
    else:
        return []

    seen: set[str] = set()
    tags: list[str] = []
    for item in raw_items:
        tag = str(item).strip()
        tag = " ".join(tag.split())
        if not tag:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag)
    return tags


def normalize_paper(root: Path, item: dict[str, Any]) -> dict[str, Any]:
    file_info = item.get("file") if isinstance(item.get("file"), dict) else {}
    ids = item.get("identifiers") if isinstance(item.get("identifiers"), dict) else {}
    bib = item.get("bibliographic") if isinstance(item.get("bibliographic"), dict) else {}
    triage = item.get("triage") if isinstance(item.get("triage"), dict) else {}

    authors_value = bib.get("authors") or []
    if isinstance(authors_value, list):
        authors = ", ".join(str(author) for author in authors_value if author)
    else:
        authors = str(authors_value)

    doi = ids.get("doi") or ""
    arxiv_id = ids.get("arxiv_id") or ""
    identifier = doi or arxiv_id
    current_path = str(file_info.get("current_path") or "")
    title = str(bib.get("title") or "")
    original_name = str(file_info.get("original_name") or Path(current_path).name)
    category = str(triage.get("category") or "")
    moved_at = str(triage.get("moved_at") or "")
    paper_id = str(item.get("id") or file_info.get("sha256") or current_path)
    tags = normalize_tags(item.get("tags"))
    tags_text = ", ".join(tags)

    search_text = " ".join(
        [title, authors, category, identifier, original_name, current_path, tags_text]
    ).lower()

    return {
        "id": paper_id,
        "title": title,
        "authors": authors,
        "category": category,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "identifier": identifier,
        "file": original_name,
        "path": current_path,
        "moved_at": moved_at,
        "tags": tags,
        "tags_text": tags_text,
        "size_bytes": file_info.get("size_bytes") or 0,
        "exists": bool(current_path and safe_join(root, current_path).exists()),
        "search_text": search_text,
    }


class ViewerState:
    def __init__(self, papers_root: Path, manifest: Path, config: Path, default_viewer: str, read_only: bool = False):
        self.papers_root = papers_root
        self.manifest = manifest
        self.config = config
        self.default_viewer = default_viewer
        self.read_only = read_only

    def load_manifest(self) -> dict[str, Any]:
        if not self.manifest.exists():
            return {"papers": []}
        return load_yaml_or_json(self.manifest)

    def papers(self) -> list[dict[str, Any]]:
        data = self.load_manifest()
        items = data.get("papers") or []
        if not isinstance(items, list):
            return []
        normalized = []
        for item in items:
            if isinstance(item, dict):
                try:
                    normalized.append(normalize_paper(self.papers_root, item))
                except ValueError:
                    continue
        return normalized

    def tags(self) -> list[str]:
        tag_set: dict[str, str] = {}
        for paper in self.papers():
            for tag in paper["tags"]:
                tag_set.setdefault(tag.casefold(), tag)
        return sorted(tag_set.values(), key=str.casefold)

    def find_paper(self, paper_id: str) -> dict[str, Any] | None:
        for paper in self.papers():
            if paper["id"] == paper_id:
                return paper
        return None

    def load_config(self) -> dict[str, Any]:
        viewer = self.default_viewer
        if self.config.exists():
            try:
                data = json.loads(self.config.read_text(encoding="utf-8"))
                if data.get("viewer") in VIEWERS:
                    viewer = data["viewer"]
            except (OSError, json.JSONDecodeError):
                pass
        return {"viewer": viewer, "read_only": self.read_only}

    def save_config(self, config: dict[str, Any]) -> dict[str, Any]:
        if self.read_only:
            raise PermissionError("Viewer is read-only")
        viewer = config.get("viewer")
        if viewer not in VIEWERS:
            raise ValueError(f"Unknown viewer: {viewer}")
        self.config.parent.mkdir(parents=True, exist_ok=True)
        payload = {"viewer": viewer}
        self.config.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return payload

    def update_tags(self, paper_id: str, tags: list[str]) -> dict[str, Any]:
        if self.read_only:
            raise PermissionError("Viewer is read-only")
        normalized_tags = normalize_tags(tags)
        with manifest_lock(self.papers_root):
            data = self.load_manifest()
            papers = data.get("papers") or []
            if not isinstance(papers, list):
                raise ValueError("manifest papers must be a list")

            for item in papers:
                if not isinstance(item, dict):
                    continue
                file_info = item.get("file") if isinstance(item.get("file"), dict) else {}
                item_id = str(item.get("id") or file_info.get("sha256") or file_info.get("current_path") or "")
                if item_id == paper_id:
                    item["tags"] = normalized_tags
                    data["updated_at"] = now_iso()
                    write_yaml_atomic(self.manifest, data)
                    return {"id": paper_id, "tags": normalized_tags, "all_tags": self.tags()}

        raise ValueError("Paper not found")


class ManifestHandler(BaseHTTPRequestHandler):
    state: ViewerState

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), format % args))

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        try:
            if path == "/":
                text_response(self, HTTPStatus.OK, load_index_html(), "text/html; charset=utf-8")
            elif path == "/favicon.svg":
                text_response(self, HTTPStatus.OK, FAVICON_SVG.read_text(encoding="utf-8"), "image/svg+xml")
            elif path == "/api/papers":
                data = self.state.load_manifest()
                papers = self.state.papers()
                json_response(
                    self,
                    HTTPStatus.OK,
                    {
                        "root": str(self.state.papers_root),
                        "manifest": str(self.state.manifest),
                        "updated_at": data.get("updated_at"),
                        "count": len(papers),
                        "read_only": self.state.read_only,
                        "tags": self.state.tags(),
                        "papers": papers,
                    },
                )
            elif path == "/api/config":
                json_response(self, HTTPStatus.OK, self.state.load_config())
            elif path == "/api/tags":
                json_response(self, HTTPStatus.OK, {"tags": self.state.tags()})
            elif path.startswith("/pdf/"):
                self.serve_pdf(path.removeprefix("/pdf/"))
            else:
                json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except Exception as exc:
            json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            body = self.read_json()
            if parsed.path == "/api/config":
                if self.state.read_only:
                    json_response(self, HTTPStatus.FORBIDDEN, {"error": "Viewer is read-only"})
                    return
                config = self.state.save_config(body)
                json_response(self, HTTPStatus.OK, config)
            elif parsed.path == "/api/open":
                self.open_pdf(body)
            elif parsed.path == "/api/open-manifest":
                if self.state.read_only:
                    json_response(self, HTTPStatus.FORBIDDEN, {"error": "Viewer is read-only"})
                    return
                self.open_manifest()
            elif parsed.path == "/api/tags":
                if self.state.read_only:
                    json_response(self, HTTPStatus.FORBIDDEN, {"error": "Viewer is read-only"})
                    return
                paper_id = str(body.get("id") or "")
                if not paper_id:
                    raise ValueError("Missing paper id")
                tags = body.get("tags")
                if not isinstance(tags, list):
                    raise ValueError("tags must be a list")
                json_response(self, HTTPStatus.OK, self.state.update_tags(paper_id, tags))
            else:
                json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except ValueError as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except PermissionError as exc:
            json_response(self, HTTPStatus.FORBIDDEN, {"error": str(exc)})
        except Exception as exc:
            json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def open_pdf(self, body: dict[str, Any]) -> None:
        paper_id = str(body.get("id") or "")
        if not paper_id:
            raise ValueError("Missing paper id")
        viewer = str(body.get("viewer") or self.state.load_config().get("viewer") or "default")
        if viewer not in VIEWERS:
            raise ValueError(f"Unknown viewer: {viewer}")

        paper = self.state.find_paper(paper_id)
        if paper is None:
            json_response(self, HTTPStatus.NOT_FOUND, {"error": "Paper not found"})
            return

        pdf_path = safe_join(self.state.papers_root, paper["path"])
        if not pdf_path.exists():
            json_response(self, HTTPStatus.NOT_FOUND, {"error": "PDF file not found"})
            return

        if viewer == "browser":
            url_id = urllib.parse.quote(paper_id, safe="")
            json_response(self, HTTPStatus.OK, {"ok": True, "viewer": viewer, "url": f"/pdf/{url_id}"})
            return

        command = ["open", str(pdf_path)]
        if viewer == "skim":
            command = ["open", "-a", "Skim", str(pdf_path)]
        elif viewer == "preview":
            command = ["open", "-a", "Preview", str(pdf_path)]
        subprocess.Popen(command)
        json_response(self, HTTPStatus.OK, {"ok": True, "viewer": viewer})

    def open_manifest(self) -> None:
        if not self.state.manifest.exists():
            json_response(self, HTTPStatus.NOT_FOUND, {"error": "Manifest file not found"})
            return
        subprocess.Popen(["open", str(self.state.manifest)])
        json_response(self, HTTPStatus.OK, {"ok": True, "path": str(self.state.manifest)})

    def serve_pdf(self, quoted_id: str) -> None:
        paper_id = urllib.parse.unquote(quoted_id)
        paper = self.state.find_paper(paper_id)
        if paper is None:
            json_response(self, HTTPStatus.NOT_FOUND, {"error": "Paper not found"})
            return
        pdf_path = safe_join(self.state.papers_root, paper["path"])
        if not pdf_path.exists():
            json_response(self, HTTPStatus.NOT_FOUND, {"error": "PDF file not found"})
            return

        content_type = mimetypes.guess_type(pdf_path.name)[0] or "application/pdf"
        size = pdf_path.stat().st_size
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'inline; filename="{pdf_path.name}"')
        self.end_headers()
        with pdf_path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local web viewer for pdf-triage manifest.yaml")
    parser.add_argument("--papers-root", type=Path, default=DEFAULT_PAPERS_ROOT)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--pid-file", type=Path, default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--viewer", choices=sorted(VIEWERS), default="default")
    parser.add_argument("--read-only", action="store_true", help="disable manifest/config writes from the viewer")
    parser.add_argument("--open-browser", action="store_true", help="open the viewer URL after startup")
    parser.add_argument("--stop", action="store_true", help="stop a running viewer process or LaunchAgent")
    parser.add_argument("--restart", action="store_true", help="restart LaunchAgent if installed, otherwise start in background")
    parser.add_argument("--status", action="store_true", help="print current pid/LaunchAgent status")
    parser.add_argument("--install-launch-agent", action="store_true", help="install and start a macOS LaunchAgent")
    parser.add_argument("--uninstall-launch-agent", action="store_true", help="stop and remove the macOS LaunchAgent")
    return parser


def resolve_runtime_paths(args: argparse.Namespace) -> dict[str, Path]:
    papers_root = args.papers_root.expanduser().resolve()
    manifest = args.manifest.expanduser().resolve() if args.manifest else papers_root / MANIFEST_NAME
    config = args.config.expanduser().resolve() if args.config else papers_root / ".triage" / CONFIG_NAME
    pid_file = args.pid_file.expanduser().resolve() if args.pid_file else pid_file_for(papers_root)
    return {
        "papers_root": papers_root,
        "manifest": manifest,
        "config": config,
        "pid_file": pid_file,
    }


def print_status(paths: dict[str, Path]) -> None:
    plist_path = launch_agent_path()
    pid_text = "not running"
    if paths["pid_file"].exists():
        try:
            pid = int(paths["pid_file"].read_text(encoding="utf-8").strip())
            pid_text = f"running pid {pid}" if pid_is_running(pid) else f"stale pid {pid}"
        except (OSError, ValueError):
            pid_text = "invalid pid file"
    print(f"pid_file={paths['pid_file']}")
    print(f"process={pid_text}")
    print(f"launch_agent={'installed' if plist_path.exists() else 'not installed'}")
    print(f"launch_agent_path={plist_path}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = resolve_runtime_paths(args)

    if args.status:
        print_status(paths)
        return 0

    if args.stop:
        plist_path = launch_agent_path()
        if plist_path.exists():
            domain = f"gui/{os.getuid()}"
            launchctl(["bootout", domain, str(plist_path)], check=False)
            print(f"Stopped LaunchAgent: {plist_path}")
        if stop_pid_file(paths["pid_file"]):
            print(f"Stopped process from pid file: {paths['pid_file']}")
        return 0

    if args.install_launch_agent:
        install_launch_agent(args, paths)
        return 0

    if args.uninstall_launch_agent:
        uninstall_launch_agent(paths)
        return 0

    if args.restart:
        if not restart_launch_agent(paths):
            stop_pid_file(paths["pid_file"])
            start_background(args, paths)
        return 0

    state = ViewerState(paths["papers_root"], paths["manifest"], paths["config"], args.viewer, args.read_only)
    handler = type("BoundManifestHandler", (ManifestHandler,), {"state": state})
    server = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{server.server_port}"
    write_pid_file(paths["pid_file"])

    print(f"Serving {paths['manifest']}")
    print(url)
    if args.open_browser:
        subprocess.Popen(["open", url])

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping")
    finally:
        server.server_close()
        remove_pid_file(paths["pid_file"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
