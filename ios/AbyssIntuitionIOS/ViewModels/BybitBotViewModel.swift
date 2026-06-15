import Foundation

@MainActor
final class BybitBotViewModel: ObservableObject {
    @Published private(set) var config: DemoBotConfigStatus?
    @Published private(set) var status: DemoBotStatus?
    @Published private(set) var performance: DemoBotPerformanceResponse?
    @Published private(set) var health: HealthResponse?
    @Published private(set) var isLoading = false
    @Published var errorMessage: String?

    @Published var mode = "SCALPING"
    @Published var leverage = 2.0
    @Published var riskPerTradePct = 0.01
    @Published var maxMarginFraction = 0.35
    @Published var pollIntervalSeconds = 5
    @Published var cooldownSeconds = 30

    let modes = ["CONSERVATIVE", "BALANCED", "AGGRESSIVE", "SCALPING"]

    func refresh(using client: APIClient, symbol: String) async {
        isLoading = true
        defer { isLoading = false }

        do {
            async let healthRequest = client.fetchHealth()
            async let configRequest = client.fetchBotConfig()
            async let statusRequest = client.fetchBotStatus(symbol: symbol)
            async let performanceRequest = client.fetchBotPerformance(symbol: symbol)

            let (health, config, status, performance) = try await (healthRequest, configRequest, statusRequest, performanceRequest)
            self.health = health
            self.config = config
            self.status = status
            self.performance = performance
            hydrateControls(from: status)
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func start(using client: APIClient, symbol: String) async {
        isLoading = true
        defer { isLoading = false }

        do {
            let started = try await client.startBot(request: BotStartRequest(
                symbol: symbol,
                pollIntervalSeconds: pollIntervalSeconds,
                leverage: leverage,
                riskPerTradePct: riskPerTradePct,
                maxMarginFraction: maxMarginFraction,
                cooldownSeconds: cooldownSeconds,
                mode: mode
            ))
            status = started
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func stop(using client: APIClient) async {
        isLoading = true
        defer { isLoading = false }

        do {
            status = try await client.stopBot(closePosition: true)
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func hydrateControls(from status: DemoBotStatus) {
        mode = status.mode
        leverage = status.leverage
        riskPerTradePct = status.riskPerTradePct
        maxMarginFraction = status.maxMarginFraction
        pollIntervalSeconds = status.pollIntervalSeconds
        cooldownSeconds = status.cooldownSeconds
    }
}
