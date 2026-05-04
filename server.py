#!/usr/bin/env python3
"""
Jellyfin XMLTV + M3U EPG/stream server for DR channels.

Endpoints:
  GET /epg.xml              — XMLTV guide (today + next 7 days)
  GET /epg.xml?days=3       — override number of days
  GET /epg.xml?date=YYYY-MM-DD — single specific date

  GET /channels.m3u         — M3U playlist for Jellyfin Live TV tuner
  GET /proxy?url=<encoded>  — HLS proxy (rewrites relative URLs so Jellyfin
                              can follow the full playlist chain)

Configure stream URLs in streams.json (stream_key -> HLS master URL).
Optionally map friendly stream keys to DR API channel IDs in channel_ids.json.
"""

import concurrent.futures
import http.server
import io
import json
import logging
import os
import socket
import ssl
import urllib.parse
import urllib.request
from datetime import date, timedelta
from xml.etree import ElementTree as ET

# Force IPv4 — Docker bridge networks often lack IPv6 routes.
_real_getaddrinfo = socket.getaddrinfo
def _ipv4_getaddrinfo(host, port, family=0, *args, **kwargs):
    return _real_getaddrinfo(host, port, socket.AF_INET, *args, **kwargs)
socket.getaddrinfo = _ipv4_getaddrinfo

# Global socket timeout covers DNS + SSL handshake (urlopen timeout does not).
socket.setdefaulttimeout(10)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

HOST = "0.0.0.0"
PORT = 8765
PROXY_IMAGES = os.environ.get("PROXY_IMAGES", "").lower() in ("1", "true", "yes")
PROXY_STREAMS = os.environ.get("PROXY_STREAMS", "").lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Channel list (all IDs from the original DR schedule URLs)
# ---------------------------------------------------------------------------
ALL_CHANNELS = [
    "20875", "20876", "20892", "21546", "22221", "22463", "192099",
    "20966", "21303", "21544", "21904", "22146", "22155", "22191",
    "22410", "204135", "204156", "293074",
    "21006", "21135", "21463", "21477", "21511", "21755", "22006",
    "22341", "213361", "213499", "213683", "213878",
    "237449",
    "21297", "21302", "21399", "21468", "21514", "21593", "21652",
    "21885", "21980", "22037", "299482", "513827",
    "21642", "21658", "21717", "22236", "233818",
    "21776", "21788", "21858", "21873", "22302", "22315", "215052", "274815",
    "21752", "21837",
    "213448", "299558",
    "21355", "21677", "22113", "22210", "22279", "213443", "299533",
]

BASE_URL = "https://prod95-cdn.dr-massive.com/api/schedules"
COMMON_PARAMS = {
    "device": "web_browser",
    "duration": "24",
    "ff": "idp,ldp,rpt",
    "geoLocation": "dk",
    "hour": "22",
    "isDeviceAbroad": "false",
    "lang": "da",
    "segments": "drtv,optedin",
    "sub": "Registered",
}

# ---------------------------------------------------------------------------
# Stream URL config  (loaded from streams.json next to this file)
# ---------------------------------------------------------------------------
_STREAMS_FILE = os.path.join(os.path.dirname(__file__), "streams.json")
_CHANNEL_IDS_FILE = os.path.join(os.path.dirname(__file__), "channel_ids.json")
_LOGOS_FILE = os.path.join(os.path.dirname(__file__), "logos.json")


def load_stream_urls() -> dict[str, str]:
    """Return {stream_key: hls_master_url} from streams.json, or {} if missing."""
    if not os.path.exists(_STREAMS_FILE):
        return {}
    with open(_STREAMS_FILE) as f:
        return json.load(f)


def load_logos() -> dict[str, str]:
    """Return {stream_key: logo_url} from logos.json, or {} if missing."""
    if not os.path.exists(_LOGOS_FILE):
        return {}
    with open(_LOGOS_FILE) as f:
        return json.load(f)


def load_channel_id_map() -> dict[str, str]:
    """Return {stream_key: dr_channel_id} from channel_ids.json, or {} if missing.

    When a stream key (e.g. 'DR1') differs from the DR API channel ID (e.g. '20875'),
    this map lets the server fetch EPG by the API ID while using the friendly key
    as the tvg-id in M3U and channel id in XMLTV, so Jellyfin can match them.
    """
    if not os.path.exists(_CHANNEL_IDS_FILE):
        return {}
    with open(_CHANNEL_IDS_FILE) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# EPG helpers
# ---------------------------------------------------------------------------

def fetch_schedules_for_date(target_date: str) -> list[dict]:
    params = dict(COMMON_PARAMS)
    params["channels"] = ",".join(ALL_CHANNELS)
    params["date"] = target_date
    url = BASE_URL + "?" + urllib.parse.urlencode(params)
    log.info("Fetching schedules %s", url)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with _opener.open(req, timeout=30) as resp:
        return json.load(resp)


def xmltv_timestamp(iso_str: str) -> str:
    """Convert '2026-05-03T23:45:00+02:00' → '20260503234500 +0200'."""
    dt_part, _, offset_part = iso_str.partition("+")
    if not offset_part:
        dt_part, _, offset_part = iso_str.partition("-")
        sign = "-"
    else:
        sign = "+"
    dt_clean = dt_part.replace("T", "").replace(":", "").replace("-", "")
    offset_clean = offset_part.replace(":", "")
    return f"{dt_clean} {sign}{offset_clean}"


def _proxy_url(base_url: str, url: str) -> str:
    """Return url routed through the local /proxy endpoint."""
    return base_url + "/proxy?url=" + urllib.parse.quote(url, safe="")


def build_xmltv(channel_data_by_day: list[list[dict]], dr_to_key: dict[str, str] | None = None, logos: dict[str, str] | None = None, base_url: str = "") -> bytes:
    """Build XMLTV XML.

    dr_to_key: optional {dr_api_channel_id -> stream_key} rename map so that
    channel ids in the output match the tvg-id values in the M3U playlist.
    logos: optional {stream_key -> logo_url} for channel icon elements.
    base_url: if set, logo and programme image URLs are routed through /proxy.
    """
    if dr_to_key is None:
        dr_to_key = {}
    if logos is None:
        logos = {}
    channels: dict[str, str] = {}  # (renamed) channelId -> display name
    programmes: list[dict] = []

    for day_data in channel_data_by_day:
        for channel_block in day_data:
            raw_id = channel_block["channelId"]
            channel_id = dr_to_key.get(raw_id, raw_id)
            for sched in channel_block.get("schedules", []):
                item = sched.get("item", {})
                if channel_id not in channels:
                    name = item.get("broadcastChannel") or item.get(
                        "customFields", {}
                    ).get("BroadcastChannel", channel_id)
                    channels[channel_id] = name
                programmes.append(
                    {
                        "channel_id": channel_id,
                        "start": sched.get("startTimeInDefaultTimeZone", ""),
                        "stop": sched.get("endTimeInDefaultTimeZone", ""),
                        "title": item.get("title", ""),
                        "desc": item.get("description") or item.get("shortDescription", ""),
                        "icon": item.get("images", {}).get("wallpaper", ""),
                        "season": item.get("seasonNumber"),
                        "episode": item.get("episodeNumber"),
                        "keywords": item.get("keywords", []),
                        "live": sched.get("live", False),
                    }
                )

    tv = ET.Element("tv", attrib={"generator-info-name": "dr-tvxml"})

    for cid, cname in sorted(channels.items()):
        ch_el = ET.SubElement(tv, "channel", id=cid)
        dn = ET.SubElement(ch_el, "display-name")
        dn.text = cname
        if cid in logos:
            icon_src = _proxy_url(base_url, logos[cid]) if (base_url and PROXY_IMAGES) else logos[cid]
            ET.SubElement(ch_el, "icon", src=icon_src)

    for prog in programmes:
        start_iso, stop_iso = prog["start"], prog["stop"]
        if not start_iso or not stop_iso:
            continue
        try:
            start_xmltv = xmltv_timestamp(start_iso)
            stop_xmltv = xmltv_timestamp(stop_iso)
        except Exception:
            continue

        p = ET.SubElement(tv, "programme", attrib={
            "start": start_xmltv, "stop": stop_xmltv, "channel": prog["channel_id"],
        })
        ET.SubElement(p, "title", lang="da").text = prog["title"]
        if prog["desc"]:
            ET.SubElement(p, "desc", lang="da").text = prog["desc"]
        if prog["icon"]:
            icon_src = _proxy_url(base_url, prog["icon"]) if (base_url and PROXY_IMAGES) else prog["icon"]
            ET.SubElement(p, "icon", src=icon_src)

        season, episode = prog["season"], prog["episode"]
        if season is not None and episode is not None:
            ET.SubElement(p, "episode-num", system="xmltv_ns").text = f"{season-1}.{episode-1}."
            ET.SubElement(p, "episode-num", system="onscreen").text = f"S{season:02d}E{episode:02d}"
        elif episode is not None:
            ET.SubElement(p, "episode-num", system="onscreen").text = f"E{episode:02d}"

        for kw in prog.get("keywords", []):
            if "_" in kw:
                ET.SubElement(p, "category", lang="da").text = kw.split("_", 1)[1].replace("_", " ")

        if prog["live"]:
            ET.SubElement(p, "live")

    ET.indent(tv, space="  ")
    buf = io.BytesIO()
    ET.ElementTree(tv).write(buf, encoding="utf-8", xml_declaration=True)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# M3U helpers
# ---------------------------------------------------------------------------

def build_m3u(stream_urls: dict[str, str], channel_names: dict[str, str], base_url: str, logos: dict[str, str] | None = None) -> bytes:
    """
    Build an M3U playlist where each stream URL goes through the local proxy.
    channel_names: {channel_id -> display name}  (populated from EPG data)
    base_url: e.g. 'http://192.168.1.10:8765'
    logos: optional {channel_id -> logo_url}
    """
    if logos is None:
        logos = {}
    lines = ["#EXTM3U"]
    for channel_id, hls_url in sorted(stream_urls.items()):
        name = channel_names.get(channel_id, channel_id)
        stream_src = base_url + "/proxy?url=" + urllib.parse.quote(hls_url, safe="") if PROXY_STREAMS else hls_url
        if channel_id in logos:
            logo_src = _proxy_url(base_url, logos[channel_id]) if PROXY_IMAGES else logos[channel_id]
            logo_attr = f' tvg-logo="{logo_src}"'
        else:
            logo_attr = ""
        lines.append(
            f'#EXTINF:-1 tvg-id="{channel_id}" tvg-name="{name}"'
            f'{logo_attr} group-title="DR",'
            f'{name}'
        )
        lines.append(stream_src)
    return ("\n".join(lines) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# HLS proxy helpers
# ---------------------------------------------------------------------------

_PROXY_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "*/*",
    "Origin": "https://www.dr.dk",
    "Referer": "https://www.dr.dk/",
}

_SSL_CTX = ssl.create_default_context()
_FETCH_TIMEOUT = 10  # seconds — hard deadline including DNS + SSL + read


def _build_opener() -> urllib.request.OpenerDirector:
    """Build a urllib opener, optionally routing through an HTTP proxy.

    Reads HTTP_PROXY (or HTTPS_PROXY) from the environment. The URL may embed
    credentials:  http://user:pass@proxy.host:3128
    """
    proxy_url = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
    handlers: list = [urllib.request.HTTPSHandler(context=_SSL_CTX)]
    if proxy_url:
        parsed = urllib.parse.urlparse(proxy_url)
        handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
        if parsed.username:
            mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            mgr.add_password(None, proxy_url, parsed.username, parsed.password or "")
            handlers.append(urllib.request.ProxyBasicAuthHandler(mgr))
        log.info("Outbound requests routed via proxy: %s://%s:%s",
                 parsed.scheme, parsed.hostname, parsed.port)
    return urllib.request.build_opener(*handlers)


_opener = _build_opener()


def _do_fetch(url: str) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers=_PROXY_HEADERS)
    with _opener.open(req, timeout=_FETCH_TIMEOUT) as resp:
        ct = resp.headers.get("Content-Type", "application/octet-stream")
        # Read up to 4 MB — playlists are tiny; this prevents hanging on a
        # streaming/chunked response that never sends EOF.
        return resp.read(4 * 1024 * 1024), ct


def fetch_upstream(url: str) -> tuple[bytes, str]:
    """Fetch a URL with a hard thread-level timeout."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(_do_fetch, url)
        try:
            return future.result(timeout=_FETCH_TIMEOUT + 2)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"upstream fetch timed out: {url}")


def rewrite_m3u8(content: bytes, upstream_url: str, proxy_base: str) -> bytes:
    """
    Rewrite an M3U8 playlist so that:
    - Variant stream lines (after #EXT-X-STREAM-INF) → proxied through /proxy
    - Media segment lines (.ts, .aac, .mp4, .m4s, etc.) → absolute upstream URLs
      (Jellyfin fetches segments directly, no need to proxy each one)
    - Other relative URLs → resolved to absolute upstream
    """
    base = upstream_url.rsplit("/", 1)[0] + "/"
    out_lines = []
    lines = content.decode("utf-8").splitlines()
    next_is_variant = False

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("#EXT-X-STREAM-INF"):
            next_is_variant = True
            out_lines.append(line)
            continue

        if stripped.startswith("#"):
            next_is_variant = False
            out_lines.append(line)
            continue

        if not stripped:
            out_lines.append(line)
            continue

        # Resolve relative URL to absolute
        if stripped.startswith("http://") or stripped.startswith("https://"):
            abs_url = stripped
        else:
            abs_url = base + stripped

        if next_is_variant:
            # Variant playlist → proxy it so we can rewrite its segments too
            proxied = proxy_base + "/proxy?url=" + urllib.parse.quote(abs_url, safe="")
            out_lines.append(proxied)
            next_is_variant = False
        else:
            # Media segment → absolute upstream URL (direct fetch by client)
            out_lines.append(abs_url)

    return "\n".join(out_lines).encode("utf-8")


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

# Simple in-process cache: {date_str -> list[dict]}  (schedules data)
_epg_cache: dict[str, list[dict]] = {}
# Channel name cache populated after first EPG fetch
_channel_names: dict[str, str] = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info(fmt, *args)

    def _base_url(self) -> str:
        host_header = self.headers.get("Host", f"{HOST}:{PORT}")
        return f"http://{host_header}"

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/epg.xml":
            self._serve_epg(query)
        elif path == "/channels.m3u":
            self._serve_m3u()
        elif path == "/proxy":
            self._serve_proxy(query)
        else:
            self.send_error(404, "Unknown endpoint")

    # -- EPG ------------------------------------------------------------------

    def _serve_epg(self, query):
        if "date" in query:
            dates = [query["date"][0]]
        else:
            days = int(query.get("days", ["7"])[0])
            today = date.today()
            # Start from yesterday: the DR API returns 24h from hour=22, so
            # yesterday's window (22:00 yesterday → 22:00 today) covers today's
            # daytime programmes that would otherwise be missing.
            dates = [(today + timedelta(days=i)).isoformat() for i in range(-1, days)]

        log.info("EPG request for dates: %s", dates)
        try:
            channel_id_map = load_channel_id_map()
            # Reverse: {dr_api_id -> stream_key} for renaming channel IDs in output
            dr_to_key = {v: k for k, v in channel_id_map.items()}

            uncached = [d for d in dates if d not in _epg_cache]
            if uncached:
                with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(uncached), 1)) as ex:
                    for d, result in zip(uncached, ex.map(fetch_schedules_for_date, uncached)):
                        _epg_cache[d] = result
                        # Populate channel name cache (keyed by stream key, not DR API ID)
                        for block in result:
                            raw_cid = block["channelId"]
                            cid = dr_to_key.get(raw_cid, raw_cid)
                            if cid not in _channel_names:
                                for sched in block.get("schedules", []):
                                    item = sched.get("item", {})
                                    name = item.get("broadcastChannel") or item.get(
                                        "customFields", {}
                                    ).get("BroadcastChannel")
                                    if name:
                                        _channel_names[cid] = name
                                        break

            results = [_epg_cache[d] for d in dates]
            xml_bytes = build_xmltv(results, dr_to_key, load_logos(), self._base_url())
        except Exception as exc:
            log.exception("Error building EPG")
            self.send_error(500, str(exc))
            return

        self._respond(200, "application/xml; charset=utf-8", xml_bytes)

    # -- M3U ------------------------------------------------------------------

    def _serve_m3u(self):
        stream_urls = load_stream_urls()
        if not stream_urls:
            self.send_error(404, "No streams configured — create streams.json")
            return
        m3u = build_m3u(stream_urls, _channel_names, self._base_url(), load_logos())
        self._respond(200, "application/x-mpegurl; charset=utf-8", m3u)

    # -- HLS proxy ------------------------------------------------------------

    def _serve_proxy(self, query):
        url_list = query.get("url")
        if not url_list:
            self.send_error(400, "Missing ?url= parameter")
            return
        upstream_url = url_list[0]
        log.info("Proxying %s", upstream_url)

        try:
            body, content_type = fetch_upstream(upstream_url)
        except Exception as exc:
            log.exception("Upstream fetch failed: %s", upstream_url)
            self.send_error(502, str(exc))
            return

        # Rewrite M3U8 playlists; pass everything else through unchanged
        if "mpegurl" in content_type.lower() or upstream_url.endswith(".m3u8"):
            content_type = "application/vnd.apple.mpegurl"
            body = rewrite_m3u8(body, upstream_url, self._base_url())

        self._respond(200, content_type, body)

    # -- helpers --------------------------------------------------------------

    def _respond(self, code: int, content_type: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionResetError, BrokenPipeError):
            pass  # client disconnected mid-transfer — not an error


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("Server listening on http://%s:%d", HOST, PORT)
    log.info("  EPG:      http://%s:%d/epg.xml", HOST, PORT)
    log.info("  Playlist: http://%s:%d/channels.m3u", HOST, PORT)
    log.info("  Proxy:    http://%s:%d/proxy?url=<encoded_hls_url>", HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
