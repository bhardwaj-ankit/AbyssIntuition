import Foundation

enum APIClientError: LocalizedError {
    case invalidBaseURL
    case invalidResponse
    case transportError(String)
    case serverError(Int, String)

    var errorDescription: String? {
        switch self {
        case .invalidBaseURL:
            return "The API base URL is invalid."
        case .invalidResponse:
            return "The server returned an unexpected response."
        case let .transportError(message):
            return message
        case let .serverError(code, message):
            return "Server error \(code): \(message)"
        }
    }
}

struct APIClient {
    let baseURL: URL
    let apiAccessToken: String
    let decoder: JSONDecoder

    init(baseURLString: String, apiAccessToken: String = "") throws {
        guard let baseURL = URL(string: baseURLString.trimmingCharacters(in: .whitespacesAndNewlines)) else {
            throw APIClientError.invalidBaseURL
        }
        self.baseURL = baseURL
        self.apiAccessToken = apiAccessToken.trimmingCharacters(in: .whitespacesAndNewlines)
        let decoder = JSONDecoder()
        self.decoder = decoder
    }

    func fetchHealth() async throws -> HealthResponse {
        try await get(path: "/health", queryItems: [])
    }

    func fetchSymbols(limit: Int = 250) async throws -> MarketSymbolsResponse {
        try await get(path: "/market/symbols", queryItems: [
            URLQueryItem(name: "quote_asset", value: "USDT"),
            URLQueryItem(name: "limit", value: String(limit))
        ])
    }

    func fetchSignal(symbol: String) async throws -> SignalAPIResponse {
        try await get(path: "/signal", queryItems: [URLQueryItem(name: "symbol", value: symbol)])
    }

    func fetchMarketBehavior(symbol: String) async throws -> MarketBehaviorResponse {
        try await get(path: "/market/behavior", queryItems: [URLQueryItem(name: "symbol", value: symbol)])
    }

    func fetchLiquidationMap(symbol: String) async throws -> LiquidationMapResponse {
        try await get(path: "/liquidation-map", queryItems: [
            URLQueryItem(name: "symbol", value: symbol),
            URLQueryItem(name: "include_events", value: "true"),
            URLQueryItem(name: "event_limit", value: "25"),
            URLQueryItem(name: "history_points", value: "36"),
            URLQueryItem(name: "candle_interval", value: "5m"),
            URLQueryItem(name: "candle_limit", value: "144")
        ])
    }

    func fetchBotConfig() async throws -> DemoBotConfigStatus {
        try await get(path: "/bot/demo/bybit/config", queryItems: [])
    }

    func fetchBotStatus(symbol: String) async throws -> DemoBotStatus {
        try await get(path: "/bot/demo/bybit/status", queryItems: [URLQueryItem(name: "symbol", value: symbol)])
    }

    func fetchBotPerformance(symbol: String, limit: Int = 30) async throws -> DemoBotPerformanceResponse {
        try await get(path: "/bot/demo/bybit/performance", queryItems: [
            URLQueryItem(name: "symbol", value: symbol),
            URLQueryItem(name: "limit", value: String(limit))
        ])
    }

    func startBot(request: BotStartRequest) async throws -> DemoBotStatus {
        try await post(path: "/bot/demo/bybit/start", queryItems: [
            URLQueryItem(name: "symbol", value: request.symbol),
            URLQueryItem(name: "poll_interval_seconds", value: String(request.pollIntervalSeconds)),
            URLQueryItem(name: "leverage", value: String(request.leverage)),
            URLQueryItem(name: "risk_per_trade_pct", value: String(request.riskPerTradePct)),
            URLQueryItem(name: "max_margin_fraction", value: String(request.maxMarginFraction)),
            URLQueryItem(name: "cooldown_seconds", value: String(request.cooldownSeconds)),
            URLQueryItem(name: "mode", value: request.mode)
        ])
    }

    func stopBot(closePosition: Bool = true) async throws -> DemoBotStatus {
        try await post(path: "/bot/demo/bybit/stop", queryItems: [
            URLQueryItem(name: "close_position", value: closePosition ? "true" : "false")
        ])
    }

    private func get<T: Decodable>(path: String, queryItems: [URLQueryItem]) async throws -> T {
        var request = try buildRequest(path: path, queryItems: queryItems)
        request.httpMethod = "GET"
        return try await perform(request)
    }

    private func post<T: Decodable>(path: String, queryItems: [URLQueryItem]) async throws -> T {
        var request = try buildRequest(path: path, queryItems: queryItems)
        request.httpMethod = "POST"
        return try await perform(request)
    }

    private func buildRequest(path: String, queryItems: [URLQueryItem]) throws -> URLRequest {
        guard var components = URLComponents(url: baseURL.appendingPathComponent(path), resolvingAgainstBaseURL: false) else {
            throw APIClientError.invalidBaseURL
        }
        components.queryItems = queryItems.isEmpty ? nil : queryItems
        guard let url = components.url else {
            throw APIClientError.invalidBaseURL
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 20
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if !apiAccessToken.isEmpty {
            request.setValue("Bearer \(apiAccessToken)", forHTTPHeaderField: "Authorization")
        }
        return request
    }

    private func perform<T: Decodable>(_ request: URLRequest) async throws -> T {
        let (data, response): (Data, URLResponse)
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch {
            // Network.framework can emit noisy `nw_connection_*` logs when requests fail early.
            // Surface an actionable message to the user instead of a vague transport error.
            if let urlError = error as? URLError {
                let target = request.url?.absoluteString ?? "the configured API"
                let hint: String
                switch urlError.code {
                case .notConnectedToInternet:
                    hint = "Not connected to the internet."
                case .cannotFindHost, .cannotConnectToHost, .timedOut, .networkConnectionLost:
                    hint = "Cannot connect to \(target). Make sure the backend is running, and the Base URL is reachable from this device."
                case .secureConnectionFailed, .serverCertificateUntrusted, .serverCertificateHasBadDate, .serverCertificateHasUnknownRoot:
                    hint = "TLS/certificate error connecting to \(target). For local testing, prefer http:// URLs unless you have a trusted HTTPS certificate."
                default:
                    hint = "Network error connecting to \(target): \(urlError.localizedDescription)"
                }
                throw APIClientError.transportError(hint)
            }
            throw APIClientError.transportError(error.localizedDescription)
        }
        guard let http = response as? HTTPURLResponse else {
            throw APIClientError.invalidResponse
        }
        guard (200 ... 299).contains(http.statusCode) else {
            let body = String(data: data, encoding: .utf8) ?? "Unknown server error"
            throw APIClientError.serverError(http.statusCode, body)
        }
        return try decoder.decode(T.self, from: data)
    }
}
