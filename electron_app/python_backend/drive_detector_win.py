"""
drive_detector_win.py — Windows drive detection via ctypes
Extracted from original photo_classification/main.py
"""

import os
import string
import ctypes
import time

DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6

SKIP_DRIVES = frozenset({"C"})


class DriveDetector:
    """Windows removable/fixed drive detector using Win32 API."""

    @staticmethod
    def get_drive_type(letter):
        return ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\")

    @staticmethod
    def get_volume_label(letter):
        drive_path = f"{letter}:\\"
        label = ctypes.create_unicode_buffer(256)
        ctypes.windll.kernel32.GetVolumeInformationW(
            drive_path, label, 256, None, None, None, None, 256
        )
        return label.value or ""

    @staticmethod
    def get_all_drives():
        """Returns dict of {letter: drive_type} for all available drives."""
        drives = {}
        for letter in string.ascii_uppercase:
            if letter in SKIP_DRIVES:
                continue
            if os.path.exists(f"{letter}:\\"):
                drives[letter] = DriveDetector.get_drive_type(letter)
        return drives

    @staticmethod
    def get_removable_drives():
        """Returns list of {letter, label, type} for removable/fixed drives."""
        drives = DriveDetector.get_all_drives()
        result = []
        for letter, dt in drives.items():
            if dt not in (DRIVE_REMOVABLE, DRIVE_FIXED):
                continue
            result.append({
                "letter": letter,
                "label": DriveDetector.get_volume_label(letter),
                "type": dt,
                "type_name": "removable" if dt == DRIVE_REMOVABLE else "fixed",
            })
        return result

    @staticmethod
    def get_scan_targets(scan_path):
        """Find drives that contain the given scan_path directory."""
        drives = DriveDetector.get_all_drives()
        targets = []
        for letter, dt in drives.items():
            if dt not in (DRIVE_REMOVABLE, DRIVE_FIXED):
                continue
            target = os.path.join(f"{letter}:\\", scan_path)
            if os.path.isdir(target):
                targets.append({
                    "letter": letter,
                    "label": DriveDetector.get_volume_label(letter),
                    "src_path": target,
                    "scan_path": scan_path,
                })
        return targets

    @staticmethod
    def monitor(scan_path, callback, stop_event):
        """
        Poll for new drives. Calls callback(letter, src_path, label) when a new
        drive with matching scan_path is detected.
        """
        known = DriveDetector.get_all_drives()

        # Check existing drives first
        for letter, dt in known.items():
            if dt not in (DRIVE_REMOVABLE, DRIVE_FIXED):
                continue
            target = os.path.join(f"{letter}:\\", scan_path)
            if os.path.isdir(target):
                callback(letter, target, DriveDetector.get_volume_label(letter))

        while not stop_event.is_set():
            try:
                current = DriveDetector.get_all_drives()
                appeared = set(current) - set(known)

                for letter in appeared:
                    if current[letter] not in (DRIVE_REMOVABLE, DRIVE_FIXED):
                        continue
                    target = os.path.join(f"{letter}:\\", scan_path)
                    if os.path.isdir(target):
                        callback(letter, target, DriveDetector.get_volume_label(letter))

                known = current
                time.sleep(1)
            except Exception:
                time.sleep(5)
