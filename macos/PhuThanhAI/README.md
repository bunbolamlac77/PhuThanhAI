# macOS launcher (S00 scaffold)

Thin SwiftUI + WKWebView shell. It contains no AI, media, FFmpeg or database logic.

## What it does at S00

1. Reads the backend URL from `PHUTHANHAI_URL` or the first command-line argument.
2. Calls `GET /api/v1/health` and refuses to open the UI unless `loopback_only` is true.
3. Renders the locally served React bundle in a `WKWebView`.

## What it does **not** do yet (S05)

- `.app` bundle assembly, `.icns` icon, code signing, Spotlight registration.
- Supervised backend spawn/termination and single-instance handoff.
- Reaping the Python child on quit.

`Info.plist` is the S05 template (`NSAllowsLocalNetworking` is required for
WKWebView to load `http://127.0.0.1`). It is not consumed by the S00 build.

## Build on this machine (verified command)

`swift build` currently fails on this Mac for a toolchain reason unrelated to this
package: the Command Line Tools `swift-package` binary aborts with a `dyld` symbol
error, and the default SDK (`MacOSX27.0.sdk`) was built with Swift 6.4 while the
installed compiler is 6.3.3. The scaffold therefore builds against `MacOSX26.5.sdk`:

```bash
cd macos/PhuThanhAI
SDK=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk
swiftc -typecheck -parse-as-library -target arm64-apple-macosx14.0 -sdk "$SDK" \
  Sources/PhuThanhAILauncher/*.swift
swiftc -O -parse-as-library -target arm64-apple-macosx14.0 -sdk "$SDK" \
  -o .build-direct/PhuThanhAI Sources/PhuThanhAILauncher/*.swift
```

Result on the target machine: `Mach-O 64-bit executable arm64`.

## Run

```bash
# terminal 1
uv run phuthanhai serve --host 127.0.0.1

# terminal 2 (port taken from the serve output)
PHUTHANHAI_URL=http://127.0.0.1:<PORT> .build-direct/PhuThanhAI
```

Browser fallback (the documented S00 path when WKWebView or packaging is not
verified): open the same URL in the default browser.
