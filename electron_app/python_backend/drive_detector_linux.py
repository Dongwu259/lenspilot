"""
drive_detector_linux.py — Linux drive detection via /proc/mounts + /sys/block
"""

import os
import time
import re


class DriveDetector:
    """Linux removable drive detector."""

    @staticmethod
    def _is_removable(mount_point):
        """Check if a mount point is on a removable device."""
        # Read /proc/mounts to find the device
        try:
            with open("/proc/mounts", "r") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] == mount_point:
                        dev = parts[0]
                        # Check /sys/block/<dev>/removable
                        dev_name = dev.replace("/dev/", "")
                        # Handle partitions: sdb1 → sdb
                        base_name = re.sub(r'\d+$', '', dev_name)
                        removable_path = f"/sys/block/{base_name}/removable"
                        if os.path.exists(removable_path):
                            with open(removable_path, "r") as rf:
                                return rf.read().strip() == "1"
                        # Only sd[b-z] USB devices without explicit /sys/block/*/removable
                        # are assumed removable. NVMe (nvme*), MMC (mmcblk*), loop devices,
                        # and other long-named devices default to NOT removable.
                        if re.match(r'sd[b-z]', base_name):
                            return True
                        break
        except Exception:
            pass
        return False

    @staticmethod
    def get_all_drives():
        """Returns dict of {mount_point: info} for removable media."""
        drives = {}
        try:
            with open("/proc/mounts", "r") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    mp = parts[1]
                    # Skip system mounts (but allow /run/media/ — udisks2 mount point)
                    if mp.startswith("/sys") or mp.startswith("/proc") or \
                       mp.startswith("/dev") or mp == "/run" or \
                       mp == "/" or mp.startswith("/boot") or mp.startswith("/snap"):
                        continue
                    # Typical removable-media mount points:
                    #   /media/<label>        — traditional (Ubuntu < 18.04)
                    #   /media/<user>/<label> — newer Gnome/Ubuntu
                    #   /run/media/<user>/<label> — modern udisks2 (Fedora, Arch, Ubuntu ≥ 20.04)
                    #   /mnt/...              — manual/admin mounts
                    if (mp.startswith("/media/") or mp.startswith("/mnt/") or
                        mp.startswith("/run/media/")):
                        if DriveDetector._is_removable(mp):
                            # Get volume label
                            label = os.path.basename(mp)
                            drives[mp] = {
                                "mount_point": mp,
                                "label": label,
                                "removable": True,
                            }
        except Exception:
            pass
        return drives

    @staticmethod
    def get_removable_drives():
        drives = DriveDetector.get_all_drives()
        return [
            {
                "letter": mp,
                "label": info.get("label", ""),
                "type": 2,
                "type_name": "removable",
            }
            for mp, info in drives.items()
        ]

    @staticmethod
    def get_volume_label(mount_point):
        """Get volume label for a mount point."""
        drives = DriveDetector.get_all_drives()
        info = drives.get(mount_point, {})
        return info.get("label", "") if isinstance(info, dict) else ""

    @staticmethod
    def get_scan_targets(scan_path):
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
