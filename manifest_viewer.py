#!/usr/bin/env /Users/m_ohashi/miniforge3/envs/py311/bin/python

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import posixpath
import subprocess
import sys
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


DEFAULT_PAPERS_ROOT = Path.home() / "Documents" / "Papers"
MANIFEST_NAME = "manifest.yaml"
CONFIG_NAME = "viewer_config.json"
VIEWERS = {"default", "skim", "preview", "browser"}


SCRIPT_DIR = Path(__file__).resolve().parent
INDEX_HTML = SCRIPT_DIR / "web" / "manifest_viewer.html"
FAVICON_SVG = SCRIPT_DIR / "web" / "favicon.svg"


def load_index_html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def load_yaml_or_json(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise RuntimeError(f"{path} requires PyYAML to read YAML") from exc
        data = yaml.safe_load(raw)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must contain a mapping")
    return data


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler: BaseHTTPRequestHandler, status: int, body: str, content_type: str) -> None:
    encoded = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
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

    search_text = " ".join(
        [title, authors, category, identifier, original_name, current_path]
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
        "size_bytes": file_info.get("size_bytes") or 0,
        "exists": bool(current_path and safe_join(root, current_path).exists()),
        "search_text": search_text,
    }


class ViewerState:
    def __init__(self, papers_root: Path, manifest: Path, config: Path, default_viewer: str):
        self.papers_root = papers_root
        self.manifest = manifest
        self.config = config
        self.default_viewer = default_viewer

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
        return {"viewer": viewer}

    def save_config(self, config: dict[str, Any]) -> dict[str, Any]:
        viewer = config.get("viewer")
        if viewer not in VIEWERS:
            raise ValueError(f"Unknown viewer: {viewer}")
        self.config.parent.mkdir(parents=True, exist_ok=True)
        payload = {"viewer": viewer}
        self.config.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return payload


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
                        "papers": papers,
                    },
                )
            elif path == "/api/config":
                json_response(self, HTTPStatus.OK, self.state.load_config())
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
                config = self.state.save_config(body)
                json_response(self, HTTPStatus.OK, config)
            elif parsed.path == "/api/open":
                self.open_pdf(body)
            elif parsed.path == "/api/open-manifest":
                self.open_manifest()
            else:
                json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except ValueError as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
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
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--viewer", choices=sorted(VIEWERS), default="default")
    parser.add_argument("--open-browser", action="store_true", help="open the viewer URL after startup")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    papers_root = args.papers_root.expanduser().resolve()
    manifest = args.manifest.expanduser().resolve() if args.manifest else papers_root / MANIFEST_NAME
    config = args.config.expanduser().resolve() if args.config else papers_root / ".triage" / CONFIG_NAME

    state = ViewerState(papers_root, manifest, config, args.viewer)
    handler = type("BoundManifestHandler", (ManifestHandler,), {"state": state})
    server = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{server.server_port}"

    print(f"Serving {manifest}")
    print(url)
    if args.open_browser:
        subprocess.Popen(["open", url])

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
