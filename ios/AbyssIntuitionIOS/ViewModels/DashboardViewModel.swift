import Foundation

@MainActor
final class DashboardViewModel: ObservableObject {
    @Published private(set) var signal: SignalAPIResponse?
    @Published private(set) var behavior: MarketBehaviorResponse?
    @Published private(set) var liquidation: LiquidationMapResponse?
    @Published private(set) var isLoading = false
    @Published var errorMessage: String?

    func refresh(using client: APIClient, symbol: String) async {
        isLoading = true
        defer { isLoading = false }

        do {
            async let signalRequest = client.fetchSignal(symbol: symbol)
            async let behaviorRequest = client.fetchMarketBehavior(symbol: symbol)
            async let liquidationRequest = client.fetchLiquidationMap(symbol: symbol)

            let (signal, behavior, liquidation) = try await (signalRequest, behaviorRequest, liquidationRequest)
            self.signal = signal
            self.behavior = behavior
            self.liquidation = liquidation
            errorMessage = nil
        } catch {
            if case APIClientError.serverError(500, _) = error {
                errorMessage = "Market analytics are temporarily unavailable from the upstream data source. The Bybit bot tab and protected app access still work."
            } else {
                errorMessage = error.localizedDescription
            }
        }
    }
}
