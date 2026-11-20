#!/usr/bin/env python3
"""Fill AppxManifest.template.xml with values from appx-identity.json.

Usage:  python fill-manifest.py <output-manifest-path> [version]
Version defaults to appx-identity.json's "version"; passing the app
version from package.json keeps a single source of truth.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(HERE, "appx-identity.json"), encoding="utf-8") as f:
    ident = json.load(f)
with open(os.path.join(HERE, "AppxManifest.template.xml"), encoding="utf-8") as f:
    tpl = f.read()

version = sys.argv[2] if len(sys.argv) > 2 else ident["version"]
out = (tpl
       .replace("{{IDENTITY_NAME}}", ident["identityName"])
       .replace("{{PUBLISHER}}", ident["publisher"])
       .replace("{{PUBLISHER_DISPLAY_NAME}}", ident["publisherDisplayName"])
       .replace("{{VERSION}}", version))

if "{{" in out:
    raise SystemExit("ERROR: unresolved placeholder(s) in manifest")

dest = sys.argv[1]
with open(dest, "w", encoding="utf-8") as f:
    f.write(out)
print("manifest -> %s  (identity=%s v%s)" % (dest, ident["identityName"], version))
