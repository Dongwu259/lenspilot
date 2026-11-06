"""editor_info.py — 色彩空间检测 + EXIF 摘要（P3-9 自 photo_editor.py 拆出）

只读信息侧：get_image_info 命令的两个底层 helper（ICC/模式推断色彩空间、
EXIF 拍摄参数人可读化）。命令本体在 photo_editor.py。
"""

import os
from PIL import Image


def _detect_color_space(img):
    """Detect color space from ICC profile or image mode."""
    icc = img.info.get('icc_profile')
    if icc:
        # Check for known profile descriptions in the raw bytes
        desc = icc.decode('latin-1', errors='ignore').lower()
        if 'prophoto' in desc or 'pro photo' in desc or 'koda' in desc and 'ro' in desc:
            return 'ProPhoto RGB'
        if 'adobe' in desc and 'rgb' in desc:
            return 'Adobe RGB'
        if 'adobe' in desc and '1998' in desc:
            return 'Adobe RGB'
        if 'srgb' in desc or 'srgb' in desc or 's rgb' in desc:
            return 'sRGB'
        if 'display' in desc and 'p3' in desc:
            return 'Display P3'
        # Try to extract description tag
        try:
            # ICC profile description is at offset ~84-90 in the desc tag
            idx = icc.find(b'desc')
            if idx >= 0 and idx + 20 < len(icc):
                tag = icc[idx:idx+50].decode('latin-1', errors='ignore').strip('\x00').strip()
                if tag:
                    return tag[:40]
        except Exception:
            pass
        return 'ICC Profile'
    # No ICC profile — infer from mode
    mode = img.mode
    if mode == 'RGB':
        return 'sRGB'
    if mode == 'CMYK':
        return 'CMYK'
    if mode == 'L':
        return 'Grayscale'
    if mode == 'RGBA':
        return 'sRGB (with alpha)'
    return mode

# ── EXIF extraction ──

def _extract_exif(img):
    """Extract key EXIF fields from a PIL Image."""
    exif_data = {}
    try:
        exif = img._getexif()
        if not exif:
            return exif_data
        # Map EXIF tag IDs to human-readable keys
        TAG_MAP = {
            271: 'camera',       # Make
            272: 'model',        # Model
            33432: 'copyright',
            34855: 'iso',
            33437: 'aperture',   # FNumber
            33434: 'shutter',    # ExposureTime
            37377: 'shutter',    # ShutterSpeedValue (fallback)
            37378: 'aperture',   # ApertureValue (fallback)
            37386: 'focalLength',
            36867: 'dateTaken',
            36868: 'dateTaken',
            37385: 'flash',
            41987: 'whiteBalance',
            41986: 'exposureMode',
            40962: 'width',
            40963: 'height',
            42036: 'lensModel',
        }
        for tag_id, key in TAG_MAP.items():
            val = exif.get(tag_id)
            if val is None:
                continue
            if key in ('aperture',):
                try:
                    val = f'f/{float(val):.1f}'
                except (ValueError, TypeError):
                    pass
            elif key in ('shutter',):
                try:
                    v = float(val)
                    if v >= 1:
                        val = f'{v:.0f}s'
                    else:
                        val = f'1/{1/v:.0f}s'
                except (ValueError, TypeError):
                    pass
            elif key in ('focalLength',):
                try:
                    val = f'{float(val):.0f}mm'
                except (ValueError, TypeError):
                    pass
            elif key in ('iso',):
                val = f'ISO {val}'
            elif key in ('dateTaken',):
                val = str(val)
            exif_data[key] = str(val)
        # Build camera string
        if exif_data.get('camera') and exif_data.get('model'):
            model = exif_data['model']
            if model.startswith(exif_data['camera']):
                exif_data['cameraModel'] = model
            else:
                exif_data['cameraModel'] = f"{exif_data['camera']} {model}"
    except Exception:
        pass
    return exif_data
