import re
import sys
import json
import hashlib
import unicodedata
from copy import deepcopy
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from html import unescape
from ipaddress import ip_address
from urllib.parse import urljoin, urlparse, urlunparse

START_URL = "https://hoadaotv.info/"

OUT_M3U = ""
OUT_JSON = ""
BROWSER_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
M3U_USER_AGENT = "Mozilla/5.0+(Windows+NT+11.0;+Win64;+x64)+AppleWebKit/537.36+(KHTML,+like+Gecko)+Chrome/113.0.0.0+Safari/537.36+Edg/113.0.1774.42"

TIMEOUT = 12
MAX_MATCHES = 80  # đủ dùng, mày tăng/giảm tùy
VN_TZ = timezone(timedelta(hours=7))
DEFAULT_MATCH_DURATION = timedelta(hours=2)
DEFAULT_IMAGE_URL = ""
JSON_DEFAULT_IMAGE_URL = "https://hailab.cloud/kodi/sport/default.png"
BLOCKED_HOSTS = {"localhost", "localhost.localdomain"}

STATUS_META = {
    "live": {
        "text": "● Live",
        "group": "🔴 Live",
        "color": "#FF0000",
    },
    "upcoming": {
        "text": "Chưa diễn ra",
        "group": "⏳ Chưa diễn ra",
        "color": "#F59E0B",
    },
    "finished": {
        "text": "Đã kết thúc",
        "group": "⚪ Đã kết thúc",
        "color": "#6B7280",
    },
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Bắt stream URL tuyệt đối trong HTML/inline JS.
STREAM_URL_RE = re.compile(r'https?:[/\\]{2}[^\s"\'<>]+?\.(?:m3u8|flv)(?:\?[^\s"\'<>]+)?', re.IGNORECASE)
SCRIPT_IMPORT_RE = re.compile(
    r'(?:from\s*|import\s*\()\s*["\']([^"\']+\.js(?:\?[^"\']*)?)["\']',
    re.IGNORECASE,
)
MAX_DISCOVERY_SCRIPTS = 24

# Bắt giờ kiểu 09:30, 11:00
TIME_RE = re.compile(r"\b(\d{1,2}:\d{2})\b")

# Link trận thường có dạng:
# - /team-a-vs-team-b-1234567
# - /truc-tiep/team-a-vs-team-b-1900-20-04-2026/1234567
MATCH_PATH_RE = re.compile(
    r"^/(?:[^/?#]+-vs-[^/?#]+-\d+|truc-tiep/[^/?#]+-vs-[^/?#]+(?:-\d{3,4}-\d{2}-\d{2}-\d{4})?/\d+)/?$",
    re.IGNORECASE,
)

def pick_text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""

def stable_id(prefix: str, text: str, n: int = 10) -> str:
    h = hashlib.md5(text.encode("utf-8", errors="ignore")).hexdigest()[:n]
    return f"{prefix}-{h}"

def normalize_source_url(source_url: str = START_URL) -> str:
    raw = (source_url or START_URL).strip()
    if not raw:
        raw = START_URL
    if "://" not in raw:
        raw = "https://" + raw

    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Link chỉ hỗ trợ http hoặc https")
    if not parsed.netloc:
        raise ValueError("Link không hợp lệ")
    if parsed.username or parsed.password:
        raise ValueError("Link không được chứa username/password")

    host = (parsed.hostname or "").strip().lower()
    if not host or host in BLOCKED_HOSTS or host.endswith(".local"):
        raise ValueError("Host không được phép crawl")

    try:
        ip = ip_address(host.strip("[]"))
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError("IP nội bộ không được phép crawl")
    except ValueError as e:
        if "không được phép" in str(e):
            raise

    path = parsed.path or "/"
    return urlunparse((parsed.scheme.lower(), parsed.netloc, path, "", parsed.query, ""))

def extract_match_links(home_html: str, source_url: str = START_URL) -> list[str]:
    soup = BeautifulSoup(home_html, "html.parser")
    hrefs = [a.get("href", "") for a in soup.select("a[href]")]

    # Fallback nếu HTML đổi nhẹ hoặc parser bỏ sót attribute nào đó.
    hrefs.extend(re.findall(r'href=["\']([^"\']+-vs-[^"\']+-\d+/?)[^"\']*["\']', home_html, flags=re.I))

    seen = set()
    out = []
    for href in hrefs:
        if not href:
            continue
        u = urljoin(source_url, href)
        parsed = urlparse(u)
        if not MATCH_PATH_RE.match(parsed.path):
            continue

        try:
            clean_url = normalize_source_url(f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}")
        except ValueError:
            continue
        key = clean_url.lower()
        if key not in seen:
            seen.add(key)
            out.append(clean_url)
    return out

def extract_title_like(soup: BeautifulSoup) -> str:
    """
    Ưu tiên og:title -> title -> h1/h2/h3 đầu tiên
    """
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return og["content"].strip()

    t = soup.title.get_text(" ", strip=True) if soup.title else ""
    if t:
        return t.strip()

    for sel in ["h1", "h2", "h3"]:
        h = soup.select_one(sel)
        if h:
            tx = pick_text(h)
            if tx:
                return tx
    return ""

def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()

def site_root_url(value: str) -> str:
    parsed = urlparse(value or "")
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}/"

def content_attr(el) -> str:
    return clean_text(el.get("content", "")) if el else ""

def find_meta_content(soup: BeautifulSoup, selectors: list[dict]) -> str:
    for attrs in selectors:
        value = content_attr(soup.find("meta", attrs=attrs))
        if value:
            return value
    return ""

def absolute_url(value: str, source_url: str) -> str:
    value = clean_text(value)
    if not value:
        return ""
    return urljoin(source_url, value)

def decoded_response_text(response: requests.Response) -> str:
    if not response.encoding or response.encoding.lower() in {"iso-8859-1", "latin-1"}:
        response.encoding = "utf-8"
    return response.text

def slugify(value: str, fallback: str = "crawl", max_length: int = 70) -> str:
    text = clean_text(value)
    if not text:
        text = fallback
    text = text.replace("Đ", "D").replace("đ", "d")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if not text:
        text = fallback
    return text[:max_length].strip("-") or fallback

def compact_site_name(value: str, source_url: str) -> str:
    text = clean_text(value)
    if not text:
        return urlparse(source_url).netloc or "crawl"

    # SEO titles often look like "Brand - long offer" or "Brand | long offer".
    text = re.split(r"\s+[|–—]\s+|\s+-\s+", text, maxsplit=1)[0].strip()
    text = re.sub(r"\s*\([^)]*\)\s*", " ", text).strip()
    return clean_text(text) or urlparse(source_url).netloc or "crawl"

def looks_like_domain(value: str) -> bool:
    text = clean_text(value).lower()
    return bool(re.fullmatch(r"(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+", text))

def clean_team_name(value: str) -> str:
    text = clean_text(value).strip(" -–|")
    text = re.sub(r"\s+\d{1,2}:\d{2}\s+(?:ngày\s+)?\d{1,2}/\d{1,2}.*$", "", text, flags=re.I)
    text = re.sub(r"\s+\d{3,4}\s*-\s*\d{1,2}\s*-\s*\d{1,2}\s*-\s*\d{4}.*$", "", text, flags=re.I)
    text = re.sub(r"\s*-\s*(Trực tiếp|Live).*?$", "", text, flags=re.I)
    return clean_text(text).strip(" -–|")

def split_teams_from_title(title: str) -> tuple[str, str]:
    """
    Cố gắng tách team A / team B từ title kiểu:
    - "A vs B"
    - "A - B"
    - "A v B"
    - "A VS B"
    """
    if not title:
        return "", ""

    t = re.sub(r"\s+", " ", title).strip()

    # Loại bớt phần thừa hay gặp
    t = re.sub(r"\s*\|\s*.*$", "", t)   # cắt sau dấu |
    t = re.sub(r"^(?:Xem\s+)?Trực\s+Tiếp\s+", "", t, flags=re.I)
    t = re.sub(r"\s*-\s*(Trực tiếp|Live).*?$", "", t, flags=re.I)
    t = re.sub(r"\s*-\s*[^-]{2,30}$", "", t)  # bỏ tên BLV ở cuối title

    # Các dấu phân cách hay dùng
    seps = [r"\s+vs\s+", r"\s+v\s+", r"\s+-\s+", r"\s+–\s+"]
    for sep in seps:
        parts = re.split(sep, t, maxsplit=1, flags=re.I)
        if len(parts) == 2:
            a = clean_team_name(parts[0])
            b = clean_team_name(parts[1])
            # chặn trường hợp tách bậy quá ngắn
            if len(a) >= 2 and len(b) >= 2:
                return a, b

    return "", ""

def parse_datetime_to_vn(value, naive_tz: timezone = VN_TZ) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp = timestamp / 1000
            return datetime.fromtimestamp(timestamp, VN_TZ)

        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=naive_tz).astimezone(VN_TZ)
        return dt.astimezone(VN_TZ)
    except (TypeError, ValueError, OSError):
        return None

def parse_vietnam_datetime(value: str) -> datetime | None:
    return parse_datetime_to_vn(value, VN_TZ)

def parse_vietnam_time_from_iso(value: str) -> str:
    dt = parse_vietnam_datetime(value)
    return dt.strftime("%H:%M") if dt else ""

def format_datetime(dt: datetime | None) -> str:
    return dt.isoformat() if dt else ""

def extract_image_url(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return (value.get("url") or value.get("contentUrl") or "").strip()
    if isinstance(value, list):
        for item in value:
            url = extract_image_url(item)
            if url:
                return url
    return ""

def status_from_times(start_at: datetime | None, end_at: datetime | None) -> str:
    if not start_at:
        return "live"

    now = datetime.now(VN_TZ)
    if now < start_at:
        return "upcoming"

    effective_end = end_at or (start_at + DEFAULT_MATCH_DURATION)
    if now > effective_end:
        return "finished"

    return "live"

def status_text(status: str) -> str:
    return STATUS_META.get(status, STATUS_META["live"])["text"]

def team_payload(name: str, image_url: str, side: str, default_image_url: str = DEFAULT_IMAGE_URL) -> dict:
    return {
        "side": side,
        "name": name,
        "image": {
            "url": image_url or default_image_url,
            "display": "cover",
            "shape": "square",
        }
    }

def image_payload(url: str, default_image_url: str = DEFAULT_IMAGE_URL) -> dict:
    return {
        "url": url or default_image_url or JSON_DEFAULT_IMAGE_URL,
        "type": "cover",
        "width": 640,
        "height": 640,
    }

def stream_image_payload(url: str, default_image_url: str = DEFAULT_IMAGE_URL) -> dict:
    return {
        "url": url or default_image_url or JSON_DEFAULT_IMAGE_URL,
        "type": "contain",
    }

def description_from_info(info: dict) -> str:
    parts = [info.get("status_text", "")]
    when = " ".join(p for p in [info.get("date", ""), info.get("time", "")] if p)
    if when:
        parts.append(when)
    return " - ".join(p for p in parts if p)

def unique_stream_urls(html: str) -> list[str]:
    out = []
    seen = set()
    for url in STREAM_URL_RE.findall(html):
        clean_url = unescape(url).replace("\\/", "/").replace("\\", "/")
        if clean_url not in seen:
            seen.add(clean_url)
            out.append(clean_url)
    return out

def stream_link_type(url: str) -> str:
    path = urlparse(url).path.lower()
    if path.endswith(".flv"):
        return "flv"
    return "hls"

def stream_link_name(url: str, index: int) -> str:
    return f"Link {index}"

def build_stream_links(stream_urls: list[str], channel_id: str, match_url: str, source_url: str = "") -> list[dict]:
    links = []
    for idx, url in enumerate(stream_urls, 1):
        links.append({
            "id": f"{channel_id}-s{idx}",
            "name": stream_link_name(url, idx),
            "url": url,
            "type": stream_link_type(url),
            "default": idx == 1,
            "subtitles": None,
            "remote_data": None,
            "request_headers": None,
        })
    return links

def json_status_label(info: dict) -> dict:
    status = info.get("status", "live")
    if status == "upcoming":
        text = "Chưa diễn ra"
        color = "#f59e0b"
    elif status == "finished":
        text = "Đã kết thúc"
        color = "#6B7280"
    else:
        text = "Trực Tiếp"
        color = "#f70525"
    return {
        "text": text,
        "position": "top-left",
        "color": color,
        "text_color": "#ffffff",
    }

def vietnam_date_text(value: str) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
        return dt.strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return value

def json_channel_name(info: dict) -> str:
    base_name = build_channel_name(info)
    league = clean_text(info.get("league", ""))
    if league and league.lower() not in base_name.lower():
        base_name = f"[{league}] {base_name}"

    time_text = clean_text(info.get("time", ""))
    date_text = vietnam_date_text(info.get("date", ""))
    if time_text and date_text:
        return f"{base_name} lúc {time_text} ngày {date_text}"
    if time_text:
        return f"{base_name} lúc {time_text}"
    return base_name

def channel_json_id(name: str, match_url: str) -> str:
    slug = slugify(name, fallback="channel", max_length=86)
    suffix = hashlib.md5(match_url.encode("utf-8", errors="ignore")).hexdigest()[:6]
    return f"{slug}-{suffix}"

def build_channel(
    info: dict,
    match_url: str,
    stream_urls: list[str],
    group: str,
    source_name: str,
    default_image_url: str = DEFAULT_IMAGE_URL,
    source_url: str = "",
) -> dict:
    display_name = json_channel_name(info)
    ch_id = channel_json_id(display_name, match_url)
    source_id = f"{ch_id}-src1"
    content_id = f"{ch_id}-c1"
    stream_id = f"{ch_id}-ep1"
    stream_links = build_stream_links(stream_urls, ch_id, match_url, source_url)
    image_url = info.get("team_a_image") or info.get("team_b_image") or default_image_url

    return {
        "id": ch_id,
        "name": display_name,
        "description": display_name,
        "label": json_status_label(info),
        "image": image_payload(image_url, default_image_url),
        "grid_number": 1,
        "display": "text-below",
        "type": "single",
        "enable_detail": True,
        "sources": [
            {
                "id": source_id,
                "name": "Source 1",
                "image": None,
                "contents": [
                    {
                        "id": content_id,
                        "name": "Content 1",
                        "image": None,
                        "streams": [
                            {
                                "id": stream_id,
                                "name": "Live",
                                "image": stream_image_payload(image_url, default_image_url),
                                "stream_links": stream_links
                            }
                        ]
                    }
                ],
                "remote_data": None,
            }
        ]
    }

def build_groups(group_channels: dict[str, list[dict]]) -> list[dict]:
    groups = []
    for status in ["live", "upcoming", "finished"]:
        channels = group_channels.get(status, [])
        if not channels:
            continue
        meta = STATUS_META[status]
        groups.append({
            "id": status,
            "name": meta["group"],
            "display": "horizontal",
            "grid_number": 1,
            "image": None,
            "enable_detail": False,
            "channels": channels
        })
    return groups or [
        {
            "id": "live",
            "name": STATUS_META["live"]["group"],
            "display": "horizontal",
            "grid_number": 1,
            "image": None,
            "enable_detail": False,
            "channels": []
        }
    ]

def source_name_from_result(result: dict) -> str:
    stats = result.get("stats") or {}
    source_metadata = stats.get("source_metadata") or {}
    json_data = result.get("json") or {}
    return (
        source_metadata.get("short_name")
        or source_metadata.get("site_name")
        or source_metadata.get("title")
        or json_data.get("name")
        or stats.get("source")
        or "Source"
    )

def source_channels_from_result(result: dict) -> list[dict]:
    channels = []
    for group in (result.get("json") or {}).get("groups", []):
        if not isinstance(group, dict):
            continue
        for channel in group.get("channels", []):
            channels.append(deepcopy(channel))
    return channels

def channels_from_group_channels(group_channels: dict[str, list[dict]]) -> list[dict]:
    channels = []
    for status in ["live", "upcoming", "finished"]:
        channels.extend(group_channels.get(status, []))
    return channels

def source_group_payload(group_id: str, name: str, channels: list[dict]) -> dict:
    return {
        "id": group_id,
        "name": name,
        "display": "horizontal",
        "grid_number": 1,
        "image": None,
        "enable_detail": False,
        "channels": channels,
    }

def build_single_source_groups(
    group_channels: dict[str, list[dict]],
    source_name: str,
    source_metadata: dict | None = None,
) -> list[dict]:
    source_metadata = source_metadata or {}
    base_id = slugify(
        source_metadata.get("output_base")
        or source_metadata.get("id")
        or source_name,
        fallback="source",
        max_length=50,
    )
    return [
        source_group_payload(
            f"{base_id}-group-1",
            source_name,
            channels_from_group_channels(group_channels),
        )
    ]

def build_source_groups(results: list[dict]) -> list[dict]:
    groups = []
    for idx, result in enumerate(results, 1):
        source_name = source_name_from_result(result)
        groups.append(source_group_payload(f"bongda-group-{idx}", source_name, source_channels_from_result(result)))
    return groups or [
        source_group_payload("bongda", "Bóng Đá", [])
    ]

def iter_json_ld_objects(data):
    if isinstance(data, list):
        for item in data:
            yield from iter_json_ld_objects(item)
    elif isinstance(data, dict):
        yield data
        graph = data.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from iter_json_ld_objects(item)

def extract_sports_event(soup: BeautifulSoup) -> dict:
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for obj in iter_json_ld_objects(data):
            event_type = obj.get("@type")
            if event_type == "SportsEvent" or (isinstance(event_type, list) and "SportsEvent" in event_type):
                return obj
    return {}

def extract_source_metadata(home_html: str, source_url: str) -> dict:
    soup = BeautifulSoup(home_html, "html.parser")
    parsed = urlparse(source_url)
    fallback_name = parsed.netloc or "crawl"

    title = find_meta_content(
        soup,
        [
            {"property": "og:title"},
            {"name": "twitter:title"},
        ],
    )
    if not title and soup.title:
        title = clean_text(soup.title.get_text(" ", strip=True))
    if not title:
        title = fallback_name

    description = find_meta_content(
        soup,
        [
            {"name": "description"},
            {"property": "og:description"},
            {"name": "twitter:description"},
        ],
    )

    image_url = find_meta_content(
        soup,
        [
            {"property": "og:image"},
            {"name": "twitter:image"},
            {"property": "og:logo"},
        ],
    )

    site_name = find_meta_content(
        soup,
        [
            {"property": "og:site_name"},
            {"name": "application-name"},
        ],
    )

    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for obj in iter_json_ld_objects(data):
            obj_type = obj.get("@type")
            types = obj_type if isinstance(obj_type, list) else [obj_type]
            if "Organization" in types or "WebSite" in types:
                if not site_name:
                    site_name = clean_text(obj.get("name", ""))
                if not description:
                    description = clean_text(obj.get("description", ""))
                if not image_url:
                    image_url = extract_image_url(obj.get("logo")) or extract_image_url(obj.get("image"))

    if not image_url:
        for link in soup.find_all("link"):
            rel = link.get("rel", "")
            rel_text = " ".join(rel).lower() if isinstance(rel, list) else str(rel).lower()
            if "icon" in rel_text and link.get("href"):
                image_url = link.get("href", "")
                break

    if not image_url:
        image_url = "/favicon.ico"

    title_short_name = compact_site_name(title, source_url)
    site_display_name = compact_site_name(site_name, source_url) if site_name else ""
    short_name = title_short_name if looks_like_domain(site_display_name) else (site_display_name or title_short_name)
    display_name = short_name
    absolute_image_url = absolute_url(image_url, source_url)
    output_base = slugify(short_name, fallback=slugify(fallback_name, fallback="crawl"), max_length=40)

    return {
        "id": output_base,
        "title": title,
        "site_name": display_name,
        "short_name": short_name,
        "description": description,
        "image": absolute_image_url,
        "source_url": source_url,
        "output_base": output_base,
    }

def parse_match_info(match_html: str) -> dict:
    """
    Lấy giờ + team A/B từ title/og:title hoặc heading.
    """
    soup = BeautifulSoup(match_html, "html.parser")
    full_text = soup.get_text("\n", strip=True)
    event = extract_sports_event(soup)
    start_at_dt = parse_vietnam_datetime(event.get("startDate", ""))
    end_at_dt = parse_vietnam_datetime(event.get("endDate", ""))
    status = status_from_times(start_at_dt, end_at_dt)

    # giờ
    if start_at_dt:
        hhmm = start_at_dt.strftime("%H:%M")
        date_text = start_at_dt.strftime("%Y-%m-%d")
    else:
        time_m = TIME_RE.search(full_text)
        hhmm = time_m.group(1) if time_m else ""
        date_text = ""

    title_like = extract_title_like(soup)
    team_a, team_b = "", ""
    team_a_image, team_b_image = "", ""

    competitors = event.get("competitor", [])
    if isinstance(competitors, list) and len(competitors) >= 2:
        home = competitors[0] if isinstance(competitors[0], dict) else {}
        away = competitors[1] if isinstance(competitors[1], dict) else {}
        team_a = home.get("name", "")
        team_b = away.get("name", "")
        team_a_image = extract_image_url(home.get("image"))
        team_b_image = extract_image_url(away.get("image"))

    if not team_a or not team_b:
        team_a, team_b = split_teams_from_title(event.get("name", "") or title_like)

    # fallback: thử tìm trong các heading nếu title_like không tách được
    if not team_a and not team_b:
        headings = [pick_text(h) for h in soup.select("h1,h2,h3") if pick_text(h)]
        for h in headings[:5]:
            a, b = split_teams_from_title(h)
            if a and b:
                team_a, team_b = a, b
                break

    return {
        "time": hhmm.strip(),
        "date": date_text,
        "start_at": format_datetime(start_at_dt),
        "end_at": format_datetime(end_at_dt),
        "status": status,
        "status_text": status_text(status),
        "league": event.get("location", {}).get("name", "") if isinstance(event.get("location"), dict) else "",
        "team_a": team_a.strip(),
        "team_b": team_b.strip(),
        "team_a_image": team_a_image.strip(),
        "team_b_image": team_b_image.strip(),
        "title_like": title_like.strip()
    }

def build_channel_name(info: dict) -> str:
    # Mày muốn ngắn gọn để UI hiện đẹp
    if info["team_a"] and info["team_b"]:
        return f'{info["team_a"]} vs {info["team_b"]}'.strip()
    if info["title_like"]:
        return info["title_like"]
    return "Live"

def guess_request_headers(m3u8_url: str, match_url: str, source_url: str = "") -> list[dict]:
    """
    Một số CDN cần Referer. Nếu mày biết chắc referer nào thì set cứng ở đây.
    Default: dùng chính match_url làm Referer (an toàn hơn để chống 403).
    """
    root = site_root_url(source_url) or site_root_url(match_url)
    return [
        {"key": "User-Agent", "value": M3U_USER_AGENT},
        {"key": "Referer", "value": root or match_url},
        {"key": "Origin", "value": root},
    ]

def create_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": BROWSER_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    return s

def m3u_attr(value: str) -> str:
    return str(value or "").replace('"', "'")

def vlc_option_name(header_key: str) -> str:
    return {
        "User-Agent": "http-user-agent",
        "Referer": "http-referrer",
        "Origin": "http-origin",
    }.get(header_key, f"http-{header_key.lower()}")

def build_m3u_text(m3u_items: list[dict]) -> str:
    m3u_lines = ["#EXTM3U"]
    for it in m3u_items:
        attrs = [f'group-title="{m3u_attr(it["group"])}"']
        if it.get("logo"):
            attrs.append(f'tvg-logo="{m3u_attr(it["logo"])}"')
        m3u_lines.append(f'#EXTINF:-1 {" ".join(attrs)}, {it["name"]}')
        for header in it.get("headers", []):
            key = header.get("key", "")
            value = header.get("value", "")
            if key and value:
                m3u_lines.append(f"#EXTVLCOPT:{vlc_option_name(key)}={value}")
        m3u_lines.append(it["url"])
    return "\n".join(m3u_lines) + "\n"

def m3u_source_separator(source_name: str) -> str:
    name = clean_text(source_name).upper() or "SOURCE"
    return f"#------------------{name}-------------------------"

def m3u_body_lines(m3u_text: str) -> list[str]:
    lines = []
    for line in (m3u_text or "").splitlines():
        clean_line = line.strip()
        if not clean_line or clean_line == "#EXTM3U":
            continue
        lines.append(line)
    return lines

def build_buncha_json(
    group_channels: dict[str, list[dict]],
    source_url: str = START_URL,
    source_metadata: dict | None = None,
    groups: list[dict] | None = None,
) -> dict:
    source_metadata = source_metadata or {}
    name = source_metadata.get("short_name") or source_metadata.get("site_name") or source_metadata.get("title") or urlparse(source_url).netloc or "Crawl"
    description = source_metadata.get("description") or name
    image_url = source_metadata.get("image", "")
    return {
        "id": source_metadata.get("id") or slugify(name),
        "url": source_url,
        "name": name,
        "color": "#1cb57a",
        "description": description,
        "image": {
            "url": image_url
        },
        "groups": groups if groups is not None else build_groups(group_channels),
        "option": {
            "save_history": False,
            "save_search_history": False,
            "save_wishlist": False
        }
    }

def merge_crawled_json(results: list[dict]) -> dict:
    first_image = ""

    for result in results:
        json_data = result.get("json") or {}
        image_url = ((json_data.get("image") or {}).get("url") or "").strip()
        if image_url and not first_image:
            first_image = image_url

    return build_buncha_json(
        {"live": [], "upcoming": [], "finished": []},
        "bongda",
        {
            "id": "bongda",
            "title": "Bóng Đá",
            "site_name": "Bóng Đá",
            "short_name": "Bóng Đá",
            "description": "Danh sách trực tiếp bóng đá",
            "image": first_image,
            "source_url": "bongda",
            "output_base": "bongda",
        },
        groups=build_source_groups(results),
    )

def merge_crawled_m3u(results: list[dict]) -> str:
    lines = ["#EXTM3U"]
    for result in results:
        source_name = source_name_from_result(result)
        body = m3u_body_lines(result.get("m3u", ""))
        if not body:
            continue
        lines.append(m3u_source_separator(source_name))
        lines.extend(body)
    return "\n".join(lines) + "\n"

def merge_crawls(source_urls: list[str], max_matches: int = MAX_MATCHES, logger=None) -> dict:
    results = []
    errors = []
    seen = set()

    for raw_url in source_urls:
        raw_url = clean_text(raw_url)
        if not raw_url:
            continue
        try:
            source_url = normalize_source_url(raw_url)
        except ValueError as e:
            errors.append({"source": raw_url, "error": str(e)})
            continue

        key = source_url.lower()
        if key in seen:
            continue
        seen.add(key)

        try:
            if logger:
                logger(f"Crawl merge: {source_url}")
            results.append(crawl(max_matches=max_matches, source_url=source_url, logger=logger))
        except Exception as e:
            errors.append({"source": source_url, "error": str(e)})

    if not results:
        raise ValueError("Không crawl được nguồn nào để gộp.")

    counts = {"live": 0, "upcoming": 0, "finished": 0}
    total_channels = 0
    total_m3u_items = 0
    sources = []

    for result in results:
        stats = result.get("stats") or {}
        groups = stats.get("groups") or {}
        for status in counts:
            counts[status] += int(groups.get(status) or 0)
        total_channels += int(stats.get("channels") or 0)
        total_m3u_items += int(stats.get("m3u_items") or 0)
        sources.append({
            "source": stats.get("source", ""),
            "source_metadata": stats.get("source_metadata") or {},
            "adapter": stats.get("adapter"),
            "api_url": stats.get("api_url"),
            "matches_found": stats.get("matches_found", 0),
            "channels": stats.get("channels", 0),
            "m3u_items": stats.get("m3u_items", 0),
            "groups": groups,
        })

    return {
        "json": merge_crawled_json(results),
        "m3u": merge_crawled_m3u(results),
        "stats": {
            "source": "bongda",
            "source_metadata": {
                "id": "bongda",
                "title": "Bóng Đá",
                "site_name": "Bóng Đá",
                "short_name": "Bóng Đá",
                "description": "Danh sách trực tiếp bóng đá",
                "output_base": "bongda",
            },
            "sources_found": len(results),
            "channels": total_channels,
            "m3u_items": total_m3u_items,
            "groups": counts,
            "sources": sources,
            "errors": errors,
        },
    }

def collect_stream_urls_from_values(*values) -> list[str]:
    out = []
    seen = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False)
        else:
            text = str(value)
        for url in unique_stream_urls(text):
            if url not in seen:
                seen.add(url)
                out.append(url)
    return out

def first_non_empty(*values):
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            text = clean_text(value)
            if text:
                return text
            continue
        return value
    return ""

def nested_dict(value: dict, *keys) -> dict:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}

def text_from_obj(value, *keys) -> str:
    if isinstance(value, dict):
        for key in keys:
            text = clean_text(value.get(key, ""))
            if text:
                return text
    return ""

def api_headers(source_url: str, content_type: str = "") -> dict:
    root = site_root_url(source_url)
    headers = {
        "User-Agent": BROWSER_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Referer": source_url,
        "Origin": root,
        "X-Requested-With": "XMLHttpRequest",
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers

def get_json_list(payload) -> list:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ["data", "matches", "results", "fixtures", "livestreams"]:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    data = payload.get("data")
    if isinstance(data, dict):
        return get_json_list(data)
    return []

def relevant_script_url(script_url: str, source_url: str) -> bool:
    parsed = urlparse(script_url)
    path = parsed.path.lower()
    host = (parsed.hostname or "").lower()
    source_host = (urlparse(source_url).hostname or "").lower()
    if not path.endswith(".js"):
        return False
    if any(blocked in host for blocked in ["googletagmanager", "cloudflareinsights", "google-analytics"]):
        return False
    if host and host != source_host and "cdn" not in host and "static" not in host:
        return False
    return (
        "/assets/" in path
        or "/_nuxt/app" in path
        or "/_nuxt/runtime" in path
        or path.endswith("/main.js")
    )

def script_urls_from_html(home_html: str, source_url: str) -> list[str]:
    soup = BeautifulSoup(home_html, "html.parser")
    seen = set()
    out = []
    assets = [script.get("src", "") for script in soup.find_all("script", src=True)]
    for link in soup.find_all("link", href=True):
        rels = {value.lower() for value in (link.get("rel") or [])}
        href = link.get("href", "")
        if not href:
            continue
        if "modulepreload" in rels:
            assets.append(href)
            continue
        if "preload" in rels and (link.get("as", "").lower() == "script" or href.lower().endswith(".js")):
            assets.append(href)

    for asset in assets:
        script_url = urljoin(source_url, asset)
        key = script_url.split("#", 1)[0]
        if key in seen or not relevant_script_url(key, source_url):
            continue
        seen.add(key)
        out.append(key)
    return out[:12]

def imported_script_urls(script_text: str, base_url: str, source_url: str) -> list[str]:
    seen = set()
    out = []
    for match in SCRIPT_IMPORT_RE.findall(script_text or ""):
        script_url = urljoin(base_url, unescape(match))
        key = script_url.split("#", 1)[0]
        if key in seen or not relevant_script_url(key, source_url):
            continue
        seen.add(key)
        out.append(key)
    return out

def discovery_texts(session: requests.Session, home_html: str, source_url: str) -> list[str]:
    texts = [home_html]
    queue = script_urls_from_html(home_html, source_url)
    seen = set()
    while queue and len(seen) < MAX_DISCOVERY_SCRIPTS:
        script_url = queue.pop(0)
        key = script_url.split("#", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        try:
            response = session.get(script_url, timeout=TIMEOUT)
            if response.status_code == 200:
                script_text = decoded_response_text(response)
                texts.append(script_text)
                for imported_url in imported_script_urls(script_text, script_url, source_url):
                    if imported_url not in seen and imported_url not in queue:
                        queue.append(imported_url)
        except Exception:
            continue
    return texts

def discover_api_candidates(session: requests.Session, home_html: str, source_url: str) -> list[dict]:
    combined = "\n".join(discovery_texts(session, home_html, source_url))
    urls = []
    seen_urls = set()
    for raw in re.findall(r'https?://[^\s"\'`)]+', combined):
        clean_url = unescape(raw).replace("\\/", "/").rstrip(";,")
        if "${" in clean_url:
            continue
        try:
            parsed = urlparse(clean_url)
        except ValueError:
            continue
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        if clean_url not in seen_urls:
            seen_urls.add(clean_url)
            urls.append(clean_url)

    candidates = []
    seen = set()

    def add(kind: str, url: str, **extra):
        key = (kind, url.rstrip("/"))
        if not url or key in seen:
            return
        seen.add(key)
        candidates.append({"kind": kind, "url": url.rstrip("/"), **extra})

    for url in urls:
        try:
            parsed = urlparse(url)
        except ValueError:
            continue
        path = parsed.path.rstrip("/")
        if path.endswith("/api/v1/external"):
            add("external-fixtures", url)
        if "/internal/api/matches" in path:
            add("internal-matches", url.split("?", 1)[0].rstrip("/"))
        if path.endswith("/v2") and ("/livestreams/public" in combined or "/matches" in combined):
            add("v2-livestreams", url)

    if "/matches/graph" in combined:
        for url in urls:
            try:
                parsed = urlparse(url)
            except ValueError:
                continue
            if "api" in (parsed.hostname or "") and parsed.path in {"", "/"}:
                add("matches-graph", url)

    return candidates

def external_fixture_matches(session: requests.Session, base_url: str, source_url: str, max_matches: int) -> list[dict]:
    response = session.get(
        f"{base_url}/fixtures/unfinished",
        headers=api_headers(source_url),
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    matches = get_json_list(response.json())
    matches.sort(key=lambda item: parse_datetime_to_vn(item.get("startTime", ""), timezone.utc) or datetime.max.replace(tzinfo=VN_TZ))
    return matches[:max_matches]

def internal_matches(session: requests.Session, api_url: str, source_url: str, max_matches: int) -> list[dict]:
    response = session.get(api_url, headers=api_headers(source_url), timeout=TIMEOUT)
    response.raise_for_status()
    matches = get_json_list(response.json())
    return matches[:max_matches]

def graph_matches(session: requests.Session, base_url: str, source_url: str, max_matches: int) -> list[dict]:
    now_utc = datetime.now(timezone.utc)
    body = {
        "queries": [
            {"field": "start_date", "type": "gte", "value": (now_utc - DEFAULT_MATCH_DURATION).strftime("%Y-%m-%d %H:%M:%S")},
            {"field": "start_date", "type": "lte", "value": (now_utc + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")},
        ],
        "query_and": True,
        "limit": max_matches,
        "page": 1,
        "order_asc": "start_date",
    }
    response = session.post(
        f"{base_url}/matches/graph",
        headers=api_headers(source_url, "application/json"),
        data=json.dumps(body),
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return get_json_list(response.json())[:max_matches]

def v2_livestream_matches(session: requests.Session, base_url: str, source_url: str, max_matches: int) -> list[dict]:
    out = []
    seen = set()
    endpoints = [
        f"{base_url}/matches?limit={max_matches}",
        f"{base_url}/livestreams/public",
    ]
    for endpoint in endpoints:
        try:
            response = session.get(endpoint, headers=api_headers(source_url), timeout=TIMEOUT)
            response.raise_for_status()
            records = get_json_list(response.json())
        except Exception:
            continue
        for record in records:
            key = first_non_empty(record.get("_id"), record.get("id"), record.get("externalId"), nested_dict(record, "match").get("externalId"), json.dumps(record, sort_keys=True)[:120])
            if key in seen:
                continue
            seen.add(key)
            out.append(record)
            if len(out) >= max_matches:
                return out
    return out

def embedded_match_arrays(home_html: str) -> list[dict]:
    out = []
    for var_name in ["lives", "matches", "listLives", "liveMatches"]:
        match = re.search(rf"\b(?:var|let|const)\s+{re.escape(var_name)}\s*=\s*(\[.*?\]);", home_html, re.S)
        if not match:
            continue
        try:
            records = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(records, list) and any(isinstance(item, dict) for item in records):
            out.extend(item for item in records if isinstance(item, dict))
    return out

def api_team_info(match: dict) -> tuple[str, str, str, str]:
    home = (
        nested_dict(match, "homeTeam")
        or nested_dict(match, "homeClub")
        or nested_dict(match, "teamA")
        or nested_dict(match, "match", "homeTeam")
        or nested_dict(match, "teams", "home")
    )
    away = (
        nested_dict(match, "awayTeam")
        or nested_dict(match, "awayClub")
        or nested_dict(match, "teamB")
        or nested_dict(match, "match", "awayTeam")
        or nested_dict(match, "teams", "away")
    )
    team_a = first_non_empty(
        text_from_obj(home, "name"),
        match.get("homeClubName"),
        match.get("team_1"),
    )
    team_b = first_non_empty(
        text_from_obj(away, "name"),
        match.get("awayClubName"),
        match.get("team_2"),
    )
    team_a_image = first_non_empty(
        text_from_obj(home, "logoUrl", "logo", "picture", "image"),
        match.get("homeClubLogoUrl"),
        match.get("team_1_logo"),
    )
    team_b_image = first_non_empty(
        text_from_obj(away, "logoUrl", "logo", "picture", "image"),
        match.get("awayClubLogoUrl"),
        match.get("team_2_logo"),
    )
    return str(team_a), str(team_b), str(team_a_image), str(team_b_image)

def api_league_name(match: dict) -> str:
    league = match.get("league")
    if isinstance(league, dict):
        return text_from_obj(league, "name", "shortName")
    return clean_text(first_non_empty(
        match.get("tournamentName"),
        match.get("leagueName"),
        league,
        nested_dict(match, "match", "league").get("name"),
    ))

def api_start_time(match: dict, naive_tz: timezone) -> datetime | None:
    value = first_non_empty(
        match.get("startTime"),
        match.get("matchTime"),
        match.get("start_date"),
        match.get("start_date_formatted"),
    )
    if not value and isinstance(match.get("time"), (int, float)):
        value = match.get("time")
    return parse_datetime_to_vn(value, naive_tz)

def api_status_without_time(match: dict) -> str:
    raw_status = str(match.get("status", "")).lower()
    if match.get("isLive") or match.get("is_live") or raw_status in {"live", "1h", "2h", "ht"}:
        return "live"
    if raw_status in {"ft", "finished", "ended", "done"}:
        return "finished"
    return "upcoming"

def parse_api_match_info(match: dict, naive_tz: timezone = VN_TZ) -> dict:
    start_at_dt = api_start_time(match, naive_tz)
    end_at_dt = start_at_dt + DEFAULT_MATCH_DURATION if start_at_dt else None
    status = status_from_times(start_at_dt, end_at_dt) if start_at_dt else api_status_without_time(match)
    team_a, team_b, team_a_image, team_b_image = api_team_info(match)
    title_like = clean_text(first_non_empty(match.get("title"), match.get("name")))
    if not team_a or not team_b:
        fallback_a, fallback_b = split_teams_from_title(title_like)
        team_a = team_a or fallback_a
        team_b = team_b or fallback_b
    if not title_like and team_a and team_b:
        title_like = f"{team_a} vs {team_b}"

    return {
        "time": start_at_dt.strftime("%H:%M") if start_at_dt else "",
        "date": start_at_dt.strftime("%Y-%m-%d") if start_at_dt else "",
        "start_at": format_datetime(start_at_dt),
        "end_at": format_datetime(end_at_dt),
        "status": status,
        "status_text": status_text(status),
        "league": api_league_name(match),
        "team_a": team_a.strip(),
        "team_b": team_b.strip(),
        "team_a_image": team_a_image.strip(),
        "team_b_image": team_b_image.strip(),
        "title_like": title_like.strip(),
    }

def api_match_url(source_url: str, match: dict, info: dict) -> str:
    root = site_root_url(source_url).rstrip("/") + "/"
    slug = clean_text(first_non_empty(match.get("slug"), match.get("nameNoUtf8")))
    if not slug:
        slug = slugify(build_channel_name(info), fallback="match")
    match_id = first_non_empty(match.get("_id"), match.get("id"), match.get("externalId"), match.get("referenceId"), nested_dict(match, "match").get("externalId"))
    if "homeClub" in match and match.get("id") and match.get("slug"):
        return urljoin(root, f"truc-tiep/{slug}-I{match.get('id')}")
    if match_id:
        return urljoin(root, f"truc-tiep/{slug}-{match_id}")
    return urljoin(root, f"truc-tiep/{slug}")

def api_stream_urls(match: dict) -> list[str]:
    return collect_stream_urls_from_values(match)

def build_result_from_api_matches(
    matches: list[dict],
    source_url: str,
    source_metadata: dict,
    adapter: str,
    api_url: str = "",
    naive_tz: timezone = VN_TZ,
    logger=None,
) -> dict:
    source_name = source_metadata.get("site_name") or source_metadata.get("title") or urlparse(source_url).netloc
    source_image = source_metadata.get("image", "")
    m3u_items = []
    group_channels = {"live": [], "upcoming": [], "finished": []}
    seen_streams = set()
    match_stats = []
    matches = [item for item in matches if isinstance(item, dict)]
    matches.sort(key=lambda item: api_start_time(item, naive_tz) or datetime.max.replace(tzinfo=VN_TZ))

    if logger:
        logger(f"Adapter: {adapter}")
        if api_url:
            logger(f"API: {api_url}")
        logger(f"Tim thay so tran dau API: {len(matches)}")

    for idx, match in enumerate(matches, 1):
        info = parse_api_match_info(match, naive_tz)
        match_url = api_match_url(source_url, match, info)
        base_name = build_channel_name(info)
        streams = api_stream_urls(match)
        stream_urls = streams if info["status"] == "live" else []
        status = info["status"] if info["status"] in group_channels else "upcoming"

        group_channels[status].append(
            build_channel(info, match_url, stream_urls, source_name, source_name, source_image, source_url)
        )

        added_count = 0
        if info["status"] == "live":
            for link_idx, url in enumerate(stream_urls, 1):
                if url in seen_streams:
                    continue
                seen_streams.add(url)
                display_name = base_name if link_idx == 1 else f"{base_name} (Link {link_idx})"
                m3u_items.append({
                    "name": (f'[{info["time"]}] {display_name}'.strip() if info.get("time") else display_name),
                    "url": url,
                    "group": base_name,
                    "logo": source_image or info.get("team_a_image") or info.get("team_b_image"),
                    "headers": guess_request_headers(url, match_url, source_url),
                })
                added_count += 1

        match_stats.append({
            "url": match_url,
            "name": base_name,
            "status": info["status"],
            "status_text": info["status_text"],
            "time": info.get("time", ""),
            "streams_found": len(streams),
            "m3u8_found": len(streams),
            "m3u_added": added_count,
        })

        if logger:
            logger(
                f"[{idx}/{len(matches)}] {match_url} | {info['status_text']} | "
                f"Tim thay {len(streams)} stream | Da them M3U {added_count} link"
            )

    counts = {status: len(channels) for status, channels in group_channels.items()}
    return {
        "json": build_buncha_json(
            group_channels,
            source_url,
            source_metadata,
            groups=build_single_source_groups(group_channels, source_name, source_metadata),
        ),
        "m3u": build_m3u_text(m3u_items),
        "stats": {
            "source": source_url,
            "source_metadata": source_metadata,
            "adapter": adapter,
            "api_url": api_url,
            "matches_found": len(matches),
            "channels": sum(counts.values()),
            "m3u_items": len(m3u_items),
            "groups": counts,
            "matches": match_stats,
        }
    }

def crawl_spa_or_api_source(
    session: requests.Session,
    source_url: str,
    home_html: str,
    source_metadata: dict,
    max_matches: int,
    logger=None,
) -> dict | None:
    embedded = embedded_match_arrays(home_html)
    if embedded:
        return build_result_from_api_matches(
            embedded[:max_matches],
            source_url,
            source_metadata,
            "embedded-json",
            naive_tz=VN_TZ,
            logger=logger,
        )

    candidates = discover_api_candidates(session, home_html, source_url)
    for candidate in candidates:
        try:
            kind = candidate["kind"]
            api_url = candidate["url"]
            if kind == "external-fixtures":
                matches = external_fixture_matches(session, api_url, source_url, max_matches)
                naive_tz = timezone.utc
            elif kind == "internal-matches":
                matches = internal_matches(session, api_url, source_url, max_matches)
                naive_tz = VN_TZ
            elif kind == "matches-graph":
                matches = graph_matches(session, api_url, source_url, max_matches)
                naive_tz = timezone.utc
            elif kind == "v2-livestreams":
                matches = v2_livestream_matches(session, api_url, source_url, max_matches)
                naive_tz = timezone.utc
            else:
                continue
        except Exception as e:
            if logger:
                logger(f"Bo qua API {candidate.get('url')}: {e}")
            continue

        if matches:
            return build_result_from_api_matches(
                matches[:max_matches],
                source_url,
                source_metadata,
                kind,
                candidate["url"],
                naive_tz,
                logger,
            )

    return None

def fetch_home_response(session: requests.Session, source_url: str) -> tuple[requests.Response, str]:
    last_error = None
    response = None
    for _ in range(2):
        try:
            response = session.get(source_url, timeout=TIMEOUT)
            break
        except requests.RequestException as e:
            last_error = e

    if response is None:
        raise last_error or RuntimeError("Khong tai duoc trang nguon")

    if response.status_code < 400:
        return response, source_url

    parsed = urlparse(source_url)
    root_url = site_root_url(source_url) + "/"
    if parsed.path and parsed.path != "/" and root_url != source_url:
        root_response = session.get(root_url, timeout=TIMEOUT)
        if root_response.status_code < 400:
            return root_response, root_url

    response.raise_for_status()
    return response, source_url

def crawl(max_matches: int = MAX_MATCHES, source_url: str = START_URL, logger=None) -> dict:
    source_url = normalize_source_url(source_url)
    s = create_session()
    home, home_url = fetch_home_response(s, source_url)
    home_html = decoded_response_text(home)

    source_metadata = extract_source_metadata(home_html, home_url)
    source_metadata["source_url"] = source_url
    source_name = source_metadata.get("site_name") or source_metadata.get("title") or urlparse(source_url).netloc
    source_image = source_metadata.get("image", "")

    api_result = crawl_spa_or_api_source(s, source_url, home_html, source_metadata, max_matches, logger)
    if api_result:
        if logger:
            logger(f"Nguon: {source_metadata.get('title', source_url)}")
            logger(f"Logo/Image: {source_image}")
            logger("Da crawl bang logic API/SPA chung")
        return api_result

    match_links = extract_match_links(home_html, source_url)[:max_matches]
    if logger:
        logger(f"Nguồn: {source_metadata.get('title', source_url)}")
        logger(f"Logo/Image: {source_image}")
        logger(f"Tìm thấy số trang trận đấu: {len(match_links)}")

    m3u_items = []
    group_channels = {"live": [], "upcoming": [], "finished": []}
    seen_streams = set()
    match_stats = []

    for idx, match_url in enumerate(match_links, 1):
        try:
            r = s.get(match_url, timeout=TIMEOUT)
            if r.status_code != 200:
                match_stats.append({
                    "url": match_url,
                    "status_code": r.status_code,
                    "error": "Non-200 response"
                })
                continue
            html = decoded_response_text(r)
        except Exception as e:
            match_stats.append({
                "url": match_url,
                "error": str(e)
            })
            continue

        info = parse_match_info(html)
        base_name = build_channel_name(info)

        # Lấy tất cả stream duy nhất theo thứ tự.
        streams = unique_stream_urls(html)
        stream_urls = streams if info["status"] == "live" else []
        status = info["status"] if info["status"] in group_channels else "upcoming"
        group_channels[status].append(
            build_channel(info, match_url, stream_urls, source_name, source_name, source_image, source_url)
        )

        added_count = 0
        if info["status"] == "live":
            for link_idx, u in enumerate(stream_urls, 1):
                if u in seen_streams:
                    continue
                seen_streams.add(u)

                display_name = base_name if link_idx == 1 else f"{base_name} (Link {link_idx})"
                m3u_items.append({
                    "name": (f'[{info["time"]}] {display_name}'.strip() if info.get("time") else display_name),
                    "url": u,
                    "group": base_name,
                    "logo": source_image or info.get("team_a_image") or info.get("team_b_image"),
                    "headers": guess_request_headers(u, match_url, source_url),
                })
                added_count += 1

        match_stats.append({
            "url": match_url,
            "name": base_name,
            "status": info["status"],
            "status_text": info["status_text"],
            "time": info.get("time", ""),
            "streams_found": len(streams),
            "m3u8_found": len(streams),
            "m3u_added": added_count
        })

        if logger:
            logger(
                f"[{idx}/{len(match_links)}] {match_url} | {info['status_text']} | "
                f"Tìm thấy {len(streams)} stream | Đã thêm M3U {added_count} link"
            )

    counts = {status: len(channels) for status, channels in group_channels.items()}
    return {
        "json": build_buncha_json(
            group_channels,
            source_url,
            source_metadata,
            groups=build_single_source_groups(group_channels, source_name, source_metadata),
        ),
        "m3u": build_m3u_text(m3u_items),
        "stats": {
            "source": source_url,
            "source_metadata": source_metadata,
            "matches_found": len(match_links),
            "channels": sum(counts.values()),
            "m3u_items": len(m3u_items),
            "groups": counts,
            "matches": match_stats,
        }
    }

def main():
    source_url = sys.argv[1] if len(sys.argv) > 1 else START_URL
    try:
        result = crawl(source_url=source_url, logger=print)
    except Exception as e:
        print(f"Lỗi khi crawl: {e}")
        return

    output_base = result["stats"]["source_metadata"].get("output_base", "crawl")
    out_m3u = OUT_M3U or f"{output_base}.txt"
    out_json = OUT_JSON or f"{output_base}.json"

    with open(out_m3u, "w", encoding="utf-8", newline="\n") as f:
        f.write(result["m3u"])

    with open(out_json, "w", encoding="utf-8", newline="\n") as f:
        json.dump(result["json"], f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"\nXONG -> {out_m3u} | Tổng số kênh M3U: {result['stats']['m3u_items']}")
    print(f"XONG -> {out_json} | Tổng số channels JSON: {result['stats']['channels']}")

if __name__ == "__main__":
    main()