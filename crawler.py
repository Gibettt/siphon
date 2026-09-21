"""
crawler.py - Crawls and downloads all frontend assets from a website
"""
import asyncio
import os
import re
import zipfile
import io
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse
from typing import Callable, Optional

import httpx
from bs4 import BeautifulSoup


# Asset extensions to download
ASSET_EXTENSIONS = {
    # Stylesheets
    ".css", ".scss", ".sass", ".less",
    # Scripts
    ".js", ".mjs", ".jsx", ".ts", ".tsx",
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".avif", ".bmp",
    # Fonts
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    # Video/Audio
    ".mp4", ".webm", ".ogg", ".mp3", ".wav",
    # Documents/Data
    ".json", ".xml", ".txt", ".pdf",
    # Maps
    ".map",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(fragment="", query=""))


def url_to_local_path(url: str, base_domain: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.lstrip("/")
    if not path or path.endswith("/"):
        path = path + "index.html"
    elif "." not in Path(path).name:
        path = path + "/index.html"
    return path


def extract_css_urls(css_text: str, base_url: str) -> list[str]:
    """Extract URLs referenced inside CSS files."""
    urls = []
    for match in re.finditer(r'url\(["\']?(.*?)["\']?\)', css_text):
        href = match.group(1).strip()
        if href and not href.startswith("data:"):
            urls.append(urljoin(base_url, href))
    # @import
    for match in re.finditer(r'@import\s+["\']([^"\']+)["\']', css_text):
        href = match.group(1).strip()
        if href:
            urls.append(urljoin(base_url, href))
    return urls


def extract_js_urls(js_text: str, base_url: str) -> list[str]:
    """Try to extract static asset URLs from JS (chunk manifests, etc.)."""
    urls = []
    # Look for /_next/static, /static/, etc. patterns
    for match in re.finditer(r'["\`\']((?:/[a-zA-Z0-9_\-./]+\.(?:js|css|png|jpg|svg|woff2?))["\`\'])', js_text):
        path = match.group(1).strip("\"'`")
        if path:
            urls.append(urljoin(base_url, path))
    return urls


async def fetch(client: httpx.AsyncClient, url: str) -> tuple[bytes, dict, int]:
    try:
        resp = await client.get(url, follow_redirects=True, timeout=20)
        return resp.content, dict(resp.headers), resp.status_code
    except Exception as e:
        return b"", {}, 0


class WebCrawler:
    def __init__(
        self,
        start_url: str,
        max_pages: int = 50,
        max_assets: int = 500,
        same_origin_only: bool = True,
        progress_cb: Optional[Callable] = None,
    ):
        self.start_url = start_url.rstrip("/")
        parsed = urlparse(start_url)
        self.base_origin = f"{parsed.scheme}://{parsed.netloc}"
        self.base_domain = parsed.netloc
        self.max_pages = max_pages
        self.max_assets = max_assets
        self.same_origin_only = same_origin_only
        self.progress_cb = progress_cb

        self.visited_pages: set[str] = set()
        self.downloaded_assets: dict[str, bytes] = {}  # local_path -> bytes
        self.page_queue: list[str] = [start_url]
        self.asset_queue: set[str] = set()
        self.log: list[str] = []
        self.total_bytes: int = 0

    def _log(self, msg: str):
        self.log.append(msg)
        if self.progress_cb:
            self.progress_cb(msg)

    def _is_same_origin(self, url: str) -> bool:
        return url.startswith(self.base_origin)

    def _should_crawl_page(self, url: str) -> bool:
        if not self._is_same_origin(url):
            return False
        parsed = urlparse(url)
        ext = Path(parsed.path).suffix.lower()
        if ext and ext not in {".html", ".htm", ".php", ".asp", ".aspx", ""}:
            return False
        return True

    def _extract_links_and_assets(self, html: str, page_url: str):
        soup = BeautifulSoup(html, "lxml")

        # Page links
        for tag in soup.find_all("a", href=True):
            href = tag["href"].strip()
            if href.startswith("mailto:") or href.startswith("tel:") or href.startswith("javascript:"):
                continue
            full = urljoin(page_url, href)
            full = normalize_url(full)
            if self._should_crawl_page(full) and full not in self.visited_pages:
                self.page_queue.append(full)

        # CSS links
        for tag in soup.find_all("link", rel=True):
            rel = " ".join(tag.get("rel", [])).lower()
            href = tag.get("href", "").strip()
            if not href:
                continue
            if "stylesheet" in rel or href.endswith(".css"):
                full = urljoin(page_url, href)
                self.asset_queue.add(full)

        # Inline styles with url()
        for tag in soup.find_all(style=True):
            for u in extract_css_urls(tag["style"], page_url):
                self.asset_queue.add(u)

        # Scripts
        for tag in soup.find_all("script", src=True):
            src = tag["src"].strip()
            if src:
                self.asset_queue.add(urljoin(page_url, src))

        # Images
        for tag in soup.find_all(["img", "image", "source"]):
            for attr in ["src", "srcset", "data-src", "data-srcset"]:
                val = tag.get(attr, "").strip()
                if val:
                    for part in val.split(","):
                        part = part.strip().split(" ")[0]
                        if part:
                            self.asset_queue.add(urljoin(page_url, part))

        # Favicon
        for tag in soup.find_all("link"):
            rel = " ".join(tag.get("rel", [])).lower()
            if "icon" in rel:
                href = tag.get("href", "")
                if href:
                    self.asset_queue.add(urljoin(page_url, href))

        # Videos
        for tag in soup.find_all(["video", "audio"]):
            src = tag.get("src", "")
            if src:
                self.asset_queue.add(urljoin(page_url, src))

        # Preload links
        for tag in soup.find_all("link", rel=True):
            rel = " ".join(tag.get("rel", [])).lower()
            if "preload" in rel or "prefetch" in rel or "modulepreload" in rel:
                href = tag.get("href", "")
                if href:
                    self.asset_queue.add(urljoin(page_url, href))

        # Meta og:image etc.
        for meta in soup.find_all("meta"):
            content = meta.get("content", "")
            if content and content.startswith("http") and any(
                content.endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".svg", ".webp"]
            ):
                self.asset_queue.add(content)

    async def _download_asset(self, client: httpx.AsyncClient, url: str):
        if not url.startswith("http"):
            return
        norm = normalize_url(url)
        local_path = url_to_local_path(norm, self.base_domain)

        if local_path in self.downloaded_assets:
            return

        content, headers, status = await fetch(client, url)
        if status == 0 or not content:
            self._log(f"  ✗ Failed: {url}")
            return

        self.downloaded_assets[local_path] = content
        self.total_bytes += len(content)
        self._log(f"  ✓ Asset ({len(content)//1024}KB): {local_path}")

        # If CSS, extract more URLs
        ct = headers.get("content-type", "")
        if "css" in ct or url.endswith(".css"):
            css_text = content.decode("utf-8", errors="ignore")
            extra_urls = extract_css_urls(css_text, url)
            for eu in extra_urls:
                if len(self.downloaded_assets) < self.max_assets:
                    await self._download_asset(client, eu)

    async def crawl(self) -> dict:
        self._log(f"🚀 Starting crawl: {self.start_url}")
        start_time = time.time()

        limits = httpx.Limits(max_connections=10, max_keepalive_connections=5)
        async with httpx.AsyncClient(headers=HEADERS, limits=limits) as client:
            first_html = ""
            first_headers = {}

            # Crawl pages
            while self.page_queue and len(self.visited_pages) < self.max_pages:
                url = self.page_queue.pop(0)
                url = normalize_url(url)
                if url in self.visited_pages:
                    continue
                self.visited_pages.add(url)

                self._log(f"📄 Page: {url}")
                content, hdrs, status = await fetch(client, url)
                if not content or status == 0:
                    self._log(f"  ✗ Failed (status={status})")
                    continue

                html = content.decode("utf-8", errors="ignore")
                local_path = url_to_local_path(url, self.base_domain)
                self.downloaded_assets[local_path] = content
                self.total_bytes += len(content)

                if not first_html:
                    first_html = html
                    first_headers = hdrs

                self._extract_links_and_assets(html, url)

                # Download assets concurrently in batches
                if len(self.asset_queue) > 0:
                    batch = list(self.asset_queue)[:50]
                    self.asset_queue -= set(batch)
                    tasks = [
                        self._download_asset(client, u)
                        for u in batch
                        if len(self.downloaded_assets) < self.max_assets
                    ]
                    await asyncio.gather(*tasks)

            # Download remaining assets
            while self.asset_queue and len(self.downloaded_assets) < self.max_assets:
                batch = list(self.asset_queue)[:50]
                self.asset_queue -= set(batch)
                tasks = [self._download_asset(client, u) for u in batch]
                await asyncio.gather(*tasks)

        elapsed = time.time() - start_time
        self._log(f"✅ Done! {len(self.downloaded_assets)} files, {self.total_bytes//1024}KB in {elapsed:.1f}s")

        return {
            "html": first_html,
            "headers": first_headers,
            "files": self.downloaded_assets,
            "stats": {
                "pages": len(self.visited_pages),
                "assets": len(self.downloaded_assets),
                "total_kb": self.total_bytes // 1024,
                "elapsed": round(elapsed, 1),
            },
            "log": self.log,
        }


def build_zip(files: dict[str, bytes], site_name: str) -> bytes:
    """Pack all downloaded files into a ZIP archive."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for local_path, content in files.items():
            arcname = f"{site_name}/{local_path}"
            zf.writestr(arcname, content)
    return buf.getvalue()
