"""
PhotoS - GPX Track Handling

Parses GPX track files (stdlib xml.etree) and interpolates a position for an
arbitrary timestamp, so photos can be geo-tagged from a recorded track.
"""

import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import List, Optional, Tuple

Point = Tuple[datetime, float, float]  # (timestamp[UTC-aware], lat, lon)

_TZ_OFFSET_RE = re.compile(r"\s*([+-])(\d{1,2}):(\d{2})\s*")


def parse_gpx(path: str) -> List[Point]:
    """Parse a GPX file into sorted (utc_time, lat, lon) points.

    Matches <trkpt lat=".." lon=".."><time>ISO8601</time></trkpt> anywhere in
    the document (namespace-agnostic). Points without a time are skipped.
    GPX timestamps are UTC per spec, so parsed times are normalized to
    timezone-aware UTC (a rare naive time is ASSUMED UTC) — stripping tzinfo
    and comparing against camera-local EXIF times used to skew matching by
    the whole timezone offset (8 hours in UTC+8). Pass the camera's EXIF
    ``OffsetTime`` to ``position_at`` to bridge the two frames correctly.
    Returns [] for unreadable or empty files.
    """
    points = []
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError):
        return []
    for elem in tree.iter():
        tag = elem.tag.rsplit("}", 1)[-1]  # strip namespace prefix
        if tag != "trkpt":
            continue
        lat = elem.attrib.get("lat")
        lon = elem.attrib.get("lon")
        ts = None
        for child in elem:
            ctag = child.tag.rsplit("}", 1)[-1]
            if ctag == "time":
                try:
                    # fromisoformat accepts the trailing 'Z' only on
                    # Python ≥3.11; normalize to '+00:00' so py3.9/3.10 work
                    raw = child.text.strip()
                    if raw.endswith("Z") or raw.endswith("z"):
                        raw = raw[:-1] + "+00:00"
                    ts = datetime.fromisoformat(raw)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    else:
                        ts = ts.astimezone(timezone.utc)
                except (ValueError, AttributeError):
                    ts = None
        if lat is None or lon is None or ts is None:
            continue
        try:
            flat, flon = float(lat), float(lon)
        except ValueError:
            continue
        if not (math.isfinite(flat) and math.isfinite(flon)):
            continue  # "nan"/"inf" parse as float but aren't coordinates
        points.append((ts, flat, flon))
    return sorted(points, key=lambda p: p[0])


@lru_cache(maxsize=4)
def _cached_points(path: str) -> List[Point]:
    """Thread-safe parse cache — batch workers share one parse per GPX file."""
    return parse_gpx(path)


def parse_tz_offset(spec) -> Optional[timedelta]:
    """Parse an EXIF OffsetTime string ("+08:00" / "-05:30") → timedelta.

    None for None/empty/unparseable input.
    """
    if spec is None or spec == "":
        return None
    if isinstance(spec, timedelta):
        return spec
    m = _TZ_OFFSET_RE.fullmatch(str(spec))
    if not m:
        return None
    sign = 1 if m.group(1) == "+" else -1
    hours, minutes = int(m.group(2)), int(m.group(3))
    if hours > 14 or minutes > 59:
        return None
    return sign * timedelta(hours=hours, minutes=minutes)


def position_at(path: str, ts: datetime,
                tz_offset=None) -> Optional[Tuple[float, float]]:
    """Interpolate (lat, lon) for a timestamp from a GPX file.

    ``ts`` may be timezone-aware (compared directly against the UTC track)
    or naive camera-local EXIF time; for the latter pass ``tz_offset`` —
    the EXIF ``OffsetTime`` string ("+08:00") or a timedelta — so the local
    wall time can be converted to UTC first. Without an offset a naive time
    is assumed to already be UTC (the camera-clock-set-to-UTC case).

    Linear interpolation between the bracketing track points; longitude
    interpolation takes the short way across the ±180° antimeridian. None
    when the timestamp is outside the recorded range or the file is empty.
    """
    points = _cached_points(path)
    if not points:
        return None
    if ts.tzinfo is None:
        offset = parse_tz_offset(tz_offset) or timedelta(0)
        # local = UTC + offset → UTC = local − offset
        ts = ts.replace(tzinfo=timezone.utc) - offset
    if ts < points[0][0] or ts > points[-1][0]:
        return None
    if len(points) == 1:
        return (points[0][1], points[0][2])

    for i in range(len(points) - 1):
        t0, lat0, lon0 = points[i]
        t1, lat1, lon1 = points[i + 1]
        if t0 <= ts <= t1:
            if t1 == t0:
                return (lat1, lon1)
            frac = (ts - t0).total_seconds() / (t1 - t0).total_seconds()
            lat = lat0 + (lat1 - lat0) * frac
            # |Δlon| > 180° means the segment crosses the antimeridian:
            # interpolate the short way (through ±180°), then normalize
            # back — plain linear interpolation would swing through 0°.
            if abs(lon1 - lon0) > 180.0:
                if lon1 > lon0:
                    lon0 += 360.0
                else:
                    lon1 += 360.0
            lon = lon0 + (lon1 - lon0) * frac
            if lon > 180.0:
                lon -= 360.0
            elif lon < -180.0:
                lon += 360.0
            return (lat, lon)
    return None


def to_dms_rational(deg: float) -> Tuple[Tuple[int, int], Tuple[int, int], Tuple[int, int]]:
    """Convert decimal degrees to piexif DMS rationals: ((d,1),(m,1),(s,100))."""
    # Work in integer centiseconds: a fractional second rounding up to 60.00
    # then carries into minutes (and minutes into degrees) automatically,
    # instead of emitting an invalid DMS like 12° 34' 60.00".
    total_cs = int(round(abs(deg) * 360000))  # 1° = 3600 s = 360000 cs
    d, rem = divmod(total_cs, 360000)
    m, s_cs = divmod(rem, 6000)
    return ((d, 1), (m, 1), (s_cs, 100))
