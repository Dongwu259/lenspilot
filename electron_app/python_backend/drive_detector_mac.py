"""
drive_detector_mac.py — macOS drive detection via diskutil
"""

import os
import subprocess
import time
import plistlib
import re


class DriveDetector:
    """macOS removable drive detector using diskutil."""

    @staticmethod
    def get_all_drives():
        """Returns dict of {mount_point: info} for all mounted volumes."""
        drives = {}
        try:
            result = subprocess.run(
                ["diskutil", "list", "-plist", "external"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                return drives

            data = plistlib.loads(result.stdout.encode())
            for disk in data.get("AllDisksAndPartitions", []):
                for part in disk.get("Partitions", []):
                    mp = part.get("MountPoint")
                    if mp:
                        drives[mp] = {
                            "mount_point": mp,
                            "label": part.get("VolumeName", ""),
                            "removable": True,  # external disks are removable
                        }
        except Exception:
            pass

        return drives

    @staticmethod
    def get_removable_drives():
        """Returns list of dicts with mount info for removable drives."""
        drives = DriveDetector.get_all_drives()
        result = []
        for mp, info in drives.items():
            result.append({
                "letter": mp,  # macOS uses mount path instead of drive letter
                "label": info.get("label", ""),
                "type": 2,  # DRIVE_REMOVABLE equivalent
                "type_name": "removable",
            })
        return result

    @staticmethod
    def get_volume_label(mount_point):
        """Get volume label for a mount point."""
        drives = DriveDetector.get_all_drives()
        info = drives.get(mount_point, {})
        return info.get("label", "") if isinstance(info, dict) else ""

    @staticmethod
    def get_scan_targets(scan_path):
        """Find drives that contain the given scan_path directory."""
        drives = DriveDetector.get_all_drives()
        targets = []
        for mp, info in drives.items():
            target = os.path.join(mp, scan_path)
            if os.path.isdir(target):
                targets.append({
                    "letter": mp,
                    "label": info.get("label", ""),
                    "src_path": target,
                    "scan_path": scan_path,
                })
        return targets

    @staticmethod
    def monitor(scan_path, callback, stop_event):
        """Poll for new mounted volumes."""
        known = set()

        while not stop_event.is_set():
            try:
                drives = DriveDetector.get_all_drives()
                current = set(drives.keys())
                appeared = current - known

                for mp in appeared:
                    info = drives[mp]
                    target = os.path.join(mp, scan_path)
                    if os.path.isdir(target):
                        callback(mp, target, info.get("label", ""))

                known = current
                time.sleep(2)
            except Exception:
                time.sleep(5)
