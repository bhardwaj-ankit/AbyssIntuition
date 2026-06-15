import SwiftUI

struct SectionBackground: ViewModifier {
    func body(content: Content) -> some View {
        content
            .background(
                LinearGradient(
                    colors: [
                        Color(red: 0.07, green: 0.09, blue: 0.16),
                        Color(red: 0.13, green: 0.15, blue: 0.24),
                        Color(red: 0.09, green: 0.20, blue: 0.25)
                    ],
                    startPoint: .topLeading,
                    endPoint: .bottomTrailing
                )
                .ignoresSafeArea()
            )
    }
}

extension View {
    func appBackdrop() -> some View {
        modifier(SectionBackground())
    }
}
