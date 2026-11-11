import SwiftUI

@main
struct PhuThanhAIApp: App {
    @StateObject private var backend = BackendController()

    var body: some Scene {
        WindowGroup("PhuThanhAI") {
            LauncherView()
                .environmentObject(backend)
                .frame(minWidth: 1100, minHeight: 720)
        }
        .windowResizability(.contentMinSize)
        .commands {
            CommandGroup(replacing: .newItem) {}
        }
    }
}
