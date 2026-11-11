import Foundation

/// Minimal health payload mirror of the backend `HealthResponse`.
/// Only the fields the launcher actually needs are decoded.
struct BackendHealth: Decodable {
    let status: String
    let productVersion: String
    let loopbackOnly: Bool

    enum CodingKeys: String, CodingKey {
        case status
        case productVersion = "product_version"
        case loopbackOnly = "loopback_only"
    }
}

/// Owns the launcher's view of the backend.
///
/// At S00 the backend is started externally (`uv run phuthanhai serve`) and its
/// URL is provided through `PHUTHANHAI_URL` or the first command-line argument.
/// Supervised spawn, single-instance detection and reaping on quit belong to S05;
/// they are deliberately not faked here.
@MainActor
final class BackendController: ObservableObject {
    enum Status: Equatable {
        case idle
        case checking(String)
        case ready(URL)
        case failed(String)
    }

    @Published private(set) var status: Status = .idle
    @Published private(set) var version: String?

    let baseURL: URL?

    init() {
        baseURL = Self.resolveBaseURL()
    }

    static func resolveBaseURL() -> URL? {
        if let raw = ProcessInfo.processInfo.environment["PHUTHANHAI_URL"], let url = URL(string: raw) {
            return url
        }
        if let raw = CommandLine.arguments.dropFirst().first, let url = URL(string: raw) {
            return url
        }
        return nil
    }

    func bootstrap() async {
        guard let baseURL else {
            status = .failed("Chưa có địa chỉ backend. Chạy: uv run phuthanhai serve --host 127.0.0.1")
            return
        }
        status = .checking(baseURL.absoluteString)
        var request = URLRequest(url: baseURL.appendingPathComponent("api/v1/health"))
        request.timeoutInterval = 3
        request.setValue("application/json", forHTTPHeaderField: "Accept")

        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                status = .failed("Phản hồi không phải HTTP")
                return
            }
            guard http.statusCode == 200 else {
                status = .failed("Health trả về HTTP \(http.statusCode)")
                return
            }
            let health = try JSONDecoder().decode(BackendHealth.self, from: data)
            guard health.loopbackOnly else {
                status = .failed("Backend không bind loopback — từ chối mở UI")
                return
            }
            version = health.productVersion
            status = .ready(baseURL)
        } catch {
            status = .failed("Không kết nối được backend: \(error.localizedDescription)")
        }
    }
}
