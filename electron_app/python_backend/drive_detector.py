"""
drive_detector.py — Cross-platform removable drive detection
Auto-selects the correct implementation based on sys.platform.
"""

import sys

if sys.platform == 'win32':
    from drive_detector_win import DriveDetector, DRIVE_REMOVABLE, DRIVE_FIXED
elif sys.platform == 'darwin':
    from drive_detector_mac import DriveDetector
    DRIVE_REMOVABLE = 2
    DRIVE_FIXED = 3
else:
    from drive_detector_linux import DriveDetector
    DRIVE_REMOVABLE = 2
    DRIVE_FIXED = 3
