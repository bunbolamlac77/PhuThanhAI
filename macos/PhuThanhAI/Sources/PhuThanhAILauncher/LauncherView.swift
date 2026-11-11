import SwiftUI
import WebKit

/// Thin WKWebView wrapper. It renders the locally served React bundle and never
/// injects a capability token into a URL.
struct WebView: NSViewRepresentable {
    let url: URL

    func makeNSView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.allowsBackForwardNavigationGestures = false
        webView.load(URLRequest(url: url))
        return webView
    }

    func updateNSView(_ webView: WKWebView, context: Context) {
        if webView.url != url {
            webView.load(URLRequest(url: url))
        }
    }
}

struct LauncherView: View {
    @EnvironmentObject private var backend: BackendController

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            content
        }
        .task {
            await backend.bootstrap()
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
            Button("Kiểm tra lại") {
                Task { await backend.bootstrap() }
            }
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
        case .checking(let target):
            Text("Đang kiểm tra \(target)").font(.system(size: 11)).foregroundStyle(.secondary)
        case .ready:
            Text("Local · loopback")
                .font(.system(size: 11))
                .padding(.horizontal, 8)
                .padding(.vertical, 2)
                .background(Color.green.opacity(0.18), in: Capsule())
        case .failed(let message):
            Text(message)
                .font(.system(size: 11))
                .foregroundStyle(.red)
                .lineLimit(2)
        }
    }

    @ViewBuilder
    private var content: some View {
        switch backend.status {
        case .ready(let url):
            WebView(url: url)
        case .checking:
            ProgressView("Đang chờ backend…").frame(maxWidth: .infinity, maxHeight: .infinity)
        case .idle, .failed:
            VStack(spacing: 10) {
                Text("Backend chưa sẵn sàng")
                    .font(.system(size: 14, weight: .medium))
                Text("Chạy backend rồi mở lại, hoặc đặt biến môi trường PHUTHANHAI_URL.")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                Text("uv run phuthanhai serve --host 127.0.0.1")
                    .font(.system(size: 12, design: .monospaced))
                    .textSelection(.enabled)
                Text("S00: launcher là scaffold. Đóng gói .app, icon và Spotlight thuộc S05.")
                    .font(.system(size: 11))
                    .foregroundStyle(.tertiary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .padding()
        }
    }
}
