"""
PhotoS - Image Deduplication

Perceptual hash (dhash) based duplicate detection. Pure PIL implementation,
no additional dependencies required.
"""

import os
import shutil
from pathlib import Path
from typing import List, Dict, Tuple, Optional
from collections import defaultdict


def _flattened(img):
    """Pixel data with Pillow version compat (get_flattened_data in Pillow 12+,
    getdata fallback for older Pillows / py3.9 → Pillow 11)."""
    gfd = getattr(img, "get_flattened_data", None)
    if gfd is not None:
        return gfd()
    return img.getdata()


def dhash(image, hash_size: int = 8) -> str:
    """Compute the difference hash (dhash) of an image.

    Algorithm:
      1. Convert to grayscale and resize to (hash_size+1) × hash_size
      2. Compute horizontal gradient: 1 if pixel[n] < pixel[n+1], else 0
      3. Return 64-bit hash as hex string

    Args:
        image: A PIL Image object.
        hash_size: Hash precision (default 8 → 64-bit hash).

    Returns:
        Hex string representation of the 64-bit hash.
    """
    img = image.convert("L").resize((hash_size + 1, hash_size))
    pixels = list(_flattened(img))

    hash_bits = []
    for row in range(hash_size):
        for col in range(hash_size):
            left = pixels[row * (hash_size + 1) + col]
            right = pixels[row * (hash_size + 1) + col + 1]
            hash_bits.append("1" if left < right else "0")

    # Convert bit string to hex
    bit_str = "".join(hash_bits)
    return f"{int(bit_str, 2):016x}"


def hamming_distance(hash1: str, hash2: str) -> int:
    """Compute Hamming distance between two hex hash strings."""
    if len(hash1) != len(hash2):
        return 999
    # Convert hex to integers
    n1 = int(hash1, 16)
    n2 = int(hash2, 16)
    # XOR and count bits (int.bit_count is Python 3.10+; fall back for 3.9)
    xor = n1 ^ n2
    bc = getattr(xor, "bit_count", None)
    if bc is not None:
        return bc()
    return bin(xor).count("1")


def _load_image_safe(path: str):
    """Try to load an image (RAW included), return None on failure.

    RAW files (prime dedup material — burst shooters) go through the
    engine's rawpy/sips decoder instead of being skipped as unopenable.
    """
    try:
        from .metrics import _open_decodable
        img, temps = _open_decodable(path)
        try:
            dhash(img)  # force decode while the temp file still exists
            img._photos_dedup_temps = temps
            return img
        except Exception:
            for t in temps:
                try:
                    os.unlink(t)
                except OSError:
                    pass
            raise
    except Exception:
        return None


class DuplicateGroups(dict):
    """find_duplicates() result — a plain ``hash → [paths]`` dict, plus a
    ``skipped`` attribute: how many input files could not be opened or
    hashed (RAW, corrupt). Keeps the dict contract for existing callers,
    but "no duplicates found" no longer implies "every file was checked".
    """

    def __init__(self, *args, skipped: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self.skipped = skipped


def find_duplicates(
    paths: List[str],
    threshold: int = 5,
    progress_callback=None,
) -> Dict[str, List[str]]:
    """Find duplicate/similar images using perceptual hashing.

    Images with Hamming distance ≤ threshold are considered duplicates.

    Args:
        paths: List of image file paths.
        threshold: Max Hamming distance to consider as duplicate (default 5).
        progress_callback: Optional callback(current, total).

    Returns:
        Dict mapping representative hash → list of duplicate paths.
        Each group has ≥ 2 images. The returned dict carries a ``skipped``
        attribute: count of files that could not be opened/hashed.
    """
    # Phase 1: compute hashes
    hashes: Dict[str, str] = {}
    skipped = 0
    total = len(paths)
    for i, path in enumerate(paths):
        if progress_callback:
            progress_callback(i + 1, total)
        img = _load_image_safe(path)
        if img is None:
            skipped += 1  # unopenable (corrupt) — don't skip silently
            continue
        try:
            hashes[path] = dhash(img)
        except Exception:
            skipped += 1
        finally:
            for t in getattr(img, "_photos_dedup_temps", ()) or ():
                try:
                    os.unlink(t)
                except OSError:
                    pass

    # Phase 2: group by hash (exact match first)
    hash_groups: Dict[str, List[str]] = defaultdict(list)
    for path, h in hashes.items():
        hash_groups[h].append(path)

    # Phase 3: merge groups within threshold distance. Popcount bucketing
    # first: hashes with |popcount(a)-popcount(b)| > threshold can NEVER be
    # within threshold Hamming distance, so only same-band hashes are
    # compared — the plain double loop was O(n²) over every unique hash.
    def _popcount(h: str) -> int:
        return bin(int(h, 16)).count("1")

    hash_list = list(hash_groups.keys())
    bands: Dict[int, List[str]] = defaultdict(list)
    for h in hash_list:
        bands[_popcount(h)].append(h)

    merged: Dict[str, List[str]] = {}
    assigned = set()

    for h1 in hash_list:
        if h1 in assigned:
            continue
        band = _popcount(h1)
        group = list(hash_groups[h1])
        assigned.add(h1)
        for delta in range(0, threshold + 1):
            for h2 in bands.get(band + delta, ()):
                if h2 in assigned:
                    continue
                if hamming_distance(h1, h2) <= threshold:
                    group.extend(hash_groups[h2])
                    assigned.add(h2)
            if delta:  # also the symmetric band below (delta=0 covered once)
                for h2 in bands.get(band - delta, ()):
                    if h2 in assigned:
                        continue
                    if hamming_distance(h1, h2) <= threshold:
                        group.extend(hash_groups[h2])
                        assigned.add(h2)
        if len(group) >= 2:
            merged[h1] = group

    return DuplicateGroups(merged, skipped=skipped)


class HandleResult(tuple):
    """handle_duplicates() result — a plain ``(kept, removed)`` 2-tuple
    (unchanged unpacking contract), plus a ``failed`` attribute: files
    whose move failed with OSError (cross-device, permission, in use) are
    counted there instead of crashing the batch halfway through.
    """

    def __new__(cls, kept: int, removed: int, failed: int = 0):
        self = super().__new__(cls, (kept, removed))
        self.failed = failed
        return self


def handle_duplicates(
    dup_groups: Dict[str, List[str]],
    action: str = "report",
    dry_run: bool = False,
) -> Tuple[int, int]:
    """Act on duplicate groups.

    Args:
        dup_groups: Result from find_duplicates().
        action: "report" | "move" | "delete" | "keep-sharpest".
            keep-sharpest keeps the sharpest image in each burst (blur-score)
            and moves/deletes the rest — pick-keepers for burst sequences.
        dry_run: If True, only print what would happen.

    Returns:
        (files_kept, files_removed) — the tuple also carries a ``failed``
        attribute with the count of moves that failed (see HandleResult).
    """
    kept, removed, failed = 0, 0, 0

    for group_id, paths in dup_groups.items():
        if action == "keep-sharpest":
            # blur-score is the variance-of-Laplacian focus measure; higher
            # = sharper. Best-effort: unreadable files score 0 (lose).
            from .metrics import compute_blur_score
            scores = {}
            for p in paths:
                try:
                    scores[p] = compute_blur_score(p)
                except Exception:
                    scores[p] = 0.0
            keeper = max(paths, key=lambda p: scores[p])
        else:
            keeper = paths[0]  # keep the first one
        dupes = [p for p in paths if p != keeper]

        kept += 1

        for dup in dupes:
            removed += 1
            if action == "move":
                dup_dir = Path(keeper).parent / "_duplicates"
                if not dry_run:
                    try:
                        dup_dir.mkdir(parents=True, exist_ok=True)
                        dest = dup_dir / Path(dup).name
                        counter = 1
                        while dest.exists():
                            dest = dup_dir / f"{Path(dup).stem}_{counter}{Path(dup).suffix}"
                            counter += 1
                        # shutil.move falls back to copy+unlink across
                        # devices; os.rename dies there with errno 18 EXDEV
                        shutil.move(dup, str(dest))
                    except OSError:
                        # permission / file-in-use / cross-device: count the
                        # failure and continue (mirrors gui
                        # ._dedup_move_to_trash) instead of crashing mid-move
                        removed -= 1
                        failed += 1
            elif action in ("delete", "keep-sharpest"):
                # keep-sharpest removes the non-keepers (the blurry rest of a
                # burst), keeping only the sharpest image per group
                if not dry_run:
                    try:
                        os.unlink(dup)
                    except OSError:
                        # a locked/permission-denied file must not abort the
                        # remaining deletions (mirrors the move branch)
                        removed -= 1
                        failed += 1

    return HandleResult(kept, removed, failed)
