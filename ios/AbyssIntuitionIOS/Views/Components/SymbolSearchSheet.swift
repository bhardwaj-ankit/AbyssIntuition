import SwiftUI

struct SymbolSearchSheet: View {
    let title: String
    let symbols: [String]
    @Binding var selectedSymbol: String

    @Environment(\.dismiss) private var dismiss
    @State private var query = ""

    private var filteredSymbols: [String] {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty {
            return symbols
        }
        return symbols.filter { $0.localizedCaseInsensitiveContains(trimmed) }
    }

    var body: some View {
        NavigationStack {
            List(filteredSymbols, id: \.self) { symbol in
                Button {
                    selectedSymbol = symbol
                    dismiss()
                } label: {
                    HStack {
                        Text(symbol)
                            .foregroundStyle(.primary)
                        Spacer()
                        if symbol == selectedSymbol {
                            Image(systemName: "checkmark.circle.fill")
                                .foregroundStyle(.green)
                        }
                    }
                }
            }
            .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always), prompt: "Search symbol")
            .navigationTitle(title)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                }
            }
        }
    }
}

struct SymbolSelectionButton: View {
    let title: String
    let symbols: [String]
    @Binding var selectedSymbol: String
    var tint: Color = .white

    @State private var showingSheet = false

    var body: some View {
        Button {
            showingSheet = true
        } label: {
            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(title)
                        .font(.caption.weight(.medium))
                        .foregroundStyle(tint.opacity(0.7))
                    Text(selectedSymbol)
                        .font(.headline.weight(.semibold))
                        .foregroundStyle(tint)
                }
                Spacer()
                Image(systemName: "magnifyingglass")
                    .font(.headline.weight(.semibold))
                    .foregroundStyle(tint)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .background(Color.white.opacity(0.08), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        }
        .buttonStyle(.plain)
        .sheet(isPresented: $showingSheet) {
            SymbolSearchSheet(title: title, symbols: symbols, selectedSymbol: $selectedSymbol)
        }
    }
}
