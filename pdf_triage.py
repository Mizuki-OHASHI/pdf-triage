#!/usr/bin/env /Users/m_ohashi/miniforge3/envs/py311/bin/python

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


SCRIPT_NAME = "pdf_triage.py"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_PAPERS_ROOT = Path.home() / "Documents" / "Papers"
DEFAULT_LOG_PATH = SCRIPT_DIR / "logs" / "pdf_triage.log"
MANIFEST_NAME = "manifest.yaml"
SCHEMA_VERSION = 1
TOOL_PATH = os.pathsep.join(
    [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
        os.environ.get("PATH", ""),
    ]
)

ARXIV_ID_RE = re.compile(
    r"(?i)(?:arxiv\s*:\s*|arxiv\.org/(?:abs|pdf)/)"
    r"([a-z\-]+(?:\.[a-z]{2})?/\d{7}|\d{4}\.\d{4,5}(?:v\d+)?)"
)
ARXIV_FILENAME_RE = re.compile(r"^\d{4}\.\d{4,5}(?:v\d+)?\.pdf$", re.IGNORECASE)
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)

PUBLISHER_RE = re.compile(
    "|".join(
        re.escape(item)
        for item in [
            "doi.org",
            "sciencedirect.com",
            "springer.com",
            "link.springer.com",
            "nature.com",
            "journals.aps.org",
            "aps.org",
            "aip.scitation.org",
            "scitation.org",
            "wiley.com",
            "onlinelibrary.wiley.com",
            "tandfonline.com",
            "pubs.acs.org",
            "acs.org",
            "rsc.org",
            "academic.oup.com",
            "cambridge.org",
            "ieeexplore.ieee.org",
            "dl.acm.org",
            "iopscience.iop.org",
            "frontiersin.org",
            "mdpi.com",
            "pnas.org",
            "cell.com",
            "science.org",
            "plos.org",
            "jstor.org",
            "annualreviews.org",
        ]
    ),
    re.IGNORECASE,
)

NOISE_TITLE_RE = re.compile(
    r"(?i)\b(abstract|introduction|keywords?|contents|references|bibliography|"
    r"copyright|all rights reserved|downloaded from|accepted manuscript)\b"
)
AFFILIATION_RE = re.compile(
    r"(?i)\b(university|institute|department|laboratory|lab\.|faculty|school|"
    r"college|center|centre|academy|corporation|inc\.|ltd\.|gmbh|"
    r"universit[a-zäéàà̈]*|institut|rechenzentrum|division|national lab|"
    r"riken|laboratoire|research center)\b"
)
HEADER_LINE_RE = re.compile(
    r"(?i)("
    r"\brapid communications\b|"
    r"\bphysical review\s+[a-z]?\s*\d+\b|"
    r"\bj\.\s*phys\.\s*soc\.\s*jpn\.|"
    r"\bjournal of the physical society of japan\s+\d+\b|"
    r"\bvolume\s+\d+\b|"
    r"\bvol\.\s*\d+\b|"
    r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{4}\b|"
    r"\b\d+\s*\(\d{4}\)\s*\d+\s*[-–]\s*\d+\b|"
    r"\bwww\.|"
    r"\bdoi\s*:\s*10\."
    r")"
)
AUTHOR_STOP_RE = re.compile(
    r"(?i)^[^A-Za-z]*(abstract|received|accepted|available online|published|doi\s*:|pacs\b|contents\b|"
    r"program summary|title of library|catalogue identifier)\b"
)


@dataclass(frozen=True)
class Classification:
    category: str | None
    reason: str
    arxiv_id: str | None = None
    doi: str | None = None


@dataclass(frozen=True)
class Extraction:
    title: str | None
    authors: list[str]
    method: str
    confidence: float


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def log_line(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")


def summarize_result(result: dict[str, Any]) -> dict[str, Any]:
    keys = ["path", "status", "category", "target", "reason", "classification_attempts"]
    return {key: result[key] for key in keys if key in result}


def run_command(args: list[str], timeout: int) -> subprocess.CompletedProcess[str] | None:
    command = [resolve_executable(args[0]), *args[1:]]
    env = {**os.environ, "PATH": TOOL_PATH}
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def resolve_executable(name: str) -> str:
    if "/" in name:
        return name
    found = shutil.which(name, path=TOOL_PATH)
    return found or name


def wait_until_stable(path: Path, stable_seconds: int, timeout: int) -> bool:
    last_size = -1
    stable_count = 0
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if not path.exists():
            return False

        try:
            size = path.stat().st_size
        except OSError:
            return False

        if size > 0 and size == last_size:
            stable_count += 1
            if stable_count >= stable_seconds:
                return True
        else:
            stable_count = 0
            last_size = size

        time.sleep(1)

    return False


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def mdls_value(path: Path, name: str) -> str:
    result = run_command(["mdls", "-name", name, str(path)], timeout=5)
    if result is None or result.returncode != 0:
        return ""
    return result.stdout


def where_froms(path: Path) -> list[str]:
    raw = mdls_value(path, "kMDItemWhereFroms")
    if not raw or "(null)" in raw:
        return []
    quoted = re.findall(r'"((?:\\"|[^"])*)"', raw)
    return [item.replace('\\"', '"') for item in quoted if item.strip()]


def parse_mdls_string(raw: str) -> str | None:
    if not raw or "(null)" in raw:
        return None
    _, _, value = raw.partition("=")
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1].replace('\\"', '"')
    return value or None


def pdfinfo(path: Path) -> dict[str, str]:
    result = run_command(["pdfinfo", str(path)], timeout=10)
    if result is None or result.returncode != 0:
        return {}

    info: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            info[key.strip()] = value.strip()
    return info


def pdf_text_head(path: Path, pages: int) -> str:
    result = run_command(
        ["pdftotext", "-layout", "-f", "1", "-l", str(pages), str(path), "-"],
        timeout=20,
    )
    if result is None or result.returncode != 0:
        return ""
    return result.stdout


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def clean_title(value: str) -> str:
    value = normalize_text(value)
    value = re.sub(r"\s*[✩*∗†‡]+$", "", value).strip()
    return value


def find_arxiv_id(haystack: str, filename: str) -> str | None:
    match = ARXIV_ID_RE.search(haystack)
    if match:
        return match.group(1).rstrip(".")

    if ARXIV_FILENAME_RE.match(filename):
        return filename[:-4]

    return None


def find_doi(haystack: str) -> str | None:
    match = DOI_RE.search(haystack)
    if not match:
        return None
    return match.group(0).rstrip(".,);]")


def classify(path: Path, where_values: list[str], info: dict[str, str], text: str) -> Classification:
    haystack = "\n".join(
        [
            path.name,
            "\n".join(where_values),
            "\n".join(f"{key}: {value}" for key, value in info.items()),
            text[:12000],
        ]
    )

    arxiv_id = find_arxiv_id(haystack, path.name)
    if arxiv_id:
        return Classification("arxiv", "matched arXiv identifier or URL", arxiv_id=arxiv_id)

    doi = find_doi(haystack)
    if doi:
        return Classification("journal", "matched DOI", doi=doi)

    if PUBLISHER_RE.search(haystack):
        return Classification("journal", "matched known publisher/source URL")

    return Classification(None, "no arXiv, DOI, or known journal publisher signal")


def clean_pdf_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\f", "\n").splitlines():
        line = normalize_text(raw)
        if not line:
            continue
        if len(line) < 3:
            continue
        if re.fullmatch(r"\d+", line):
            continue
        if line.lower().startswith("arxiv:"):
            continue
        lines.append(line)
    return lines


def valid_title_line(line: str) -> bool:
    if len(line) < 8 or len(line) > 240:
        return False
    if NOISE_TITLE_RE.search(line):
        return False
    if HEADER_LINE_RE.search(line):
        return False
    if "@" in line or "http://" in line or "https://" in line:
        return False
    if DOI_RE.search(line):
        return False
    if AFFILIATION_RE.search(line):
        return False
    return True


def valid_metadata_title(value: str) -> bool:
    if not value:
        return False
    lowered = value.lower()
    if lowered in {"untitled", "unknown"}:
        return False
    if lowered.startswith("doi:") or lowered.startswith("doi "):
        return False
    doi = find_doi(value)
    if doi and normalize_text(value.replace(doi, "")).strip(" :.-") == "":
        return False
    return valid_title_line(value)


def likely_author_line(line: str) -> bool:
    if len(line) < 5 or len(line) > 300:
        return False
    if "@" in line or "http://" in line or "https://" in line:
        return False
    if NOISE_TITLE_RE.search(line) or AFFILIATION_RE.search(line):
        return False
    if HEADER_LINE_RE.search(line) or AUTHOR_STOP_RE.search(line):
        return False
    if DOI_RE.search(line):
        return False
    alpha = sum(ch.isalpha() for ch in line)
    if alpha < 4:
        return False
    separators = [",", ";", " and ", "&"]
    has_separator = any(sep in line for sep in separators)
    words = [word for word in re.split(r"\s+", line) if word]
    has_name_shape = 2 <= len(words) <= 40 and any(word[:1].isupper() for word in words)
    return has_separator or has_name_shape


def split_authors(value: str | None) -> list[str]:
    if not value:
        return []
    value = normalize_text(value)
    value = value.replace(" ,", ",")
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"(?<=[A-Za-z])\s*,?\s*\d+(?:\s*,\s*(?:\d+|[+*∗†‡]))*(?=\s+(?:and\s+)?[A-Z]|$)", ", ", value)
    value = re.sub(r"(?<=[A-Za-z])\s*[+*∗†‡]+(?=\s+(?:and\s+)?[A-Z]|$)", ", ", value)
    value = re.sub(r"\s+and\s+", ", ", value)
    value = re.sub(r"\s*,\s*", ", ", value)
    value = re.sub(r"(?:,\s*)+", ", ", value).strip(" ,")
    parts = value.split(";") if ";" in value else value.split(",")
    authors = []
    for part in parts:
        author = normalize_text(part.strip(" ,;"))
        author = re.sub(r"[*∗†‡]+$", "", author).strip(" ,;")
        author = re.sub(r"(?<=[A-Za-z])\d+(?:\s*,\s*(?:\d+|[+*∗†‡]))*$", "", author).strip()
        author = re.sub(r"\s+(?:[a-z](?:\s*,\s*(?:[a-z]|\d+|[+*∗†‡]))*|\d+)$", "", author).strip()
        author = author.strip(" ,;*∗†‡")
        if len(author) >= 2 and any(ch.isalpha() for ch in author):
            authors.append(author)
    return authors


def extract_author_lines(lines: list[str], start: int) -> list[str]:
    authors: list[str] = []
    for line in lines[start : start + 14]:
        if AUTHOR_STOP_RE.search(line):
            break
        if AFFILIATION_RE.search(line):
            continue
        if likely_author_line(line):
            authors.extend(split_authors(line))
    return authors


def extract_title_authors(info: dict[str, str], text: str) -> Extraction:
    metadata_title = clean_title(info.get("Title", ""))
    metadata_author = normalize_text(info.get("Author", ""))

    if valid_metadata_title(metadata_title):
        authors = split_authors(metadata_author)
        confidence = 0.85 if authors else 0.72
        return Extraction(metadata_title, authors, "pdfinfo_metadata", confidence)

    lines = clean_pdf_lines(text)
    title: str | None = None
    title_index: int | None = None

    for index, line in enumerate(lines[:50]):
        if not valid_title_line(line):
            continue

        title_parts = [line]
        for next_line in lines[index + 1 : index + 3]:
            if not valid_title_line(next_line):
                break
            if likely_author_line(next_line):
                break
            if len(" ".join(title_parts + [next_line])) > 260:
                break
            title_parts.append(next_line)

        title = clean_title(" ".join(title_parts))
        title_index = index
        break

    authors: list[str] = []
    if title_index is not None:
        authors = extract_author_lines(lines, title_index + 1)

    if title:
        confidence = 0.68 if authors else 0.55
        return Extraction(title, authors, "first_pages_heuristic", confidence)

    return Extraction(None, split_authors(metadata_author), "not_found", 0.0)


def unique_target(dest: Path, filename: str) -> Path:
    target = dest / filename
    if not target.exists():
        return target

    stem = target.stem
    suffix = target.suffix
    for index in range(2, 1000):
        candidate = dest / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return dest / f"{stem}-{stamp}{suffix}"


def default_manifest(root: Path) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_by": SCRIPT_NAME,
        "root": str(root),
        "updated_at": None,
        "papers": [],
    }


def load_manifest(path: Path, root: Path) -> dict[str, Any]:
    if not path.exists():
        return default_manifest(root)

    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return default_manifest(root)

    data = yaml.safe_load(raw)

    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must contain a mapping at the top level")
    if "papers" not in data or not isinstance(data["papers"], list):
        data["papers"] = []
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("generated_by", SCRIPT_NAME)
    data.setdefault("root", str(root))
    data.setdefault("updated_at", None)
    return data


def write_manifest_atomic(path: Path, data: dict[str, Any]) -> None:
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


def upsert_paper(manifest: dict[str, Any], entry: dict[str, Any]) -> None:
    papers = manifest.setdefault("papers", [])
    entry_id = entry["id"]
    for index, existing in enumerate(papers):
        if isinstance(existing, dict) and existing.get("id") == entry_id:
            papers[index] = entry
            return
    papers.append(entry)


def build_entry(
    original_path: Path,
    target: Path,
    digest: str,
    size_bytes: int,
    where_values: list[str],
    info: dict[str, str],
    classification: Classification,
    extraction: Extraction,
) -> dict[str, Any]:
    return {
        "id": f"sha256:{digest}",
        "file": {
            "current_path": str(target),
            "original_path": str(original_path),
            "original_name": original_path.name,
            "size_bytes": size_bytes,
            "sha256": digest,
        },
        "source": {
            "where_froms": where_values,
            "detected_url": where_values[0] if where_values else None,
        },
        "identifiers": {
            "arxiv_id": classification.arxiv_id,
            "doi": classification.doi,
        },
        "bibliographic": {
            "title": extraction.title,
            "authors": extraction.authors,
        },
        "triage": {
            "category": classification.category,
            "reason": classification.reason,
            "moved_at": now_iso(),
        },
        "extraction": {
            "title_author_method": extraction.method,
            "confidence": extraction.confidence,
            "pdfinfo": {
                key: info[key]
                for key in ["Title", "Author", "Creator", "Producer", "Pages"]
                if key in info
            },
            "llm_model": None,
        },
    }


def process_pdf(path: Path, args: argparse.Namespace) -> dict[str, Any]:
    path = path.expanduser().resolve()
    if path.suffix.lower() != ".pdf":
        return {"path": str(path), "status": "skipped", "reason": "not a PDF"}

    if not path.exists():
        return {"path": str(path), "status": "error", "reason": "file does not exist"}

    if not args.no_wait:
        stable = wait_until_stable(path, args.stable_seconds, args.timeout)
        if not stable:
            return {"path": str(path), "status": "skipped", "reason": "file did not become stable"}

    where_values = where_froms(path)
    info = pdfinfo(path)
    text = pdf_text_head(path, args.text_pages)
    classification = classify(path, where_values, info, text)
    classification_attempts = 1

    if classification.category is None and not args.no_wait and args.unknown_retry_timeout > 0:
        deadline = time.monotonic() + args.unknown_retry_timeout
        while time.monotonic() < deadline:
            time.sleep(args.unknown_retry_interval)
            classification_attempts += 1
            where_values = where_froms(path)
            info = pdfinfo(path)
            text = pdf_text_head(path, args.text_pages)
            classification = classify(path, where_values, info, text)
            if classification.category is not None:
                break

    if classification.category is None:
        return {
            "path": str(path),
            "status": "skipped",
            "reason": classification.reason,
            "classification_attempts": classification_attempts,
        }

    extraction = extract_title_authors(info, text)
    digest = sha256_file(path)
    size_bytes = path.stat().st_size
    dest = args.papers_root / classification.category
    already_in_dest = path.parent.resolve() == dest.resolve()

    if args.dry_run:
        target = path if already_in_dest else unique_target(dest, path.name)
        entry = build_entry(
            original_path=path,
            target=target,
            digest=digest,
            size_bytes=size_bytes,
            where_values=where_values,
            info=info,
            classification=classification,
            extraction=extraction,
        )
        return {
            "path": str(path),
            "status": "would_move",
            "category": classification.category,
            "target": str(target),
            "classification_attempts": classification_attempts,
            "manifest_entry": entry,
        }

    dest.mkdir(parents=True, exist_ok=True)

    with manifest_lock(args.papers_root):
        target = path if already_in_dest else unique_target(dest, path.name)
        entry = build_entry(
            original_path=path,
            target=target,
            digest=digest,
            size_bytes=size_bytes,
            where_values=where_values,
            info=info,
            classification=classification,
            extraction=extraction,
        )
        if not already_in_dest:
            shutil.move(str(path), str(target))
        manifest = load_manifest(args.manifest, args.papers_root)
        manifest["root"] = str(args.papers_root)
        manifest["updated_at"] = now_iso()
        upsert_paper(manifest, entry)
        write_manifest_atomic(args.manifest, manifest)

    return {
        "path": str(path),
        "status": "updated" if already_in_dest else "moved",
        "category": classification.category,
        "target": str(target),
        "classification_attempts": classification_attempts,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Move recognized research PDFs to Documents/Papers/arxiv or "
            "Documents/Papers/journal and update manifest.yaml."
        )
    )
    parser.add_argument("pdfs", nargs="+", help="PDF file path(s) passed by Folder Actions")
    parser.add_argument(
        "--papers-root",
        type=Path,
        default=DEFAULT_PAPERS_ROOT,
        help=f"destination root directory (default: {DEFAULT_PAPERS_ROOT})",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="manifest path (default: PAPERS_ROOT/manifest.yaml)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print planned actions without moving files")
    parser.add_argument("--no-wait", action="store_true", help="skip download-stability waiting")
    parser.add_argument("--stable-seconds", type=int, default=2, help="consecutive stable-size checks required")
    parser.add_argument("--timeout", type=int, default=60, help="seconds to wait for a stable PDF")
    parser.add_argument("--text-pages", type=int, default=2, help="number of first pages to inspect")
    parser.add_argument(
        "--unknown-retry-timeout",
        type=int,
        default=30,
        help="seconds to retry extraction before leaving an initially unknown PDF untouched",
    )
    parser.add_argument(
        "--unknown-retry-interval",
        type=int,
        default=3,
        help="seconds between unknown-classification retries",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=DEFAULT_LOG_PATH,
        help=f"append one JSON record per run (default: {DEFAULT_LOG_PATH})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    started_at = now_iso()
    args.papers_root = args.papers_root.expanduser().resolve()
    args.manifest = (args.manifest.expanduser().resolve() if args.manifest else args.papers_root / MANIFEST_NAME)
    args.log = args.log.expanduser().resolve()

    results: list[dict[str, Any]] = []
    had_error = False
    for item in args.pdfs:
        try:
            result = process_pdf(Path(item), args)
        except Exception as exc:
            result = {"path": item, "status": "error", "reason": str(exc)}
        if result.get("status") == "error":
            had_error = True
        results.append(result)

    log_line(
        args.log,
        {
            "started_at": started_at,
            "finished_at": now_iso(),
            "pid": os.getpid(),
            "argv": sys.argv if argv is None else [SCRIPT_NAME, *argv],
            "cwd": os.getcwd(),
            "papers_root": str(args.papers_root),
            "dry_run": args.dry_run,
            "tools": {
                "pdfinfo": resolve_executable("pdfinfo"),
                "pdftotext": resolve_executable("pdftotext"),
            },
            "results": [summarize_result(result) for result in results],
        },
    )

    json.dump(results, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 1 if had_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
