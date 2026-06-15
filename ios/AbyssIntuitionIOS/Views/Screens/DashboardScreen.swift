import SwiftUI

struct DashboardScreen: View {
    @EnvironmentObject private var settings: AppSettings
    @EnvironmentObject private var symbolStore: SymbolStore
    @StateObject private var viewModel = DashboardViewModel()

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    header
                    if let error = viewModel.errorMessage {
                        errorBanner(error)
                    }
                    if let signal = viewModel.signal {
                        signalHero(signal)
                        marketRead(signal)
                    }
                    if let behavior = viewModel.behavior {
                        behaviorCard(behavior)
                    }
                    if let liquidation = viewModel.liquidation {
                        liquidationCard(liquidation)
                    }
                }
                .padding(20)
            }
            .navigationTitle("Abyss Intuition")
            .navigationBarTitleDisplayMode(.inline)
            .appBackdrop()
            .task {
                await loadAll()
            }
            .refreshable {
                await loadAll()
            }
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Live crypto signal stack on mobile")
                .font(.system(size: 30, weight: .bold, design: .rounded))
                .foregroundStyle(.white)
            Text("Unified signal, market behavior, and liquidation context with one backend source of truth.")
                .font(.callout)
                .foregroundStyle(.white.opacity(0.76))

            SymbolSelectionButton(title: "Symbol", symbols: symbolOptions, selectedSymbol: $settings.selectedSymbol)
            .onChange(of: settings.selectedSymbol) { _, _ in
                Task { await loadAll() }
            }
        }
    }

    private func signalHero(_ payload: SignalAPIResponse) -> some View {
        let signal = payload.signal
        return GlassCard(title: "Unified Signal", subtitle: payload.cumulative.strategyMode.replacingOccurrences(of: "_", with: " ")) {
            VStack(alignment: .leading, spacing: 16) {
                VStack(alignment: .leading, spacing: 10) {
                    Text(signal.direction.rawValue)
                        .font(.system(size: 42, weight: .black, design: .rounded))
                        .foregroundStyle(signal.direction.glow)
                    Text("Quality \(signal.signalQuality) • \(signal.horizon)")
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(.white.opacity(0.72))
                }

                HStack(spacing: 12) {
                    MetricPill(label: "Confidence", value: AppFormat.confidence(signal.confidence), tint: signal.direction.tint)
                    MetricPill(label: "Price", value: AppFormat.price(signal.currentPrice))
                }
                HStack(spacing: 12) {
                    MetricPill(label: "Take Profit", value: AppFormat.price(signal.tp), tint: Color.green)
                    MetricPill(label: "Stop Loss", value: AppFormat.price(signal.sl), tint: Color.red)
                }
            }
        }
    }

    private func marketRead(_ payload: SignalAPIResponse) -> some View {
        GlassCard(title: "Signal Structure", subtitle: "Internal timeframes all read from the same signal API payload") {
            VStack(alignment: .leading, spacing: 14) {
                HStack(spacing: 12) {
                    MetricPill(label: "Market Mode", value: payload.cumulative.marketMode)
                    MetricPill(label: "Agreement", value: AppFormat.confidence(payload.cumulative.agreementRatio))
                }
                HStack(spacing: 12) {
                    MetricPill(label: "HTF Bias", value: payload.explain.features.htfRegime)
                    MetricPill(label: "Spread", value: AppFormat.percent(payload.explain.features.spreadBps / 100, digits: 3))
                }

                VStack(alignment: .leading, spacing: 10) {
                    Text("Timeframes")
                        .font(.headline)
                        .foregroundStyle(.white)
                    ForEach(payload.cumulative.timeframes) { timeframe in
                        HStack(alignment: .top) {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(timeframe.timeframe)
                                    .font(.headline.weight(.semibold))
                                    .foregroundStyle(.white)
                                Text("\(timeframe.marketMode) • \(timeframe.pattern)")
                                    .font(.caption)
                                    .foregroundStyle(.white.opacity(0.65))
                            }
                            Spacer()
                            VStack(alignment: .trailing, spacing: 4) {
                                Text(timeframe.direction.rawValue)
                                    .font(.subheadline.weight(.bold))
                                    .foregroundStyle(timeframe.direction.tint)
                                Text(AppFormat.confidence(timeframe.confidence))
                                    .font(.caption)
                                    .foregroundStyle(.white.opacity(0.7))
                            }
                        }
                        .padding(12)
                        .background(Color.black.opacity(0.18), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                    }
                }

                if !payload.signal.reasons.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Decision reasons")
                            .font(.headline)
                            .foregroundStyle(.white)
                        ForEach(payload.signal.reasons.prefix(6), id: \.self) { reason in
                            Text("• \(reason)")
                                .font(.footnote)
                                .foregroundStyle(.white.opacity(0.78))
                        }
                    }
                }
            }
        }
    }

    private func behaviorCard(_ behavior: MarketBehaviorResponse) -> some View {
        GlassCard(title: "Market Behavior", subtitle: behavior.regime.replacingOccurrences(of: "_", with: " ")) {
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 12) {
                    MetricPill(label: "Mode", value: behavior.mode.capitalized)
                    MetricPill(label: "Bias", value: behavior.bias.capitalized)
                }
                if let strength = behavior.biasStrength {
                    MetricPill(label: "Bias Strength", value: AppFormat.confidence(strength))
                }
                Text(behaviorSummary(behavior))
                    .font(.body)
                    .foregroundStyle(.white.opacity(0.85))
                if let breakdown = behavior.breakdown {
                    Text(breakdownSummary(breakdown))
                        .font(.footnote)
                        .foregroundStyle(.white.opacity(0.7))
                }
            }
        }
    }

    private func liquidationCard(_ liquidation: LiquidationMapResponse) -> some View {
        GlassCard(title: "Liquidity Magnet", subtitle: liquidation.source) {
            VStack(alignment: .leading, spacing: 14) {
                HStack(spacing: 12) {
                    MetricPill(label: "Dominant Pull", value: liquidation.dominantPull.rawValue, tint: liquidation.dominantPull.tint)
                    MetricPill(label: "Confidence", value: AppFormat.confidence(liquidation.confidence))
                }
                HStack(spacing: 12) {
                    MetricPill(label: "Events", value: "\(liquidation.eventsSummary.totalEvents)")
                    MetricPill(label: "OI Change", value: AppFormat.percent(liquidation.marketMetrics.openInterestChangePct))
                }
                HStack(spacing: 12) {
                    MetricPill(label: "Funding", value: AppFormat.signed(liquidation.marketMetrics.lastFundingRateBps))
                    MetricPill(label: "Orderbook", value: AppFormat.signed(liquidation.marketMetrics.orderBookImbalance, digits: 3))
                }
                Text(liquidation.quality.degradedMode ? (liquidation.quality.degradedReason ?? "Degraded liquidation quality") : "Liquidation quality looks healthy.")
                    .font(.footnote)
                    .foregroundStyle(liquidation.quality.degradedMode ? Color.yellow : Color.white.opacity(0.72))
            }
        }
    }

    private func errorBanner(_ message: String) -> some View {
        Text(message)
            .font(.footnote.weight(.medium))
            .foregroundStyle(.white)
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.red.opacity(0.35), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
    }

    private var symbolOptions: [String] {
        let storeSymbols = symbolStore.symbols.map(\.symbol)
        return storeSymbols.isEmpty ? [settings.selectedSymbol, "ETHUSDT", "SOLUSDT"] : storeSymbols
    }

    private func loadAll() async {
        do {
            let client = try settings.makeClient()
            await symbolStore.load(using: client, preserving: settings.selectedSymbol)
            await viewModel.refresh(using: client, symbol: settings.selectedSymbol)
        } catch {
            viewModel.errorMessage = "\(error.localizedDescription) (Base URL: \(settings.sanitizedBaseURL()))"
        }
    }

    private func behaviorSummary(_ behavior: MarketBehaviorResponse) -> String {
        if let error = behavior.error, !error.isEmpty {
            return error
        }
        return "\(behavior.mode.capitalized) market with \(behavior.bias.lowercased()) bias under \(behavior.regime.replacingOccurrences(of: "_", with: " ").lowercased())."
    }

    private func breakdownSummary(_ breakdown: MarketBehaviorBreakdown) -> String {
        let rows = [
            ("1H", breakdown.regime1h),
            ("4H", breakdown.regime4h),
            ("12H", breakdown.regime12h),
        ]
        let parts = rows.compactMap { label, leg -> String? in
            guard let leg else { return nil }
            let direction = leg.direction?.rawValue ?? "N/A"
            return "\(label): \(direction) \(leg.strength)"
        }
        return parts.joined(separator: " • ")
    }
}
