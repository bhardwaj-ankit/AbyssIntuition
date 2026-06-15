import SwiftUI

struct SettingsScreen: View {
    @EnvironmentObject private var settings: AppSettings
    @EnvironmentObject private var symbolStore: SymbolStore
    @Environment(\.dismiss) private var dismiss
    @State private var draftProfile = AppSettings.BackendProfile.custom
    @State private var draftURL = ""
    @State private var draftToken = ""
    @State private var connectivityMessage = ""
    @State private var isChecking = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Backend") {
                    Picker("Backend profile", selection: $draftProfile) {
                        ForEach(AppSettings.BackendProfile.allCases) { profile in
                            Text(profile.title).tag(profile)
                        }
                    }
                    .onChange(of: draftProfile) { _, profile in
                        draftURL = defaultURL(for: profile, currentDraft: draftURL)
                        connectivityMessage = "\(profile.title) profile loaded. Tap Save to keep it."
                    }

                    TextField("API Base URL", text: $draftURL)
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                        .autocorrectionDisabled()
                        .onChange(of: draftURL) { _, newValue in
                            let trimmed = newValue.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
                            if trimmed != defaultURL(for: draftProfile, currentDraft: newValue).lowercased(),
                               draftProfile != .custom
                            {
                                draftProfile = .custom
                            }
                        }
                    SecureField("API Access Token (optional)", text: $draftToken)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    Text("Use `Simulator Local` for the iOS simulator (uses `http://127.0.0.1:8000` on this Mac), and `Device Tailscale` for your physical iPhone over Tailscale. If you type a different URL manually (for example your LAN IP), the profile switches to `Custom`.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)

                    Button(isChecking ? "Checking..." : "Test connection") {
                        Task { await testConnection() }
                    }
                    .disabled(isChecking)

                    if !connectivityMessage.isEmpty {
                        Text(connectivityMessage)
                            .font(.footnote)
                    }

                    Button("Restore recommended profile defaults") {
                        settings.resetBackendDefaults()
                        draftProfile = {
                            #if targetEnvironment(simulator)
                            .simulatorLocal
                            #else
                            .deviceTailscale
                            #endif
                        }()
                        draftURL = defaultURL(for: draftProfile, currentDraft: settings.apiBaseURL)
                        draftToken = settings.apiAccessToken
                        connectivityMessage = "Recommended defaults restored. Tap Save to keep them."
                    }
                    .font(.footnote.weight(.medium))
                }

                Section("Active Symbol") {
                    SymbolSelectionButton(
                        title: "Default symbol",
                        symbols: symbolOptions,
                        selectedSymbol: $settings.selectedSymbol,
                        tint: .primary
                    )
                }
            }
            .navigationTitle("Settings")
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Close") { dismiss() }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Save") {
                        settings.applyBackendProfile(draftProfile)
                        settings.apiBaseURL = draftURL.trimmingCharacters(in: .whitespacesAndNewlines)
                        settings.apiAccessToken = draftToken.trimmingCharacters(in: .whitespacesAndNewlines)
                        dismiss()
                    }
                }
            }
            .onAppear {
                draftProfile = settings.backendProfile
                draftURL = settings.apiBaseURL
                draftToken = settings.apiAccessToken
            }
        }
    }

    private func testConnection() async {
        isChecking = true
        defer { isChecking = false }
        do {
            let client = try APIClient(baseURLString: draftURL, apiAccessToken: draftToken)
            _ = try await client.fetchHealth()
            do {
                let config = try await client.fetchBotConfig()
                connectivityMessage = "Connected: protected API access is working. Bybit config is \(config.configured ? "ready" : "not ready")."
            } catch let APIClientError.serverError(code, _) where code == 401 {
                connectivityMessage = "Connected to /health, but token is missing/invalid (401). Update API Access Token and try again."
            } catch {
                connectivityMessage = error.localizedDescription
            }
        } catch {
            connectivityMessage = error.localizedDescription
        }
    }

    private var symbolOptions: [String] {
        let options = symbolStore.symbols.map(\.symbol)
        return options.isEmpty ? [settings.selectedSymbol, "BTCUSDT", "ETHUSDT", "SOLUSDT"] : options
    }

    private func defaultURL(for profile: AppSettings.BackendProfile, currentDraft: String) -> String {
        switch profile {
        case .simulatorLocal:
            return "http://127.0.0.1:8000"
        case .deviceTailscale:
            return "http://100.91.99.54"
        case .custom:
            return currentDraft
        }
    }
}
