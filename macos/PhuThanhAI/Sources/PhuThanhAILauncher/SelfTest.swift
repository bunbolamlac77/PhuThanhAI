import AppKit
import Foundation
import WebKit

/// Headless WKWebView smoke test used by tools/run_s05_gate.py.
///
/// It starts the real supervised backend, loads the real React bundle, checks the
/// three-region shell, and asks WKWebView to decode the first library videos
/// (canplay/error + dimensions). Results are written as JSON; no screen capture
/// permission is needed.
@MainActor
final class SelfTestRunner: NSObject, WKNavigationDelegate {
    private var webView: WKWebView?
    private var window: NSWindow?
    private var continuation: CheckedContinuation<Void, Never>?
    private var backend: BackendController?

    func run() async {
        let args = CommandLine.arguments
        func value(_ name: String) -> String? {
            guard let index = args.firstIndex(of: name), args.indices.contains(index + 1) else { return nil }
            return args[index + 1]
        }
        let output = URL(fileURLWithPath: value("--self-test-output") ?? "/tmp/phuthanhai-selftest.json")
        let dataDir = value("--data-dir").map { URL(fileURLWithPath: $0, isDirectory: true) }
        let controller = BackendController(dataDir: dataDir)
        backend = controller
        await controller.start()
        var result: [String: Any] = ["backend": String(describing: controller.status)]
        guard case .ready(let url) = controller.status else {
            result["ok"] = false
            write(result, to: output)
            return
        }
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        let web = WKWebView(frame: NSRect(x: 0, y: 0, width: 1200, height: 800), configuration: configuration)
        web.navigationDelegate = self
        webView = web
        // Media playback is suspended for a WKWebView that is not in a window.
        let testWindow = NSWindow(
            contentRect: NSRect(x: 40, y: 40, width: 1200, height: 800),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        testWindow.title = "PhuThanhAI self-test"
        testWindow.contentView = web
        testWindow.orderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        self.window = testWindow
        web.load(URLRequest(url: url))
        await waitForNavigation(timeout: 30)
        let script = """
          const out = {title: document.title, library: false, health: null, videos: []};
          for (let i = 0; i < 60; i++) {
            if (document.querySelector('.library-grid')) { out.library = true; break; }
            await new Promise(r => setTimeout(r, 250));
          }
          try { const h = await fetch('/api/v1/health'); out.health = h.status; } catch (e) { out.health = 'error'; }
          let assets = [];
          try {
            const projects = await (await fetch('/api/v1/projects')).json();
            for (const p of projects) {
              const r = await (await fetch('/api/v1/assets?project_id=' + p.project_id + '&limit=50')).json();
              assets.push(...r.items);
            }
          } catch (e) {}
          assets.sort((a, b) => (a.size_bytes || 0) - (b.size_bytes || 0));
          for (const a of assets.slice(0, 8)) {
            const v = document.createElement('video');
            v.muted = true; v.preload = 'auto'; v.src = '/api/v1/assets/' + a.asset_id + '/stream';
            document.body.appendChild(v);
            const res = await new Promise(resolve => {
              let done = false;
              const finish = x => { if (!done) { done = true; resolve(x); } };
              v.addEventListener('canplay', () => finish({ok: true, readyState: v.readyState, width: v.videoWidth, height: v.videoHeight}));
              v.addEventListener('error', () => finish({ok: false, code: v.error ? v.error.code : -1}));
              setTimeout(() => finish({ok: false, timeout: true}), 8000);
              v.load();
              v.play().catch(() => {});
            });
            out.videos.push(Object.assign({file: a.filename}, res));
            v.remove();
          }
          const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          const buttons = [...document.querySelectorAll('button')].filter(visible);
          const inputs = [...document.querySelectorAll('input, select, textarea')].filter(visible);
          out.a11y = {
            lang: document.documentElement.lang || null,
            buttons: buttons.length,
            unnamed_buttons: buttons.filter(b => !((b.innerText || b.textContent || '').trim() || b.getAttribute('aria-label') || b.title)).map(b => (b.outerHTML || '').slice(0, 240)).slice(0, 10),
            inputs: inputs.length,
            unlabeled_inputs: inputs.filter(i => !(i.getAttribute('aria-label') || i.getAttribute('aria-labelledby') || (i.id && document.querySelector('label[for="' + i.id + '"]')) || i.closest('label'))).map(i => i.id || i.type).slice(0, 10),
            images_without_alt: [...document.querySelectorAll('img')].filter(i => !i.hasAttribute('alt')).length,
            headings: document.querySelectorAll('h1,h2,h3').length,
            focusable: [...document.querySelectorAll('button,a[href],input,select,textarea,[tabindex]:not([tabindex="-1"])')].filter(visible).length,
            reduced_motion: window.matchMedia('(prefers-reduced-motion: reduce)').matches,
            base_font_px: getComputedStyle(document.body).fontSize,
          };
          return JSON.stringify(out);
        """
        do {
            let js = try await web.callAsyncJavaScript(script, arguments: [:], in: nil, contentWorld: .page)
            if let js = js as? String, let data = js.data(using: .utf8),
               let parsed = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                result.merge(parsed) { _, new in new }
                let videos = parsed["videos"] as? [[String: Any]] ?? []
                let anyPlayable = videos.contains { ($0["ok"] as? Bool) == true }
                result["ok"] = (parsed["library"] as? Bool == true) && (parsed["health"] as? Int == 200) && anyPlayable
            } else {
                result["ok"] = false
                result["error"] = "javascript returned no JSON: \(String(describing: js))"
            }
        } catch {
            result["ok"] = false
            result["error"] = "javascript: \(error.localizedDescription)"
        }
        self.window?.orderOut(nil)
        controller.terminate()
        write(result, to: output)
    }

    private func waitForNavigation(timeout: TimeInterval) async {
        await withCheckedContinuation { continuation in
            self.continuation = continuation
            DispatchQueue.main.asyncAfter(deadline: .now() + timeout) {
                self.finishNavigation()
            }
        }
    }

    private func finishNavigation() {
        continuation?.resume()
        continuation = nil
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { finishNavigation() }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { finishNavigation() }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { finishNavigation() }

    private func write(_ object: [String: Any], to url: URL) {
        if let data = try? JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted]) {
            try? data.write(to: url)
        }
    }
}
