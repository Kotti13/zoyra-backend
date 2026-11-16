# Forward proxy

Standalone HTTP/HTTPS (`CONNECT`) and SOCKS5 proxy. It does **not** decrypt TLS.
Python 3.12 stdlib only — no extra packages.

This process is meant to run **on a different machine** than the Zoyra API. The API only needs `YT_PROXY` pointing at this host.

## Where to host it

YouTube sees **this machine’s public IP**. The API can stay on Render.

| Place | Use it? | Why |
|---|---|---|
| **Your home Mac / PC / Raspberry Pi** (always on, port-forward 8888) | **Yes — best** | Residential IP; this is what unblocks YouTube for a datacenter API |
| Small home NAS + Docker | Yes | Same as home IP |
| Cheap cloud VPS (Hetzner, DigitalOcean, AWS, Fly, Oracle) | Usually **no** | Datacenter IP — YouTube blocks it the same as Render |
| **Render / Railway / Vercel** | **No** | Datacenter IP **and** those platforms are HTTP apps, not a raw CONNECT/SOCKS TCP proxy |
| **Android phone (Termux + TCP tunnel)** | Yes | Mobile IP; see [ANDROID.md](ANDROID.md). Tailscale does not reach Render. |
| Paid residential list (Webshare) | Yes | Already supported via `WEBSHARE_API_KEY` / `YT_PROXY` on the API; you can skip self-hosting |

Do **not** put this proxy on the same Render box as the API. That does not change the IP YouTube sees.

**Practical setup:** API on Render, proxy on your home Mac (or Pi) with a public port.

You need:

1. The computer left on (or a Pi).
2. Router: forward **TCP 8888** → that computer (and 1080 if you use SOCKS).
3. A stable hostname helps if your home IP changes (DuckDNS, Cloudflare, Tailscale).
4. Auth always on (`PROXY_USER` / `PROXY_PASSWORD`). Optionally `PROXY_ALLOW` = Render’s outbound IP.

Tailscale/WireGuard is safer than opening 8888 to the whole internet: install Tailscale on the home box and on a sidecar is harder on Render, so port-forward + password is the usual path.

## Run locally (this repo)

From the **repo root**:

```bash
python -m proxy --user zoyra --password 'a-long-secret'
```

Public bind (home host that Render will call):

```bash
python -m proxy --host 0.0.0.0 --http-port 8888 --socks-port 1080 \
  --user zoyra --password 'a-long-secret'
```

Public bind **without** user+password exits with an error.

## Docker (on the proxy machine)

From `proxy/`:

```bash
echo 'PROXY_PASSWORD=a-long-secret' > .env
echo 'PROXY_USER=zoyra' >> .env
docker compose up -d --build
```

## Point this backend at it

On the **API** host (Render env or this repo `.env`), not on the proxy box:

```
YT_PROXY=http://zoyra:a-long-secret@YOUR_HOME_PUBLIC_IP:8888
```

or:

```
YT_PROXY=socks5://zoyra:a-long-secret@YOUR_HOME_PUBLIC_IP:1080
```

Restart the API. Search/stream/download then leave through the proxy IP.

## Check

On the proxy machine:

```bash
curl -x http://zoyra:a-long-secret@127.0.0.1:8888 https://api.ipify.org
```

From another network (phone hotspot):

```bash
curl -x http://zoyra:a-long-secret@YOUR_HOME_PUBLIC_IP:8888 https://api.ipify.org
```

That IP should be the **home** IP. Then `GET /api/search?q=test` on the API.

## Flags / env

`PROXY_HOST`, `PROXY_HTTP_PORT` (or `PORT`), `PROXY_SOCKS_PORT`, `PROXY_USER`, `PROXY_PASSWORD`, `PROXY_ALLOW`.

`--no-socks` disables the SOCKS5 listener.
