// swift-tools-version: 6.0
// PhuThanhAI macOS launcher scaffold (S00).
//
// Scope at S00: a thin SwiftUI shell that displays the locally served React UI in
// a WKWebView after verifying the loopback health endpoint. It contains no AI,
// media, FFmpeg or database logic - those live in the Python backend.
//
// Not yet implemented (S05): .app bundling, .icns icon, Info.plist, code signing,
// Spotlight registration, and supervised backend process spawn/termination.
import PackageDescription

let package = Package(
    name: "PhuThanhAI",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "PhuThanhAI", targets: ["PhuThanhAILauncher"])
    ],
    targets: [
        .executableTarget(
            name: "PhuThanhAILauncher",
            path: "Sources/PhuThanhAILauncher"
        )
    ]
)
