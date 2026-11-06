"""
PhotoS - Model Weight Store (download / verify / cache)

Official optional plugins ship code on PyPI but keep large model weights out
of the wheel. Weights are downloaded on first use from a canonical URL (e.g. a
GitHub Release asset) into a per-user cache directory, verified against a
sha256 digest, and cached thereafter.

Cache directory resolution:
    $PHOTOS_CACHE_DIR          → <dir>/models
    $XDG_CACHE_HOME/photo-s    → <xdg>/photo-s/models
    default                    → ~/.cache/photo-s/models

Stdlib only (urllib.request) — matches the project's no-heavy-deps stance.

Slow-network behavior (v2.1.1): downloads retry with backoff and resume
from partial files via HTTP ``Range`` — a first use that stalls no longer
starts over from byte 0, and orphans from a killed process are adopted
(and swept once the final file verifies) instead of accumulating forever.
"""

import glob as _glob
import hashlib
import http.client
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import __version__

_CHUNK = 65536

# Hard ceiling on any single weight download — a hostile/typo'd URL must not
# stream an unbounded file into the user's cache directory.
_MAX_DOWNLOAD_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB

# Slow-network hardening (v2.1.1): first-use downloads die on a stalled read
# and used to leave unrecoverable .part files behind. Retries resume via
# HTTP Range from the largest stale partial of a dead process; a partial
# untouched for this long is treated as orphaned and safe to adopt.
_RETRY_ATTEMPTS = 3
_RETRY_SLEEP = 1.5          # seconds; scales with attempt number
_STALE_PART_SECONDS = 120.0  # > 2x the read timeout a live writer can stall


def _validate_url(url: str) -> None:
    """Only https:// (and file:// for local tests) may serve weights.

    Plain http:// is refused except for loopback hosts — local test servers
    and self-hosted mirrors run on 127.0.0.1; remote plain-http downloads
    would be trivially interceptable.
    """
    if url.startswith("https://") or url.startswith("file://"):
        return
    if url.startswith("http://"):
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        if host in ("127.0.0.1", "localhost", "::1", "[::1]"):
            return
    raise RuntimeError(
        "refusing to download model weights over insecure URL: {} "
        "(only https:// is allowed)".format(url))


@dataclass(frozen=True)
class WeightSpec:
    """A single downloadable model weight file.

    Plugins expose their weights via ``PhotoSPlugin.weight_specs()``; the
    engine / ``plugin fetch`` command turns each spec into a cached file via
    :func:`ensure`. ``url`` may be ``http(s)://`` or ``file://`` (the latter
    is used by tests).
    """
    name: str        # basename in the cache, e.g. "scunet.onnx"
    url: str         # http(s):// or file://
    sha256: str      # lowercase hex digest of the file
    size: int = 0    # bytes, informational only


def cache_dir() -> str:
    """Resolve the model cache directory (created lazily on demand)."""
    env = os.environ.get("PHOTOS_CACHE_DIR")
    if env:
        return os.path.join(env, "models")
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return os.path.join(xdg, "photo-s", "models")
    return str(Path.home() / ".cache" / "photo-s" / "models")


def _path_for(spec: WeightSpec) -> str:
    return os.path.join(cache_dir(), spec.name)


def verify(path: str, sha256: str) -> bool:
    """True iff the file at ``path`` hashes to ``sha256``."""
    if not os.path.isfile(path):
        return False
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return False
    return digest.hexdigest().lower() == sha256.lower()


def cached_path(spec: WeightSpec) -> Optional[str]:
    """Absolute cached path if present AND verified, else None."""
    path = _path_for(spec)
    if verify(path, spec.sha256):
        return path
    return None


def status(spec: WeightSpec) -> dict:
    """Machine-readable weight state for ``plugin info`` / ``plugin fetch``."""
    path = cached_path(spec)
    return {
        "name": spec.name,
        "cached": path is not None,
        "path": path,
        "size": spec.size,
    }


def ensure(spec: WeightSpec, timeout: int = 60, attempts: int = _RETRY_ATTEMPTS) -> str:
    """Return the absolute cached path for ``spec``, downloading if needed.

    - Cache hit (present + verified) → return immediately.
    - Otherwise stream-download to ``.{name}.{pid}.{tid}.part`` while hashing.
    - On digest match → atomically ``os.replace`` to the final path, then
      sweep every other ``.{name}.*.part`` leftover (verified final file
      makes them garbage).
    - Slow-network hardening: network failures retry up to ``attempts``
      times, resuming from our own partial via HTTP Range; a stale partial
      left by a dead process (untouched ≥ ``_STALE_PART_SECONDS``) is
      adopted the same way, and one that already holds the complete file
      is accepted without any network round-trip.
    - On final failure → ``RuntimeError``; own ``.part`` removed.

    Safe for concurrent processes: unique ``.part`` per pid+thread + atomic
    rename + verify-on-every-hit means the last successful verifier wins.
    """
    path = _path_for(spec)
    cached = cached_path(spec)
    if cached is not None:
        return cached

    os.makedirs(os.path.dirname(path), exist_ok=True)
    # pid alone collides when two threads (parallel batch workers) download the
    # same weight in one process — add the thread id so they write to distinct
    # .part files instead of truncating each other's stream.
    part = "{}.{}.{}.part".format(path, os.getpid(), threading.get_ident())
    try:
        for attempt in range(1, attempts + 1):
            offset, digest = _resume_state(part)
            if offset and digest.hexdigest().lower() == spec.sha256.lower():
                # the partial already holds the complete verified file — a
                # previous download finished but died before the rename
                os.replace(part, path)
                _sweep_parts(path)
                return path
            try:
                got = _download(spec, part, timeout, offset=offset, digest=digest)
            except RuntimeError:
                if attempt >= attempts:
                    raise
                time.sleep(_RETRY_SLEEP * attempt)
                continue
            if got == spec.sha256.lower():
                os.replace(part, path)
                _sweep_parts(path)
                return path
            # sha mismatch is deterministic on a clean download; a corrupt
            # adopted partial gets exactly one clean retry
            if offset == 0 or attempt >= attempts:
                raise RuntimeError(
                    "sha256 mismatch for {}: expected {} got {}"
                    .format(spec.name, spec.sha256, got))
            _unlink_quiet(part)
    finally:
        if os.path.exists(part):
            try:
                os.unlink(part)
            except OSError:
                pass
    raise RuntimeError("download failed for {}: {} attempts exhausted"
                       .format(spec.name, attempts))


def _resume_state(part: str):
    """(offset, running sha256) to resume ``part`` from.

    Our own partial from a failed attempt in this same call resumes first
    (the name is ours alone). Otherwise the largest orphaned partial of the
    same weight is adopted via atomic rename — only if untouched for
    ``_STALE_PART_SECONDS`` (a live writer's partial keeps its name and fd).
    Returns (0, empty digest) for a fresh start.
    """
    def _hash_file(p):
        d = hashlib.sha256()
        with open(p, "rb") as f:
            while True:
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                d.update(chunk)
        return d, os.path.getsize(p)

    if os.path.exists(part) and os.path.getsize(part) > 0:
        digest, size = _hash_file(part)
        return size, digest

    path = part.rsplit(".", 3)[0]  # strip .{pid}.{tid}.part
    best = None
    now = time.time()
    try:
        candidates = _glob.glob(_glob.escape(path) + ".*.part")
    except OSError:
        return 0, hashlib.sha256()
    for cand in candidates:
        if cand == part:
            continue
        try:
            st = os.stat(cand)
        except OSError:
            continue
        if st.st_size == 0 or now - st.st_mtime < _STALE_PART_SECONDS:
            continue
        if best is None or st.st_size > best[1]:
            best = (cand, st.st_size)
    if best is None:
        return 0, hashlib.sha256()
    try:
        os.replace(best[0], part)
    except OSError:
        return 0, hashlib.sha256()
    digest, size = _hash_file(part)
    return size, digest


def _sweep_parts(path: str) -> None:
    """Delete every ``.{name}.*.part`` leftover — the final file is verified."""
    try:
        leftovers = _glob.glob(_glob.escape(path) + ".*.part")
    except OSError:
        return
    for p in leftovers:
        _unlink_quiet(p)


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _download(spec: WeightSpec, dest: str, timeout: int,
              offset: int = 0, digest=None) -> str:
    """Stream ``spec.url`` to ``dest`` while hashing; returns the sha256 hex.

    ``offset``/``digest`` resume a previous partial (HTTP ``Range``): the
    response must be 206 with our offset, otherwise the transfer restarts
    from byte 0. A 416 (range unsatisfiable — the partial already holds the
    whole body) also falls back to a clean restart. Network errors raise
    ``RuntimeError`` ("download failed …"); digest checking stays in
    :func:`ensure` so retries never re-download on a deterministic mismatch.
    """
    _validate_url(spec.url)
    limit = spec.size if spec.size and spec.size > 0 else _MAX_DOWNLOAD_BYTES
    limit = min(limit, _MAX_DOWNLOAD_BYTES)
    if offset > limit:
        offset = 0  # partial larger than the allowed size — corrupt, restart
    if offset == 0:
        digest = hashlib.sha256()
    headers = {"User-Agent": "photo-s/{}".format(__version__)}
    if offset:
        headers["Range"] = "bytes={}-".format(offset)
    req = urllib.request.Request(spec.url, headers=headers)
    received = offset
    try:
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code == 416 and offset:
                # server can't honour the range — the partial is not a
                # prefix of the current file; restart clean
                offset = 0
                digest = hashlib.sha256()
                received = 0
                _unlink_quiet(dest)
                req = urllib.request.Request(
                    spec.url,
                    headers={"User-Agent": "photo-s/{}".format(__version__)})
                resp = urllib.request.urlopen(req, timeout=timeout)
            else:
                raise
        try:
            if offset and resp.getcode() != 206:
                # server ignored the Range header (file:// handlers and some
                # mirrors) — the body starts at byte 0, so must we
                offset = 0
                digest = hashlib.sha256()
                received = 0
            with open(dest, "wb" if offset == 0 else "ab") as f:
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > limit:
                        raise RuntimeError(
                            "download for {} exceeded size limit {} bytes "
                            "(expected {})".format(spec.name, limit, spec.size))
                    digest.update(chunk)
                    f.write(chunk)
        finally:
            resp.close()
    except (urllib.error.URLError, urllib.error.HTTPError,
            http.client.HTTPException, OSError) as e:
        raise RuntimeError(
            "download failed for {}: {} (manual download: {})"
            .format(spec.name, e, spec.url))
    return digest.hexdigest().lower()
