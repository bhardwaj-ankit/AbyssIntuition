import Foundation

@MainActor
final class AppSettings: ObservableObject {
    enum BackendProfile: String, CaseIterable, Identifiable {
        case simulatorLocal
        case deviceTailscale
        case custom

        var id: String { rawValue }

        var title: String {
            switch self {
            case .simulatorLocal:
                return "Simulator Local"
            case .deviceTailscale:
                return "Device Tailscale"
            case .custom:
                return "Custom"
            }
        }
    }

    // iOS Simulator can reliably reach services on the host Mac via localhost.
    // Prefer the FastAPI port directly to avoid relying on the reverse proxy/port 80.
    private static let defaultSimulatorAPIBaseURL = "http://127.0.0.1:8000"
    // Physical devices on the same Wi-Fi typically need the Mac's LAN IP (kept as Custom).
    private static let legacyDefaultLANAPIBaseURL = "http://172.20.10.2:8000"
    private static let defaultTailscaleAPIBaseURL = "http://100.91.99.54"
    private static let defaultAPIAccessToken = "swRhuQk4p6E16hOTv_uosKRFqMURPOKSNX9cog73br0"

    @Published var backendProfile: BackendProfile {
        didSet {
            UserDefaults.standard.set(backendProfile.rawValue, forKey: Keys.backendProfile)
        }
    }

    @Published var apiBaseURL: String {
        didSet {
            UserDefaults.standard.set(apiBaseURL, forKey: Keys.apiBaseURL)
        }
    }

    @Published var apiAccessToken: String {
        didSet {
            UserDefaults.standard.set(apiAccessToken, forKey: Keys.apiAccessToken)
        }
    }

    @Published var selectedSymbol: String {
        didSet {
            UserDefaults.standard.set(selectedSymbol, forKey: Keys.selectedSymbol)
        }
    }

    init() {
        let storedProfile = UserDefaults.standard.string(forKey: Keys.backendProfile)
        let initialProfile = Self.migratedProfile(from: storedProfile)
        let initialBaseURL = Self.migratedBaseURL(
            from: UserDefaults.standard.string(forKey: Keys.apiBaseURL),
            profile: initialProfile
        )
        let profile = Self.normalizedProfile(initialProfile, baseURL: initialBaseURL)
        let baseURL = Self.normalizedBaseURL(initialBaseURL, profile: profile)
        let accessToken = Self.migratedAccessToken(from: UserDefaults.standard.string(forKey: Keys.apiAccessToken))
        let selectedSymbol = Self.migratedSymbol(from: UserDefaults.standard.string(forKey: Keys.selectedSymbol))

        self.backendProfile = profile
        self.apiBaseURL = baseURL
        self.apiAccessToken = accessToken
        self.selectedSymbol = selectedSymbol

        UserDefaults.standard.set(profile.rawValue, forKey: Keys.backendProfile)
        UserDefaults.standard.set(baseURL, forKey: Keys.apiBaseURL)
        UserDefaults.standard.set(accessToken, forKey: Keys.apiAccessToken)
        UserDefaults.standard.set(selectedSymbol, forKey: Keys.selectedSymbol)
    }

    func makeClient() throws -> APIClient {
        try APIClient(baseURLString: apiBaseURL, apiAccessToken: apiAccessToken)
    }

    func sanitizedBaseURL() -> String {
        apiBaseURL.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func applyBackendProfile(_ profile: BackendProfile) {
        backendProfile = profile
        switch profile {
        case .simulatorLocal:
            apiBaseURL = Self.defaultSimulatorAPIBaseURL
        case .deviceTailscale:
            apiBaseURL = Self.defaultTailscaleAPIBaseURL
        case .custom:
            break
        }
    }

    func resetBackendDefaults() {
        applyBackendProfile(Self.defaultProfile())
        apiAccessToken = Self.defaultAPIAccessToken
    }

    private static func migratedProfile(from stored: String?) -> BackendProfile {
        if let stored, let profile = BackendProfile(rawValue: stored) {
            return profile
        }
        return defaultProfile()
    }

    private static func migratedBaseURL(from stored: String?, profile: BackendProfile) -> String {
        let trimmed = stored?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if trimmed.isEmpty {
            return defaultBaseURL(for: profile)
        }

        let normalized = trimmed.lowercased()
        if normalized == "http://127.0.0.1"
            || normalized == "http://127.0.0.1:8000"
            || normalized == "http://localhost"
            || normalized == "http://localhost:8000"
        {
            return defaultBaseURL(for: profile)
        }

        return trimmed
    }

    private static func migratedAccessToken(from stored: String?) -> String {
        let trimmed = stored?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return trimmed.isEmpty ? defaultAPIAccessToken : trimmed
    }

    private static func normalizedProfile(_ profile: BackendProfile, baseURL: String) -> BackendProfile {
        #if targetEnvironment(simulator)
        let normalizedURL = baseURL.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if normalizedURL.contains("127.0.0.1") || normalizedURL.contains("localhost") {
            return .simulatorLocal
        }
        return .simulatorLocal
        #else
        return profile
        #endif
    }

    private static func normalizedBaseURL(_ baseURL: String, profile: BackendProfile) -> String {
        #if targetEnvironment(simulator)
        let normalizedURL = baseURL.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if normalizedURL.contains("127.0.0.1") || normalizedURL.contains("localhost") {
            return defaultSimulatorAPIBaseURL
        }
        return defaultSimulatorAPIBaseURL
        #else
        return baseURL
        #endif
    }

    private static func migratedSymbol(from stored: String?) -> String {
        let trimmed = stored?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return trimmed.isEmpty ? "BTCUSDT" : trimmed
    }

    private static func defaultBaseURL(for profile: BackendProfile) -> String {
        switch profile {
        case .simulatorLocal:
            return defaultSimulatorAPIBaseURL
        case .deviceTailscale:
            return defaultTailscaleAPIBaseURL
        case .custom:
            return defaultTailscaleAPIBaseURL
        }
    }

    private static func defaultProfile() -> BackendProfile {
        #if targetEnvironment(simulator)
        return .simulatorLocal
        #else
        return .deviceTailscale
        #endif
    }

    private enum Keys {
        static let backendProfile = "backendProfile"
        static let apiBaseURL = "apiBaseURL"
        static let apiAccessToken = "apiAccessToken"
        static let selectedSymbol = "selectedSymbol"
    }
}
