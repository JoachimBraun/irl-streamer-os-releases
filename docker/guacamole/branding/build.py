#!/usr/bin/env python3
"""Baut guacamole-irl-theme.jar (= Zip mit guac-manifest.json im Wurzelverzeichnis)."""
import os, zipfile
here = os.path.dirname(os.path.abspath(__file__))
out = os.path.join(here, "guacamole-irl-theme.jar")
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for root, _, files in os.walk(here):
        for f in sorted(files):
            if f in ("build.py", "guacamole-irl-theme.jar") or f.endswith(".md"):
                continue
            p = os.path.join(root, f)
            z.write(p, os.path.relpath(p, here))
print(out)
