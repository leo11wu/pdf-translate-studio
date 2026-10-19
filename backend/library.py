"""Local document records and faithful text-layer exports (no model calls)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

import fitz

PIPELINE_REVISION = "paperstudio-0.3.0-pdf2zh2.9-babeldoc0.6.2-v1"
JOB_ID = re.compile(r"[a-f0-9]{32}\Z")


def safe_filename(value: str) -> str:
    name = unicodedata.normalize("NFC", value.replace("\\", "/").split("/")[-1])
    name = re.sub(r"[\x00-\x1f\x7f/:]", "_", name).strip(" .")
    suffix = ".pdf" if name.lower().endswith(".pdf") else ""
    stem = name[:-4] if suffix else name
    stem = stem.encode("utf-8")[:170].decode("utf-8", errors="ignore").strip(" .")
    return (stem or "document") + suffix


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(path: Path, **settings: Any) -> str:
    payload = {"pipeline": PIPELINE_REVISION, "source_sha256": file_sha256(path), **settings}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def write_record(directory: Path, record: dict[str, Any]) -> None:
    """Atomically replace one private record. Callers use an explicit allowlist."""
    temporary = directory / "record.json.tmp"
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(record, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    temporary.replace(directory / "record.json")


def read_records(root: Path):
    for directory in root.iterdir():
        if directory.is_symlink() or not directory.is_dir() or not JOB_ID.fullmatch(directory.name):
            continue
        path = directory / "record.json"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("id") == directory.name and record.get("schema") == 1:
                yield directory, record
        except (OSError, ValueError, TypeError):
            continue


def ordered_blocks(page: fitz.Page) -> list[str]:
    """Read ordinary two-column pages column by column, between wide headings.

    Keep the PDF's line breaks, hyphens, numbers and marginal text. This is a
    text-layer extraction, not an OCR or an inferred reconstruction of prose.
    """
    midpoint = page.rect.width / 2
    blocks = []
    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue
        # MuPDF can combine text from two columns on the same baseline into
        # one block. Split those lines before deciding the page reading order.
        groups: dict[str, list] = {}
        for line in block["lines"]:
            rect = line["bbox"]
            side = "left" if rect[2] <= midpoint + 4 else "right" if rect[0] >= midpoint - 4 else "wide"
            groups.setdefault(side, []).append(line)
        for lines in groups.values():
            text = "\n".join("".join(span["text"] for span in line["spans"]) for line in lines)
            if text.strip():
                boxes = [line["bbox"] for line in lines]
                blocks.append((min(b[0] for b in boxes), min(b[1] for b in boxes),
                               max(b[2] for b in boxes), max(b[3] for b in boxes), text))
    left = [b for b in blocks if b[2] <= midpoint + 4]
    right = [b for b in blocks if b[0] >= midpoint - 4]
    # A single-column page should retain its vertical order.
    if len(left) < 2 or len(right) < 2:
        return [str(b[4]) for b in sorted(blocks, key=lambda b: (round(b[1], 1), b[0]))]
    wide = sorted([b for b in blocks if b[0] < midpoint - 4 and b[2] > midpoint + 4], key=lambda b: b[1])
    remaining = [b for b in blocks if b not in wide]
    ordered = []
    for anchor in [*wide, None]:
        boundary = anchor[1] if anchor is not None else float("inf")
        band = [b for b in remaining if b[1] < boundary]
        remaining = [b for b in remaining if b[1] >= boundary]
        ordered.extend(sorted(band, key=lambda b: (b[0] >= midpoint - 4, round(b[1], 1), b[0])))
        if anchor is not None:
            ordered.append(anchor)
    return [str(b[4]) for b in ordered]


def extract_text(source: Path, destination: Path) -> dict[str, Any]:
    counts, empty = [], []
    with fitz.open(source) as document, destination.open("w", encoding="utf-8", newline="\n") as stream:
        os.chmod(destination, 0o600)
        for number, page in enumerate(document, 1):
            blocks = []
            for block in ordered_blocks(page):
                # Some embedded PDF fonts expose spaces as U+0001 / U+0003.
                text = unicodedata.normalize("NFC", block.replace("\x01", " ").replace("\x03", " "))
                text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text).strip()
                if text:
                    blocks.append(text)
            content = "\n\n".join(blocks)
            counts.append(len(content))
            if not content:
                empty.append(number)
            stream.write(f"===== 第 {number} 页 / 共 {len(document)} 页 =====\n\n{content}\n\n")
    return {"page_characters": counts, "empty_pages": empty, "method": "pdf-text-layer; page markers; block order; original line breaks"}
