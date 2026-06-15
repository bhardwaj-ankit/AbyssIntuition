# Internet Setup Guide

This guide is for exposing the Python backend safely so the iPhone app can connect from another network.

## Recommended Architecture

- `app` container runs FastAPI on internal port `8000`
- `caddy` is the only public entrypoint on ports `80` and `443`
- optional `API_ACCESS_TOKEN` protects the API

The compose file now binds the Python app to `127.0.0.1:8000` on the host, which keeps direct public access off the table. Public traffic should go through Caddy.

## Option 1: Same Wi-Fi or Same LAN

1. Install Docker Desktop.
2. Start the stack:

```bash
docker compose up -d --build
```

3. In the iPhone app, use:

- URL: `http://172.20.10.2`
- Token: `swRhuQk4p6E16hOTv_uosKRFqMURPOKSNX9cog73br0`

4. Tap `Test connection`.

## Option 1B: Tailscale For One Device

This is the cleanest option when:
- you only want to call the API from your own iPhone
- both the Mac and iPhone can join the same Tailscale tailnet
- you do not want router port forwarding or a public tunnel

Current host Tailscale address:

- `http://100.91.99.54`

Steps:

1. Install Tailscale on the Mac and iPhone.
2. Log both devices into the same Tailscale account/tailnet.
3. Start the backend:

```bash
./deploy/run_tailscale_access.sh
```

4. In the iPhone app, use:

- URL: `http://100.91.99.54`
- Token: your `API_ACCESS_TOKEN`

Notes:
- traffic stays inside Tailscale
- no public domain is needed
- no Cloudflare tunnel is needed
- this is the best fit for your “one device only” use case

## Option 2: Different Network Without Buying A Domain

This works, but only over plain HTTP unless you add a domain later.

1. Make sure your public IP is currently:

- `2.48.224.182`

2. Log into your router.
3. Add port forwarding rules:

- external TCP `80` -> `172.20.10.2:80`
- external TCP `443` -> `172.20.10.2:443`

4. On macOS, allow inbound traffic if the firewall prompts.
5. Start the stack:

```bash
docker compose up -d --build
```

6. In the iPhone app, use:

- URL: `http://2.48.224.182`
- Token: `swRhuQk4p6E16hOTv_uosKRFqMURPOKSNX9cog73br0`

Notes:
- This only stays stable while your public IP stays the same.
- Many home internet connections change IPs over time.
- This is acceptable for testing, not ideal for long-term use.

## Option 2B: Different Network Without A Domain Or Router Changes

This is the fastest remote-access option for testing from an iPhone on another network.

1. Install `cloudflared`:

```bash
brew install cloudflared
```

2. Start the tunnel:

```bash
cloudflared tunnel --url http://127.0.0.1 --no-autoupdate
```

3. Cloudflare will print a temporary `https://...trycloudflare.com` URL.
4. In the iPhone app, use:

- URL: the printed `https://...trycloudflare.com` URL
- Token: your `API_ACCESS_TOKEN`

Notes:
- the tunnel is temporary and changes when restarted
- the terminal running `cloudflared` must stay open
- this is excellent for testing, not the long-term production shape

Convenience scripts:

```bash
./deploy/run_remote_access.sh
./deploy/stop_remote_access.sh
```

`run_remote_access.sh` will:
- start `colima` if needed
- start the Docker stack
- verify the local API
- launch the free Cloudflare quick tunnel

## Option 3: Different Network With A Domain And HTTPS

This is the cleanest production path.

1. Buy or use a domain.
2. Create a DNS `A` record:

- `api.yourdomain.com` -> `2.48.224.182`

3. Edit [.env.docker](/Users/ankitbhardwaj/Documents/AbyssIntuition/.env.docker):

```dotenv
API_DOMAIN=api.yourdomain.com
ACME_EMAIL=you@example.com
API_ACCESS_TOKEN=replace-with-your-own-random-secret
```

4. Forward router ports:

- TCP `80` -> `172.20.10.2:80`
- TCP `443` -> `172.20.10.2:443`

5. Start the stack:

```bash
docker compose up -d --build
```

6. Caddy will request a certificate automatically.
7. In the iPhone app, use:

- URL: `https://api.yourdomain.com`
- Token: your `API_ACCESS_TOKEN`

## Option 3B: Stable Cloudflare Named Tunnel

This is the recommended route when you want:
- a stable remote URL
- HTTPS
- no router port forwarding
- cleaner long-term mobile access

Requirements:
- a Cloudflare account
- a domain or subdomain managed in Cloudflare DNS

Files prepared for this repo:
- [deploy/cloudflare/config.template.yml](/Users/ankitbhardwaj/Documents/AbyssIntuition/deploy/cloudflare/config.template.yml)
- [deploy/cloudflare/create_named_tunnel.sh](/Users/ankitbhardwaj/Documents/AbyssIntuition/deploy/cloudflare/create_named_tunnel.sh)
- [deploy/cloudflare/run_named_tunnel.sh](/Users/ankitbhardwaj/Documents/AbyssIntuition/deploy/cloudflare/run_named_tunnel.sh)

Steps:

1. Authenticate Cloudflare on this machine:

```bash
cloudflared tunnel login
```

2. Create the named tunnel and DNS route:

```bash
chmod +x deploy/cloudflare/create_named_tunnel.sh
./deploy/cloudflare/create_named_tunnel.sh abyssintuition-api api.yourdomain.com
```

3. Run the tunnel:

```bash
chmod +x deploy/cloudflare/run_named_tunnel.sh
./deploy/cloudflare/run_named_tunnel.sh abyssintuition-api
```

4. In the iPhone app, use:

- URL: `https://api.yourdomain.com`
- Token: your `API_ACCESS_TOKEN`

Notes:
- the tunnel forwards to local Caddy on `http://127.0.0.1`
- your Docker stack should already be running
- this is the best fit for remote iPhone access without opening home router ports

## Quick Checks

From another machine:

```bash
curl http://172.20.10.2/health
curl http://2.48.224.182/health
curl -H "Authorization: Bearer <token>" http://2.48.224.182/signal?symbol=BTCUSDT
```

With a domain:

```bash
curl https://api.yourdomain.com/health
curl -H "Authorization: Bearer <token>" https://api.yourdomain.com/signal?symbol=BTCUSDT
```

## Important Security Notes

- Rotate the current token before long-term public exposure.
- Keep `runtime/bybit_demo_config.json` only on the host.
- Do not commit real `.env.docker` values to git.
- Prefer a VPS or always-on Mac mini if this will run continuously.
