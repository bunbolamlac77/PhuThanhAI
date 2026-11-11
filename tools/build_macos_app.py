#!/usr/bin/env python3
"""Build the PhuThanhAI .app bundle with swiftc (SwiftPM is broken here).

Produces dist/PhuThanhAI.app with:
* compiled SwiftUI + WKWebView launcher,
* generated .icns,
* Info.plist (bundle id com.phuthanh.PhuThanhAI),
* backend_root.txt telling the launcher which project venv to spawn,
* ad-hoc code signature and Spotlight/LaunchServices registration.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCES = REPO / "macos" / "PhuThanhAI" / "Sources" / "PhuThanhAILauncher"
DIST = REPO / "dist"
APP = DIST / "PhuThanhAI.app"
SDK = Path("/Library/Developer/CommandLineTools/SDKs/MacOSX26.sdk")

PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>PhuThanhAI</string>
  <key>CFBundleDisplayName</key><string>PhuThanhAI</string>
  <key>CFBundleIdentifier</key><string>com.phuthanh.PhuThanhAI</string>
  <key>CFBundleExecutable</key><string>PhuThanhAI</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>3.0.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSUIElement</key><false/>
  <key>NSPrincipalClass</key><string>NSApplication</string>
</dict></plist>
"""


def make_icon(resources: Path) -> Path:
    from PIL import Image, ImageDraw

    iconset = resources / "AppIcon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True)
    base = Image.new("RGB", (1024, 1024), (12, 16, 20))
    draw = ImageDraw.Draw(base)
    for y in range(1024):
        shade = int(18 + 26 * (y / 1024))
        draw.line([(0, y), (1024, y)], fill=(shade, shade + 8, shade + 16))
    draw.rounded_rectangle([150, 220, 874, 804], radius=96, outline=(80, 200, 190), width=22)
    draw.polygon([(330, 560), (470, 420), (610, 560), (750, 420), (750, 720), (330, 720)],
                 fill=(70, 180, 170))
    draw.text((330, 270), "PT", fill=(235, 245, 245))
    sizes = [(16, "icon_16x16.png"), (32, "icon_16x16@2x.png"), (32, "icon_32x32.png"),
             (64, "icon_32x32@2x.png"), (128, "icon_128x128.png"), (256, "icon_128x128@2x.png"),
             (256, "icon_256x256.png"), (512, "icon_256x256@2x.png"), (512, "icon_512x512.png"),
             (1024, "icon_512x512@2x.png")]
    for size, name in sizes:
        base.resize((size, size), Image.LANCZOS).save(iconset / name)
    icns = resources / "AppIcon.icns"
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], check=True)
    shutil.rmtree(iconset)
    return icns


def main() -> int:
    if not SDK.is_dir():
        print(f"SDK missing: {SDK}", file=sys.stderr)
        return 2
    if APP.exists():
        shutil.rmtree(APP)
    (APP / "Contents" / "MacOS").mkdir(parents=True)
    resources = APP / "Contents" / "Resources"
    resources.mkdir()
    binary = APP / "Contents" / "MacOS" / "PhuThanhAI"
    sources = sorted(str(path) for path in SOURCES.glob("*.swift"))
    command = [
        "swiftc", "-parse-as-library", "-O", "-sdk", str(SDK),
        "-target", "arm64-apple-macos14.0", *sources, "-o", str(binary),
        "-framework", "SwiftUI", "-framework", "WebKit", "-framework", "AppKit",
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        print(completed.stdout + completed.stderr, file=sys.stderr)
        return completed.returncode
    (APP / "Contents" / "Info.plist").write_text(PLIST, encoding="utf-8")
    (resources / "backend_root.txt").write_text(str(REPO) + "\n", encoding="utf-8")
    make_icon(resources)
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(APP)], check=True)
    lsregister = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
    if Path(lsregister).exists():
        subprocess.run([lsregister, "-f", str(APP)], check=False)
    subprocess.run(["mdimport", str(APP)], check=False)
    print(str(APP))
    return 0


if __name__ == "__main__":
    sys.exit(main())
