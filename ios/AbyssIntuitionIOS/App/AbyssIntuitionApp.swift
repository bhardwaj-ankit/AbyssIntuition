import SwiftUI

@main
struct AbyssIntuitionApp: App {
    @StateObject private var settings = AppSettings()
    @StateObject private var symbolStore = SymbolStore()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(settings)
                .environmentObject(symbolStore)
        }
    }
}
