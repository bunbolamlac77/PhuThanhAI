import AppKit
import Foundation

final class AppDelegate: NSObject, NSApplicationDelegate {
    @MainActor var backend: BackendController?
    private var selfTest: SelfTestRunner?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let running = NSRunningApplication.runningApplications(
            withBundleIdentifier: Bundle.main.bundleIdentifier ?? "com.phuthanh.PhuThanhAI"
        ).filter { $0 != NSRunningApplication.current }
        if !running.isEmpty {
            running.first?.activate()
            NSApp.terminate(nil)
            return
        }
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)

        if CommandLine.arguments.contains("--self-test") {
            let runner = SelfTestRunner()
            selfTest = runner
            Task { @MainActor in
                await runner.run()
                NSApp.terminate(nil)
            }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        // Reap the backend synchronously before the app process disappears; relying
        // on deinit or applicationWillTerminate left an orphan on S05's first run.
        BackendController.shared?.terminate()
        backend?.terminate()
        return .terminateNow
    }

    func applicationWillTerminate(_ notification: Notification) {
        BackendController.shared?.terminate()
        backend?.terminate()
    }
}
