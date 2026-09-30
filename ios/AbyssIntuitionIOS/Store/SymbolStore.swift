import Foundation

@MainActor
final class SymbolStore: ObservableObject {
    private static let fallbackSymbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "NEARUSDT", "XRPUSDT"]

    @Published private(set) var symbols: [MarketSymbol] = []
    @Published private(set) var isLoading = false
    @Published var errorMessage: String?

    func load(using client: APIClient, preserving selectedSymbol: String) async {
        guard !isLoading else { return }
        isLoading = true
        defer { isLoading = false }

        do {
            let response = try await client.fetchSymbols()
            symbols = response.symbols.sorted { $0.symbol < $1.symbol }
            errorMessage = nil
            if !symbols.contains(where: { $0.symbol == selectedSymbol }) && !symbols.isEmpty {
                errorMessage = "Current symbol is not in the exchange symbol list."
            }
        } catch {
            if symbols.isEmpty {
                symbols = Self.fallbackSymbols.map {
                    MarketSymbol(symbol: $0, baseAsset: String($0.dropLast(4)), quoteAsset: "USDT", status: "TRADING")
                }
            }
            errorMessage = error.localizedDescription
        }
    }
}
