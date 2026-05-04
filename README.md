# jellyfin-tvxml

A lightweight Docker service that provides Jellyfin with a live TV channel list (M3U) and programme guide (XMLTV) sourced from DR's schedule API.

- Fetches EPG data directly from DR and serves it as standard XMLTV
- Streams are proxied locally so Jellyfin can reach CDN-hosted HLS streams
- Friendly channel names (e.g. `DR1`) instead of numeric API IDs
- Optional HTTP proxy support for outbound requests

## Endpoints

| Endpoint | Description |
|---|---|
| `GET /epg.xml` | XMLTV programme guide (7 days by default) |
| `GET /epg.xml?days=3` | Override number of days |
| `GET /channels.m3u` | M3U playlist for Jellyfin Live TV tuner |

## Setup

### 1. Configure your streams

Create `streams.json` with the channels you want. The key is the name Jellyfin will see:

```json
{
  "DR1": "https://drlivedr1hls.akamaized.net/hls/live/2113625/drlivedr1/master.m3u8"
}
```

If the key differs from DR's internal channel ID (it will for friendly names), create `channel_ids.json` to map them:

```json
{
  "DR1": "20875"
}
```

This lets the server fetch EPG data using the correct DR API ID while exposing the friendly name to Jellyfin.

### 2. Run with Docker Compose

**Option A — pull the pre-built image from GitHub Container Registry:**

```yaml
services:
  jellyfin-tvxml:
    image: ghcr.io/ha1fdan/jellyfin-tvxml:latest
    container_name: jellyfin-tvxml
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./streams.json:/app/streams.json:ro
      - ./channel_ids.json:/app/channel_ids.json:ro
```

```bash
docker compose up -d
```

**Option B — build locally:**

```bash
git clone https://github.com/ha1fdan/jellyfinTVxml.git
cd jellyfinTVxml
docker compose up -d --build
```

The service listens on port `8765`.

### 3. HTTP proxy (optional)

If your outbound traffic needs to go through an HTTP proxy, copy `.env.example` to `.env` and fill in your proxy URL:

```
HTTP_PROXY=http://user:pass@proxy.host:3128
```

The `compose.yml` loads `.env` automatically if present. You can also set it inline under `environment:` in the compose file.

## Adding to Jellyfin

<!-- screenshot goes here -->

1. In Jellyfin, go to **Dashboard > Live TV**
2. Add a **TV Tuner** — choose **M3U Tuner** and set the URL to:
   ```
   http://<host-ip>:8765/channels.m3u
   ```
3. Add a **TV Guide Data Provider** — choose **XMLTV** and set the URL to:
   ```
   http://<host-ip>:8765/epg.xml
   ```
4. Save and let Jellyfin refresh the guide.
