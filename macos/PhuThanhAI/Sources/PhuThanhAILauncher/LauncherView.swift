import AppKit
import SwiftUI
import WebKit

struct WebView: NSViewRepresentable {
    let url: URL
    let onFailure: (String) -> Void

    func makeNSView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.userContentController.addUserScript(
            WKUserScript(
                source: "window.__PHUTHANHAI_EMBEDDED__ = true;",
                injectionTime: .atDocumentStart,
                forMainFrameOnly: true
            )
        )
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = false
        webView.load(URLRequest(url: url))
        return webView
    }

    func updateNSView(_ webView: WKWebView, context: Context) {
        if webView.url != url {
            webView.load(URLRequest(url: url))
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator(onFailure: onFailure) }

    final class Coordinator: NSObject, WKNavigationDelegate {
        let onFailure: (String) -> Void
        init(onFailure: @escaping (String) -> Void) { self.onFailure = onFailure }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            onFailure(error.localizedDescription)
        }
    }
}

struct LauncherView: View {
    @StateObject private var backend = BackendController()
    @State private var webFailure: String?

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            content
        }
        .task {
            guard !CommandLine.arguments.contains("--self-test") else { return }
            (NSApp.delegate as? AppDelegate)?.backend = backend
            await backend.start()
        }
    }

    private var header: some View {
        HStack(spacing: 12) {
            Text("PhuThanhAI")
                .font(.system(size: 15, weight: .semibold))
            Text(backend.version.map { "v\($0)" } ?? "v3.0.0")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
            Spacer()
            statusBadge
            if case .ready(let url) = backend.status {
                Button("Mở bằng trình duyệt") { NSWorkspace.shared.open(url) }
                    .controlSize(.small)
            }
            Button("Kiểm tra lại") { Task { await backend.start() } }
                .controlSize(.small)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
    }

    @ViewBuilder
    private var statusBadge: some View {
        switch backend.status {
        case .idle:
            Text("Chưa kết nối").font(.system(size: 11)).foregroundStyle(.secondary)
        case .starting(let message):
            Text(message).font(.system(size: 11)).foregroundStyle(.secondary)
        case .ready:
            Text("Local · loopback")
                .font(.system(size: 11))
                .padding(.horizontal, 8)
                .padding(.vertical, 2)
                .background(Color.green.opacity(0.18), in: Capsule())
        case .failed(let message):
            Text(message).font(.system(size: 11)).foregroundStyle(.red).lineLimit(3)
        }
    }

    @ViewBuilder
    private var content: some View {
        switch backend.status {
        case .ready(let url):
            WebView(url: url, onFailure: { message in
                webFailure = message
                if let target = URL(string: "http://127.0.0.1:\(url.port ?? 49321)/") {
                    NSWorkspace.shared.open(target)
                }
            })
            .overlay(alignment: .bottom) {
                if let webFailure {
                    Text("WKWebView lỗi: \(webFailure) — đã mở trình duyệt dự phòng.")
                        .font(.system(size: 11))
                        .padding(6)
                        .background(Color.orange.opacity(0.85))
                        .foregroundStyle(.black)
                }
            }
        case .starting:
            ProgressView("Đang khởi động backend…").frame(maxWidth: .infinity, maxHeight: .infinity)
        case .idle, .failed:
            VStack(spacing: 10) {
                Text("Backend chưa sẵn sàng")
                    .font(.system(size: 14, weight: .medium))
                Text("Kiểm tra log tại ~/Library/Application Support/PhuThanhAI/logs/backend.log")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                Button("Thử lại") { Task { await backend.start() } }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .padding()
        }
    }
}
