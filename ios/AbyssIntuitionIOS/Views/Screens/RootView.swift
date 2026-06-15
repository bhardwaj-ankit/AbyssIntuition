import SwiftUI

struct RootView: View {
    private enum Tab {
        case market
        case bot
    }

    @EnvironmentObject private var settings: AppSettings
    @EnvironmentObject private var symbolStore: SymbolStore
    @State private var showingSettings = false
    @State private var selectedTab: Tab = .bot

    var body: some View {
        TabView(selection: $selectedTab) {
            DashboardScreen()
                .tag(Tab.market)
                .tabItem {
                    Label("Market", systemImage: "waveform.path.ecg.rectangle")
                }

            BybitBotScreen()
                .tag(Tab.bot)
                .tabItem {
                    Label("Bot", systemImage: "bolt.horizontal.circle")
                }
        }
        .toolbarBackground(.visible, for: .tabBar)
        .sheet(isPresented: $showingSettings) {
            SettingsScreen()
                .environmentObject(settings)
                .environmentObject(symbolStore)
        }
        .overlay(alignment: .topTrailing) {
            Button {
                showingSettings = true
            } label: {
                Image(systemName: "slider.horizontal.3")
                    .font(.headline.weight(.semibold))
                    .padding(14)
                    .background(.ultraThinMaterial, in: Circle())
            }
            .padding(.trailing, 20)
            .padding(.top, 8)
        }
    }
}
