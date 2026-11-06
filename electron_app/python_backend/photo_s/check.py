"""
PhotoS - Image Integrity Check

Scans image files with PIL's verify() to detect corrupt or unreadable files,
plus SHA-256 checksum manifests for long-term archive integrity.
"""

import csv
import glob
import hashlib
import os
from pathlib import Path
from typing import List

from PIL import Image

from .engine import RAW_EXTENSIONS


class VerifyResults(list):
    """verify_images() result — a plain list of per-file dicts, plus a
    ``skipped`` attribute: how many files were skipped unverified (camera
    RAW, which PIL cannot decode). Keeps the list contract for existing
    callers.
    """

    def __init__(self, *args):
        super().__init__(*args)
        self.skipped = 0


def collect_files(paths: List[str], recursive: bool = False) -> List[str]:
    """Collect ANY files (not just images) from paths/globs/dirs.

    Directories: recursive → rglob, else iterdir (files only). Globs are
    expanded. Result is sorted and deduped — for archive manifests.
    """
    files = []
    for pat in paths:
        p = Path(pat)
        if p.is_dir():
            if recursive:
                files.extend(str(x) for x in p.rglob("*") if x.is_file())
            else:
                files.extend(str(x) for x in p.iterdir() if x.is_file())
        elif p.is_file():
            files.append(str(p.absolute()))
        else:
            files.extend(m for m in glob.glob(pat) if os.path.isfile(m))
    return sorted(set(files))


def verify_images(files: List[str]) -> List[dict]:
    """Verify each image file.

    Returns a list of dicts: {"path", "ok", "error", "skipped"} — error is
    "" when ok. Missing files, unreadable files, and truncated data all
    count as broken. Camera RAW files (engine.RAW_EXTENSIONS) are the one
    exception: PIL has no CR2/NEF/ARW/... decoder, so verify() would
    condemn every RAW as corrupt. They are reported ok with skipped=True
    instead (RAW archive integrity is covered by checksum manifests). The
    returned list carries a ``skipped`` attribute with their count.
    """
    results = VerifyResults()
    for path in files:
        if Path(path).suffix.lower() in RAW_EXTENSIONS:
            results.append({"path": path, "ok": True, "error": "",
                            "skipped": True})
            results.skipped += 1
            continue
        try:
            with Image.open(path) as img:
                img.verify()
            results.append({"path": path, "ok": True, "error": "",
                            "skipped": False})
        except Exception as e:
            results.append({"path": path, "ok": False, "error": str(e),
                            "skipped": False})
    return results


def compute_checksums(paths: List[str], algorithm: str = "sha256",
                      progress_callback=None) -> List[dict]:
    """Compute a checksum per file (streaming, 1 MiB chunks).

    Returns list of {"path", "size", <algorithm>}. Unreadable/missing files
    get an empty checksum (verify_manifest treats those as missing).
    """
    # No silent fallback: an unknown algorithm name would otherwise compute
    # sha256 but label it with the bogus name — an invisible wrong result.
    # algorithms_available is a name SET, so the getattr(hashlib, ...) "new"
    # constructor bypass is closed as well.
    if algorithm not in hashlib.algorithms_available:
        raise ValueError(
            f"unsupported checksum algorithm {algorithm!r}; "
            f"supported: {sorted(h for h in hashlib.algorithms_available)}")
    algo = getattr(hashlib, algorithm)
    results = []
    for i, path in enumerate(paths):
        if progress_callback:
            progress_callback(i + 1, len(paths))
        abs_path = os.path.abspath(path)
        try:
            h = algo()
            size = 0
            with open(path, "rb") as f:
                while chunk := f.read(1 << 20):
                    h.update(chunk)
                    size += len(chunk)
            results.append({"path": abs_path, "size": size,
                            algorithm: h.hexdigest()})
        except OSError:
            results.append({"path": abs_path, "size": 0, algorithm: ""})
    return results


def write_manifest(path: str, entries: List[dict], algorithm: str = "sha256") -> str:
    """Write a checksum manifest CSV (header: path,size,<algorithm>)."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["path", "size", algorithm])
        for e in entries:
            writer.writerow([e["path"], e["size"], e[algorithm]])
    return path


def verify_manifest(path: str) -> dict:
    """Re-hash every file listed in a checksum manifest; report problems.

    Returns {"algorithm", "total", "ok", "missing": [paths], "mismatched":
    [{"path", "expected", "actual"}]}. Missing files (unreadable now) are
    reported separately from content mismatches.
    """
    entries = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return {"algorithm": "sha256", "total": 0, "ok": 0,
                    "missing": [], "mismatched": []}
        algorithm = header[2] if len(header) > 2 and header[2] else "sha256"
        for row in reader:
            if len(row) >= 3:
                entries.append({"path": row[0], "expected": row[2]})

    fresh = {e["path"]: e for e in compute_checksums(
        [e["path"] for e in entries], algorithm=algorithm)}

    missing, mismatched = [], []
    ok = 0
    for e in entries:
        f = fresh.get(e["path"])
        if f is None or not f.get(algorithm):
            missing.append(e["path"])
        elif f[algorithm] != e["expected"]:
            mismatched.append({"path": e["path"], "expected": e["expected"],
                               "actual": f[algorithm]})
        else:
            ok += 1
    return {"algorithm": algorithm, "total": len(entries), "ok": ok,
            "missing": missing, "mismatched": mismatched}
