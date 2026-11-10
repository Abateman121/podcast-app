import html
import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urljoin, urlparse

import requests

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) podcast-app/1.0"}


class SourceError(Exception):
    pass


def http_get(url, **kwargs):
    resp = requests.get(url, headers=UA, timeout=20, **kwargs)
    resp.raise_for_status()
    return resp


# ---------- OPML ----------

def parse_opml(text):
    root = ET.fromstring(text)
    found = []
    for node in root.iter("outline"):
        feed = node.get("xmlUrl") or node.get("xmlurl")
        if feed:
            found.append((feed.strip(), node.get("title") or node.get("text")))
    return found


# ---------- Apple Podcasts search (also used to find RSS feeds for other platforms) ----------

def itunes_search(term):
    resp = http_get(
        "https://itunes.apple.com/search",
        params={"media": "podcast", "term": term, "limit": 5},
    )
    results = [r for r in resp.json().get("results", []) if r.get("feedUrl")]
    if not results:
        raise SourceError(
            f"No public RSS feed found for '{term}'. Shows that are exclusive to one platform have no public feed."
        )
    wanted = term.lower().strip()
    for r in results:
        if r.get("collectionName", "").lower().strip() == wanted:
            return r["feedUrl"], r["collectionName"]
    return results[0]["feedUrl"], results[0]["collectionName"]


def from_apple(url):
    match = re.search(r"/id(\d+)", url)
    if not match:
        raise SourceError("That Apple Podcasts link has no show id (it should contain /id123456789).")
    resp = http_get("https://itunes.apple.com/lookup", params={"id": match.group(1), "entity": "podcast"})
    for r in resp.json().get("results", []):
        if r.get("feedUrl"):
            return r["feedUrl"], None
    raise SourceError("Apple Podcasts did not list a public RSS feed for that show.")


def from_spotify(url):
    if "/show/" not in url:
        raise SourceError("Paste a Spotify show link (open.spotify.com/show/...), not an episode link.")
    resp = http_get("https://open.spotify.com/oembed", params={"url": url})
    title = resp.json().get("title")
    if not title:
        raise SourceError("Could not read the show name from that Spotify link.")
    feed, name = itunes_search(title)
    return feed, f"Matched '{name}' on Apple Podcasts. Check that this is the right show."


def from_amazon(url):
    title = None
    try:
        page = http_get(url).text
        match = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', page) or re.search(
            r"<title>([^<]+)</title>", page
        )
        if match:
            title = html.unescape(match.group(1))
    except requests.RequestException:
        pass
    if not title:
        match = re.search(r"/podcasts/[0-9a-fA-F-]+/([^/?#]+)", url)
        if match:
            title = match.group(1).replace("-", " ")
    if not title:
        raise SourceError("Could not read the show name from that Amazon link.")
    title = re.sub(r"\s*[|\-]?\s*(on\s+)?Amazon Music.*$", "", title, flags=re.IGNORECASE).strip()
    feed, name = itunes_search(title)
    return feed, f"Matched '{name}' on Apple Podcasts. Check that this is the right show."


# ---------- YouTube ----------

def from_youtube(url):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if "list" in query:
        feed = f"https://www.youtube.com/feeds/videos.xml?playlist_id={query['list'][0]}"
        return feed, "YouTube audio is downloaded with yt-dlp. Only the latest 15 videos show up."
    match = re.search(r"/channel/(UC[\w-]{22})", parsed.path)
    channel_id = match.group(1) if match else None
    if not channel_id:
        page = http_get(url).text
        for pattern in (
            r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
            r'"externalId":"(UC[\w-]{22})"',
            r'"channelId":"(UC[\w-]{22})"',
        ):
            found = re.search(pattern, page)
            if found:
                channel_id = found.group(1)
                break
    if not channel_id:
        raise SourceError("Could not find the YouTube channel for that link.")
    feed = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    return feed, "YouTube audio is downloaded with yt-dlp. Only the latest 15 videos show up."


# ---------- plain URLs ----------

def discover_feed(url):
    resp = http_get(url)
    head = resp.text[:2000].lower()
    if "<rss" in head or "<feed" in head or "<?xml" in head:
        return url
    match = re.search(r'<link[^>]+type="application/(?:rss|atom)\+xml"[^>]*>', resp.text, re.IGNORECASE)
    if match:
        href = re.search(r'href="([^"]+)"', match.group(0))
        if href:
            return urljoin(url, html.unescape(href.group(1)))
    return url


def resolve(raw_url):
    """Turn any supported link into (rss_feed_url, note_for_the_user_or_None)."""
    url = raw_url.strip()
    if not url:
        raise SourceError("Paste a link first.")
    host = urlparse(url if "://" in url else "https://" + url).netloc.lower()
    try:
        if "youtube.com" in host or "youtu.be" in host:
            return from_youtube(url)
        if "podcasts.apple.com" in host:
            return from_apple(url)
        if "spotify.com" in host:
            return from_spotify(url)
        if "amazon." in host:
            return from_amazon(url)
        return discover_feed(url), None
    except requests.RequestException as exc:
        raise SourceError(f"Could not reach that link ({exc}).")
