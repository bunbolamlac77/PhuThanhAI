import SwiftUI

@main
struct PhuThanhAIApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        WindowGroup("PhuThanhAI") {
            LauncherView()
                .frame(minWidth: 1100, minHeight: 720)
        }
        .windowResizability(.contentMinSize)
        .commands {
            CommandGroup(replacing: .newItem) {}
        }
    }
}
