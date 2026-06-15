# Abyss Intuition iOS

SwiftUI client for the Abyss Intuition trading API.

## What It Includes

- Unified market signal view using the single `/signal` API
- Market behavior and liquidation-magnet summary
- Bybit demo bot config/status/performance screens
- Local backend URL settings so you can use simulator or physical iPhone

## Open In Xcode

From the repo root:

```bash
cd ios/AbyssIntuitionIOS
xcodegen generate
open AbyssIntuitionIOS.xcodeproj
```

## Build From Terminal

```bash
cd ios/AbyssIntuitionIOS
xcodegen generate
xcodebuild -project AbyssIntuitionIOS.xcodeproj -scheme AbyssIntuitionIOS -destination 'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO build
```

## Backend URL Notes

- iOS Simulator: `http://127.0.0.1:8000`
- Real iPhone over Tailscale: `http://<your-mac-tailscale-ip>` (Caddy on port 80)
- Real iPhone on same Wi-Fi (optional): `http://<your-mac-lan-ip>:8000`

Make sure the FastAPI backend is already running before you launch the app.

If you enable `API_ACCESS_TOKEN` on the backend, paste the same token into the iOS app settings. The client sends it as a Bearer token.
