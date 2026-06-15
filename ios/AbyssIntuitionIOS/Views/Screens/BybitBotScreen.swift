import SwiftUI

struct BybitBotScreen: View {
    @EnvironmentObject private var settings: AppSettings
    @EnvironmentObject private var symbolStore: SymbolStore
    @StateObject private var viewModel = BybitBotViewModel()

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    intro
                    if let error = viewModel.errorMessage {
                        errorBanner(error)
                    }
                    controlCard
                    if let config = viewModel.config {
                        configCard(config)
                    }
                    if let status = viewModel.status {
                        statusCard(status)
                    }
                    if let performance = viewModel.performance {
                        performanceCard(performance)
                    }
                }
                .padding(20)
            }
            .navigationTitle("Bybit Demo Bot")
            .navigationBarTitleDisplayMode(.inline)
            .appBackdrop()
            .task {
                await refresh()
            }
            .refreshable {
                await refresh()
            }
            .onChange(of: settings.selectedSymbol) { _, _ in
                Task { await refresh() }
            }
        }
    }

    private var intro: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Trade control from iPhone")
                .font(.system(size: 30, weight: .bold, design: .rounded))
                .foregroundStyle(.white)
            Text("The phone app stays a client. All strategy, execution rules, and Bybit demo order flow remain on your backend.")
                .font(.callout)
                .foregroundStyle(.white.opacity(0.75))
        }
    }

    private var controlCard: some View {
        GlassCard(title: "Live Controls", subtitle: "Start, stop, and tune the demo bot without touching curl") {
            VStack(alignment: .leading, spacing: 16) {
                SymbolSelectionButton(title: "Symbol", symbols: symbolOptions, selectedSymbol: $settings.selectedSymbol)

                Picker("Mode", selection: $viewModel.mode) {
                    ForEach(viewModel.modes, id: \.self) { mode in
                        Text(mode.capitalized).tag(mode)
                    }
                }
                .pickerStyle(.segmented)

                VStack(spacing: 14) {
                    sliderRow(title: "Leverage", value: viewModel.leverage, range: 1 ... 5, step: 0.5, format: { AppFormat.price($0) + "x" }) {
                        viewModel.leverage = $0
                    }
                    sliderRow(title: "Risk / Trade", value: viewModel.riskPerTradePct, range: 0.005 ... 0.03, step: 0.001, format: { AppFormat.percent($0 * 100, digits: 2) }) {
                        viewModel.riskPerTradePct = $0
                    }
                    sliderRow(title: "Max Margin", value: viewModel.maxMarginFraction, range: 0.15 ... 0.60, step: 0.01, format: { AppFormat.percent($0 * 100, digits: 0) }) {
                        viewModel.maxMarginFraction = $0
                    }
                }

                HStack(spacing: 12) {
                    Stepper("Poll \(viewModel.pollIntervalSeconds)s", value: $viewModel.pollIntervalSeconds, in: 5 ... 60, step: 5)
                    Stepper("Cooldown \(viewModel.cooldownSeconds)s", value: $viewModel.cooldownSeconds, in: 0 ... 300, step: 5)
                }
                .font(.footnote.weight(.medium))
                .foregroundStyle(.white.opacity(0.85))

                HStack(spacing: 12) {
                    Button {
                        Task { await startBot() }
                    } label: {
                        Label(viewModel.status?.running == true ? "Running" : "Start", systemImage: "play.fill")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(Color.green)
                    .disabled(viewModel.status?.running == true || viewModel.isLoading)

                    Button {
                        Task { await stopBot() }
                    } label: {
                        Label("Stop", systemImage: "stop.fill")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)
                    .tint(Color.red)
                    .disabled(viewModel.status?.running != true || viewModel.isLoading)

                    Button {
                        Task { await refresh() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                            .frame(width: 44, height: 44)
                    }
                    .buttonStyle(.bordered)
                }
            }
        }
    }

    private func configCard(_ config: DemoBotConfigStatus) -> some View {
        GlassCard(title: "Exchange Config", subtitle: config.configured ? "Configured" : "Missing config") {
            VStack(alignment: .leading, spacing: 14) {
                HStack(spacing: 12) {
                    MetricPill(label: "Bybit", value: config.configured ? "Ready" : "Not Ready", tint: config.configured ? .green : .yellow)
                    MetricPill(label: "AI", value: config.aiEnabled ? "Enabled" : "Off", tint: config.aiEnabled ? .blue : .white)
                }
                Text(config.message)
                    .font(.footnote)
                    .foregroundStyle(.white.opacity(0.76))
                Text("Config source: \(config.configSource) • \(config.baseURL)")
                    .font(.caption)
                    .foregroundStyle(.white.opacity(0.62))
            }
        }
    }

    private func statusCard(_ status: DemoBotStatus) -> some View {
        GlassCard(title: "Live Status", subtitle: status.running ? "Bot running" : "Bot idle") {
            VStack(alignment: .leading, spacing: 14) {
                HStack(spacing: 12) {
                    MetricPill(label: "Equity", value: AppFormat.price(status.accountEquity))
                    MetricPill(label: "Wallet", value: AppFormat.price(status.walletBalance))
                }
                HStack(spacing: 12) {
                    MetricPill(label: "Trades", value: "\(status.totalTrades)")
                    MetricPill(label: "Last Action", value: status.lastAction.replacingOccurrences(of: "_", with: " ").capitalized)
                }
                if let signal = status.lastSignal {
                    HStack(spacing: 12) {
                        MetricPill(label: "Bot Signal", value: signal.direction.rawValue, tint: signal.direction.tint)
                        MetricPill(label: "Confidence", value: AppFormat.confidence(signal.confidence))
                    }
                }
                if let position = status.openPosition {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Open position")
                            .font(.headline)
                            .foregroundStyle(.white)
                        Text("\(position.side.rawValue) • size \(AppFormat.price(position.size)) • entry \(AppFormat.price(position.entryPrice))")
                            .font(.subheadline)
                            .foregroundStyle(.white.opacity(0.84))
                        Text("Mark \(AppFormat.price(position.markPrice)) • PnL \(AppFormat.signed(position.unrealizedPnL)) • ROI \(AppFormat.percent(position.unrealizedROIPct))")
                            .font(.footnote)
                            .foregroundStyle(.white.opacity(0.72))
                        if let liquidation = position.liquidationPrice {
                            Text("Liquidation \(AppFormat.price(liquidation)) • buffer \(AppFormat.percent(position.liquidationBufferPct))")
                                .font(.footnote)
                                .foregroundStyle(.white.opacity(0.72))
                        }
                    }
                } else {
                    Text("No exchange position.")
                        .font(.footnote)
                        .foregroundStyle(.white.opacity(0.68))
                }
                if let decision = status.aiLastDecision {
                    Text("AI \(decision.entryVerdict) • \(decision.exitAction) • \(decision.reason)")
                        .font(.footnote)
                        .foregroundStyle(.white.opacity(0.72))
                }
                if let error = status.lastError, !error.isEmpty {
                    Text(error)
                        .font(.footnote.weight(.medium))
                        .foregroundStyle(.white)
                        .padding(10)
                        .background(Color.red.opacity(0.30), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                }
            }
        }
    }

    private func performanceCard(_ performance: DemoBotPerformanceResponse) -> some View {
        GlassCard(title: "Past Performance", subtitle: "\(performance.environment) • last \(performance.lookbackTrades) trades") {
            VStack(alignment: .leading, spacing: 14) {
                HStack(spacing: 12) {
                    MetricPill(label: "Win Rate", value: AppFormat.confidence(performance.summary.winRate), tint: .green)
                    MetricPill(label: "Net PnL", value: AppFormat.signed(performance.summary.netPnL), tint: performance.summary.netPnL >= 0 ? .green : .red)
                }
                HStack(spacing: 12) {
                    MetricPill(label: "Profit Factor", value: performance.summary.profitFactor.map { AppFormat.price($0) } ?? "--")
                    MetricPill(label: "Avg ROI", value: AppFormat.percent(performance.summary.avgROIPct))
                }
                if !performance.trades.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Recent trades")
                            .font(.headline)
                            .foregroundStyle(.white)
                        ForEach(performance.trades.prefix(5)) { trade in
                            HStack(alignment: .top) {
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(trade.side.rawValue)
                                        .font(.subheadline.weight(.semibold))
                                        .foregroundStyle(trade.side.tint)
                                    Text(trade.entryReason.isEmpty ? "Managed by bot" : trade.entryReason)
                                        .font(.caption)
                                        .foregroundStyle(.white.opacity(0.64))
                                }
                                Spacer()
                                VStack(alignment: .trailing, spacing: 4) {
                                    Text(AppFormat.signed(trade.netPnL))
                                        .font(.subheadline.weight(.semibold))
                                        .foregroundStyle(trade.netPnL >= 0 ? Color.green : Color.red)
                                    Text(AppFormat.percent(trade.roiPct))
                                        .font(.caption)
                                        .foregroundStyle(.white.opacity(0.68))
                                }
                            }
                            .padding(12)
                            .background(Color.black.opacity(0.18), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                        }
                    }
                }
            }
        }
    }

    private func sliderRow(
        title: String,
        value: Double,
        range: ClosedRange<Double>,
        step: Double,
        format: (Double) -> String,
        onChange: @escaping (Double) -> Void
    ) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(title)
                Spacer()
                Text(format(value))
                    .foregroundStyle(.white.opacity(0.72))
            }
            .font(.footnote.weight(.medium))
            .foregroundStyle(.white)

            Slider(
                value: Binding(
                    get: { value },
                    set: { onChange($0) }
                ),
                in: range,
                step: step
            )
            .tint(.white)
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

    private func refresh() async {
        do {
            let client = try settings.makeClient()
            await symbolStore.load(using: client, preserving: settings.selectedSymbol)
            await viewModel.refresh(using: client, symbol: settings.selectedSymbol)
        } catch {
            viewModel.errorMessage = error.localizedDescription
        }
    }

    private func startBot() async {
        do {
            let client = try settings.makeClient()
            await viewModel.start(using: client, symbol: settings.selectedSymbol)
            await viewModel.refresh(using: client, symbol: settings.selectedSymbol)
        } catch {
            viewModel.errorMessage = error.localizedDescription
        }
    }

    private func stopBot() async {
        do {
            let client = try settings.makeClient()
            await viewModel.stop(using: client)
            await viewModel.refresh(using: client, symbol: settings.selectedSymbol)
        } catch {
            viewModel.errorMessage = error.localizedDescription
        }
    }
}
