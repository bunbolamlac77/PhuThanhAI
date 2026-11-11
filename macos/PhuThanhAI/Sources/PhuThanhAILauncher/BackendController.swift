import Foundation

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

/// Owns the single backend process and the launcher's view of it.
///
/// The app never embeds AI logic: it spawns `python -m phuthanhai.cli serve` from
/// the configured project root, waits for loopback health, reuses an already
/// healthy backend recorded in the launcher lock file, and reaps the process on
/// quit. Exactly one backend and one app instance are allowed.
@MainActor
final class BackendController: ObservableObject {
    enum Status: Equatable {
        case idle
        case starting(String)
        case ready(URL)
        case failed(String)
    }

    @Published private(set) var status: Status = .idle
    @Published private(set) var version: String?

    @MainActor static var shared: BackendController?

    let root: URL?
    let dataDir: URL
    private var process: Process?
    private var ownsBackend = false
    private let lockName = "launcher.json"

    init(root: URL? = BackendController.resolveRoot(), dataDir: URL? = nil) {
        self.root = root
        if let dataDir {
            self.dataDir = dataDir
        } else if let raw = ProcessInfo.processInfo.environment["PHUTHANHAI_DATA_DIR"] {
            self.dataDir = URL(fileURLWithPath: raw, isDirectory: true)
        } else {
            let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
            self.dataDir = base.appendingPathComponent("PhuThanhAI", isDirectory: true)
        }
        BackendController.shared = self
    }

    nonisolated static func resolveRoot() -> URL? {
        if let raw = ProcessInfo.processInfo.environment["PHUTHANHAI_ROOT"] {
            return URL(fileURLWithPath: raw, isDirectory: true)
        }
        if let resource = Bundle.main.resourceURL?.appendingPathComponent("backend_root.txt"),
           let raw = try? String(contentsOf: resource, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines),
           !raw.isEmpty {
            return URL(fileURLWithPath: raw, isDirectory: true)
        }
        return nil
    }

    private var lockURL: URL { dataDir.appendingPathComponent(lockName) }

    func start() async {
        guard let root, FileManager.default.fileExists(atPath: root.appendingPathComponent(".venv/bin/python").path) else {
            status = .failed("Không tìm thấy project/venv. Đặt PHUTHANHAI_ROOT hoặc file backend_root.txt.")
            return
        }
        try? FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)
        status = .starting("Đang kiểm tra backend…")

        if let existing = readLock(), await probe(port: existing.port) {
            version = existing.version
            status = .ready(URL(string: "http://127.0.0.1:\(existing.port)/")!)
            return
        }
        removeLock()

        for port in 49321...49360 {
            if await probe(port: port) { continue }
            do {
                try spawn(root: root, port: port)
            } catch {
                status = .failed("Không khởi động được backend: \(error.localizedDescription)")
                return
            }
            status = .starting("Backend đang khởi động trên cổng \(port)…")
            if await waitForHealth(port: port, seconds: 45) {
                writeLock(port: port)
                status = .ready(URL(string: "http://127.0.0.1:\(port)/")!)
                return
            }
            terminate()
            status = .failed("Backend không phản hồi health sau 45 giây.")
            return
        }
        status = .failed("Không còn cổng loopback trống.")
    }

    private func spawn(root: URL, port: Int) throws {
        let python = root.appendingPathComponent(".venv/bin/python")
        let process = Process()
        process.executableURL = python
        process.arguments = ["-m", "phuthanhai.cli", "serve", "--host", "127.0.0.1", "--port", String(port)]
        process.currentDirectoryURL = root
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = root.appendingPathComponent("src").path + (env["PYTHONPATH"].map { ":\($0)" } ?? "")
        env["PHUTHANHAI_DATA_DIR"] = dataDir.path
        process.environment = env

        let logs = dataDir.appendingPathComponent("logs", isDirectory: true)
        try FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
        let logURL = logs.appendingPathComponent("backend.log")
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        let handle = try FileHandle(forWritingTo: logURL)
        handle.seekToEndOfFile()
        process.standardOutput = handle
        process.standardError = handle
        try process.run()
        self.process = process
        ownsBackend = true
    }

    private func probe(port: Int) async -> Bool {
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/v1/health") else { return false }
        var request = URLRequest(url: url)
        request.timeoutInterval = 1.5
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else { return false }
            let health = try JSONDecoder().decode(BackendHealth.self, from: data)
            guard health.loopbackOnly else { return false }
            version = health.productVersion
            return true
        } catch {
            return false
        }
    }

    private func waitForHealth(port: Int, seconds: Int) async -> Bool {
        for _ in 0..<(seconds * 2) {
            if await probe(port: port) { return true }
            try? await Task.sleep(nanoseconds: 500_000_000)
        }
        return false
    }

    private func readLock() -> (port: Int, version: String?, pid: Int32?)? {
        guard let data = try? Data(contentsOf: lockURL),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let port = object["port"] as? Int else { return nil }
        let pid = (object["pid"] as? Int).map { Int32($0) }
        return (port, object["version"] as? String, pid)
    }

    private func writeLock(port: Int) {
        let payload: [String: Any] = ["port": port, "pid": process?.processIdentifier ?? 0, "version": version ?? ""]
        if let data = try? JSONSerialization.data(withJSONObject: payload) {
            try? data.write(to: lockURL)
        }
    }

    private func removeLock() { try? FileManager.default.removeItem(at: lockURL) }

    func terminate() {
        var pid = process?.processIdentifier
        if pid == nil, let locked = readLock(), let rawPID = locked.pid, rawPID > 0 {
            pid = rawPID
        }
        if let process {
            process.terminate()
            let deadline = Date().addingTimeInterval(5)
            while process.isRunning && Date() < deadline { usleep(100_000) }
            if process.isRunning { kill(process.processIdentifier, SIGKILL) }
        } else if let pid {
            kill(pid, SIGTERM)
            let deadline = Date().addingTimeInterval(5)
            while kill(pid, 0) == 0 && Date() < deadline { usleep(100_000) }
            if kill(pid, 0) == 0 { kill(pid, SIGKILL) }
        }
        removeLock()
        process = nil
        ownsBackend = false
    }
}
