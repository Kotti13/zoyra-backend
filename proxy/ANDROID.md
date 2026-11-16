# Android (Termux) — no extra computer

Use the phone as the proxy so YouTube sees a **mobile** IP. The API on Render cannot dial your phone directly (carrier CGNAT), so the phone must open an **outbound TCP tunnel**.

**Tailscale** only works if Render is also on your tailnet (it isn’t on a normal Web Service). Skip Tailscale for this setup.

You need:

- Android
- Termux from **F-Droid** (Play Store Termux is outdated)
- A TCP tunnel: **Playit.gg** (free) or **ngrok TCP** (paid plan)

Bind the proxy to `127.0.0.1`. Only the tunnel should be public.

## 1. Termux

```bash
pkg update && pkg upgrade
pkg install python git
termux-wake-lock
```

Android Settings → Apps → Termux → Battery → **Unrestricted**.

## 2. This proxy

```bash
git clone https://github.com/Kotti13/proxy-server.git
cd proxy-server
python -m proxy --host 127.0.0.1 --http-port 8888 --no-socks \
  --user zoyra --password 'a-long-secret'
```

Leave that session running. If the repo is not on GitHub, copy only the `proxy/` folder into Termux storage and from the **parent** of `proxy/`:

```bash
python -m proxy --host 127.0.0.1 --http-port 8888 --no-socks \
  --user zoyra --password 'a-long-secret'
```

## 3. Public TCP tunnel (new Termux session)

Swipe from the left in Termux → **New session**.

### Playit.gg (free TCP)

```bash
pkg install wget
# install the Playit agent from https://playit.gg/download (Linux / Android instructions)
# create a TCP tunnel to 127.0.0.1:8888
```

Playit shows a hostname and port, e.g. `et.playit.gg:12345`.

### ngrok TCP (needs a plan that includes TCP)

```bash
pkg install wget unzip
# install ngrok, then:
ngrok config add-authtoken YOUR_TOKEN
ngrok tcp 8888
```

ngrok shows `tcp://0.tcp.ngrok.io:XXXXX`.

Do **not** use `ngrok http` — HTTP `CONNECT` (HTTPS sites, YouTube) will break.

## 4. Render / API `.env`

```
YT_PROXY=http://zoyra:a-long-secret@et.playit.gg:12345
```

or:

```
YT_PROXY=http://zoyra:a-long-secret@0.tcp.ngrok.io:XXXXX
```

Restart the API. Phone must stay awake; Termux + tunnel + proxy must keep running.

## Check

From any computer:

```bash
curl -x http://zoyra:a-long-secret@TUNNEL_HOST:PORT https://api.ipify.org
```

That should be a **mobile-carrier** IP.

## Limits

- Screen off / battery optimization will kill Termux unless unrestricted + `termux-wake-lock`.
- Each song’s audio goes through the phone (~few MB).
- Airplane mode or Wi‑Fi/cell switch drops the tunnel; start it again and update `YT_PROXY` if the port changed.
- Playit/ngrok hostnames can change on free tiers — then update Render’s `YT_PROXY`.
