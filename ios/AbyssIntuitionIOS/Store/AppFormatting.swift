import Foundation
import SwiftUI

enum AppFormat {
    static func price(_ value: Double?) -> String {
        guard let value else { return "--" }
        if value >= 1000 {
            return value.formatted(.number.precision(.fractionLength(2)))
        }
        if value >= 1 {
            return value.formatted(.number.precision(.fractionLength(4)))
        }
        return value.formatted(.number.precision(.fractionLength(6)))
    }

    static func percent(_ value: Double?, digits: Int = 2) -> String {
        guard let value else { return "--" }
        return value.formatted(.number.precision(.fractionLength(digits))) + "%"
    }

    static func confidence(_ value: Double?) -> String {
        guard let value else { return "--" }
        return percent(value * 100, digits: 1)
    }

    static func signed(_ value: Double?, digits: Int = 2) -> String {
        guard let value else { return "--" }
        let format = FloatingPointFormatStyle<Double>.number.precision(.fractionLength(digits))
        return value >= 0 ? "+" + value.formatted(format) : value.formatted(format)
    }

    static func timestamp(_ value: Int?) -> String {
        guard let value else { return "--" }
        let date = Date(timeIntervalSince1970: TimeInterval(value) / 1000)
        return date.formatted(date: .abbreviated, time: .shortened)
    }
}

extension Direction {
    var tint: Color {
        switch self {
        case .long:
            return Color(red: 0.11, green: 0.74, blue: 0.43)
        case .short:
            return Color(red: 0.89, green: 0.26, blue: 0.24)
        case .flat:
            return Color(red: 0.98, green: 0.71, blue: 0.15)
        }
    }

    var glow: LinearGradient {
        LinearGradient(
            colors: [tint.opacity(0.75), tint.opacity(0.22)],
            startPoint: .topLeading,
            endPoint: .bottomTrailing
        )
    }
}
