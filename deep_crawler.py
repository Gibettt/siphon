"""
deep_crawler.py - Deep crawler using Playwright to capture ALL network requests
including WebGL assets, 3D models (.glb, .gltf), binary data, and dynamic content.

Strategy:
  1. Launch real Chromium browser via Playwright
  2. Intercept EVERY network response (including 3D assets, WASM, etc.)
  3. Save all responses to local disk with correct path structure
  4. Patch HTML/JS to rewrite absolute URLs → relative local paths
  5. Inject a Service Worker so offline fetch requests get served locally
"""

import asyncio
import io
import os
import re
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse, urlunparse

from playwright.async_api import async_playwright, Request, Response, Page


# Extensions we consider 3D / special assets
THREED_EXTENSIONS = {
    ".glb", ".gltf", ".obj", ".mtl", ".fbx", ".dae", ".3ds",
    ".wasm", ".bin",
}

# Extensions to capture (extends basic set with 3D + data formats)
CAPTURE_EXTENSIONS = {
    # Web basics
    ".html", ".htm", ".css", ".js", ".mjs", ".json", ".xml", ".txt",
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".avif",
    # Fonts
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    # Video / Audio
    ".mp4", ".webm", ".ogg", ".mp3", ".wav",
    # 3D
    ".glb", ".gltf", ".obj", ".mtl", ".fbx", ".dae",
    # Binary / WASM
    ".wasm", ".bin",
    # Data
    ".map", ".pdf",
}

# Service Worker script injected into the root of the downloaded site.
# It intercepts all fetch() calls and serves from cache (our downloaded files).
SERVICE_WORKER_JS = r"""
// sw.js — Auto-generated offline Service Worker by Web Deep Downloader
const CACHE_NAME = 'deep-offline-v1';

self.addEventListener('install', event => {
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(clients.claim());
});

self.addEventListener('fetch', event => {
  event.respondWith(
    caches.match(event.request).then(cached => {
      if (cached) return cached;
      // Try network as fallback
      return fetch(event.request).catch(() => {
        return new Response('Offline: resource not cached', { status: 503 });
      });
    })
  );
});
"""

# Small HTML snippet to register the SW — injected into <head>
SW_REGISTER_SNIPPET = """
<script>
if ('serviceWorker' in navigator) {
  window.addEventListener('load', function() {
    navigator.serviceWorker.register('/sw.js').then(function(reg) {
      console.log('[DeepDownloader] SW registered:', reg.scope);
    }).catch(function(err) {
      console.warn('[DeepDownloader] SW registration failed:', err);
    });
  });
}
</script>
"""


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    # Drop fragment, keep query (important for 3D tile servers)
    return urlunparse(parsed._replace(fragment=""))


def url_to_local_path(url: str, include_query: bool = False) -> str:
    """Convert a URL to a safe local file path."""
    parsed = urlparse(url)
    path = parsed.path.lstrip("/") or "index.html"

    # Handle paths ending in /
    if path.endswith("/"):
        path += "index.html"
    elif "." not in Path(path).name:
        path += "/index.html"

    # Sanitize
    path = re.sub(r'[<>:"|?*\\]', "_", path)

    if include_query and parsed.query:
        # Append query hash to avoid collisions (e.g. tile servers)
        q_hash = hex(hash(parsed.query) & 0xFFFFFF)[2:]
        stem = Path(path).stem
        suffix = Path(path).suffix
        path = str(Path(path).parent / f"{stem}_{q_hash}{suffix}")

    return path


def patch_html(html: str, base_url: str) -> str:
    """
    Rewrite absolute URLs in HTML to relative paths so the page
    works from a local file system.
    """
    parsed_base = urlparse(base_url)
    origin = f"{parsed_base.scheme}://{parsed_base.netloc}"

    # Replace src="https://origin/..." → src="/..."
    html = re.sub(
        r'(src|href|action)=["\']' + re.escape(origin) + r'(/[^"\']*)["\']',
        lambda m: f'{m.group(1)}="{m.group(2)}"',
        html,
    )
    # Also patch srcset
    html = re.sub(
        re.escape(origin) + r'(/[^\s,"\']+)',
        lambda m: m.group(1),
        html,
    )
    return html


def patch_js(js: str, base_url: str) -> str:
    """Rewrite absolute origin URLs inside JS files."""
    parsed_base = urlparse(base_url)
    origin = f"{parsed_base.scheme}://{parsed_base.netloc}"
    js = js.replace(f'"{origin}/', '"/')
    js = js.replace(f"'{origin}/", "'/")
    js = js.replace(f"`{origin}/", "`/")
    return js


class DeepCrawler:
    def __init__(
        self,
        start_url: str,
        wait_seconds: int = 15,
        scroll_pages: bool = True,
        interact_3d: bool = True,
        cross_origin: bool = True,
        progress_cb: Optional[Callable] = None,
    ):
        self.start_url = start_url.rstrip("/")
        parsed = urlparse(start_url)
        self.base_origin = f"{parsed.scheme}://{parsed.netloc}"
        self.base_domain = parsed.netloc

        self.wait_seconds = wait_seconds
        self.scroll_pages = scroll_pages
        self.interact_3d = interact_3d
        self.cross_origin = cross_origin
        self.progress_cb = progress_cb

        # url_str -> bytes
        self.captured: dict[str, bytes] = {}
        # url_str -> content-type
        self.content_types: dict[str, str] = {}
        self.log: list[str] = []
        self.total_bytes: int = 0

    def _log(self, msg: str):
        self.log.append(msg)
        if self.progress_cb:
            self.progress_cb(msg)

    async def _on_response(self, response: Response):
        """Intercept every HTTP response and save its body."""
        url = response.url
        status = response.status
        if status == 0 or status >= 400:
            return
        # Skip data URIs
        if url.startswith("data:"):
            return

        norm = normalize_url(url)
        if norm in self.captured:
            return

        try:
            body = await response.body()
        except Exception:
            return

        if not body:
            return

        ct = response.headers.get("content-type", "")
        self.captured[norm] = body
        self.content_types[norm] = ct
        self.total_bytes += len(body)

        # Detect 3D assets
        ext = Path(urlparse(url).path).suffix.lower()
        tag = "🧊 3D" if ext in THREED_EXTENSIONS or "model" in ct else "📦"
        if "wasm" in ct or ext == ".wasm":
            tag = "⚙️ WASM"
        elif ext in {".glb", ".gltf"}:
            tag = "🧊 GLB/GLTF"

        self._log(f"  {tag} ({len(body)//1024}KB): {url[:80]}")

    async def _scroll_and_interact(self, page: Page):
        """Scroll page & trigger interactions to load lazy 3D assets."""
        try:
            # Scroll down in steps to trigger lazy loading
            for pct in [25, 50, 75, 100]:
                await page.evaluate(
                    f"window.scrollTo(0, document.body.scrollHeight * {pct/100})"
                )
                await asyncio.sleep(1.5)

            # Scroll back to top
            await page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(1)

            # Try clicking canvas elements to wake up 3D viewers
            if self.interact_3d:
                canvases = await page.query_selector_all("canvas")
                for canvas in canvases[:3]:
                    try:
                        box = await canvas.bounding_box()
                        if box:
                            cx = box["x"] + box["width"] / 2
                            cy = box["y"] + box["height"] / 2
                            await page.mouse.move(cx, cy)
                            await page.mouse.click(cx, cy)
                            await asyncio.sleep(0.5)
                            self._log(f"  🖱️ Clicked canvas at ({cx:.0f},{cy:.0f})")
                    except Exception:
                        pass

                # Also look for model-viewer elements
                model_viewers = await page.query_selector_all("model-viewer")
                for mv in model_viewers[:3]:
                    try:
                        await mv.scroll_into_view_if_needed()
                        await asyncio.sleep(1)
                        self._log("  👁️ Scrolled to model-viewer element")
                    except Exception:
                        pass

        except Exception as e:
            self._log(f"  ⚠️ Interact error: {e}")

    async def _crawl_iframes(self, page: Page):
        """Also navigate to iframe src URLs to capture their assets."""
        try:
            iframes = await page.query_selector_all("iframe[src]")
            iframe_urls = []
            for iframe in iframes:
                src = await iframe.get_attribute("src")
                if src and src.startswith("http"):
                    iframe_urls.append(src)

            for iframe_url in iframe_urls[:5]:
                self._log(f"  🖼️ Crawling iframe: {iframe_url[:80]}")
                try:
                    iframe_page = await page.context.new_page()
                    iframe_page.on("response", self._on_response)
                    await iframe_page.goto(iframe_url, wait_until="networkidle", timeout=30000)
                    await self._scroll_and_interact(iframe_page)
                    await asyncio.sleep(self.wait_seconds)
                    await iframe_page.close()
                except Exception as e:
                    self._log(f"    ⚠️ iframe error: {e}")
        except Exception as e:
            self._log(f"  ⚠️ iframe crawl error: {e}")

    def _build_file_map(self) -> dict[str, bytes]:
        """
        Convert captured {url -> bytes} into {local_path -> bytes},
        patch HTML/JS content to use relative paths.
        """
        files: dict[str, bytes] = {}

        for url, body in self.captured.items():
            ext = Path(urlparse(url).path).suffix.lower()
            has_query = bool(urlparse(url).query)
            local_path = url_to_local_path(url, include_query=has_query)
            ct = self.content_types.get(url, "")

            # Patch HTML files
            if ext in {".html", ".htm"} or "html" in ct:
                try:
                    text = body.decode("utf-8", errors="replace")
                    text = patch_html(text, url)
                    # Inject SW registration before </head>
                    text = text.replace("</head>", SW_REGISTER_SNIPPET + "</head>", 1)
                    body = text.encode("utf-8")
                except Exception:
                    pass

            # Patch JS files
            elif ext in {".js", ".mjs"} or "javascript" in ct:
                try:
                    text = body.decode("utf-8", errors="replace")
                    text = patch_js(text, url)
                    body = text.encode("utf-8")
                except Exception:
                    pass

            files[local_path] = body

        # Add Service Worker
        files["sw.js"] = SERVICE_WORKER_JS.encode("utf-8")

        # Add offline cache loader (pre-caches all captured files)
        cache_urls = [
            "/" + url_to_local_path(u, include_query=bool(urlparse(u).query))
            for u in self.captured.keys()
        ]
        cache_loader = f"""
// cache-loader.js — Pre-caches all downloaded assets
const URLS_TO_CACHE = {cache_urls!r};
caches.open('deep-offline-v1').then(cache => cache.addAll(URLS_TO_CACHE));
""".encode("utf-8")
        files["cache-loader.js"] = cache_loader

        return files

    async def crawl(self) -> dict:
        self._log(f"🚀 Deep crawl (Playwright): {self.start_url}")
        start_time = time.time()
        first_html = ""

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-web-security",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0.0.0 Safari/537.36"
                ),
                locale="id-ID",
                timezone_id="Asia/Jakarta",
                ignore_https_errors=True,
                extra_http_headers={
                    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"Windows"',
                },
            )
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
            """)

            page = await context.new_page()

            # 🕵️ Intercept ALL responses
            page.on("response", self._on_response)

            self._log("🌐 Opening browser and navigating...")
            try:
                resp = await page.goto(
                    self.start_url,
                    wait_until="networkidle",
                    timeout=60000,
                )
                if resp:
                    if resp.status == 403:
                        self._log(f"⛔ Akses Ditolak (HTTP 403 Forbidden). Website {self.base_domain} memproteksi halamannya dengan WAF / Anti-Bot.")
                    elif resp.status == 401:
                        self._log(f"⛔ Butuh Login/Otentikasi (HTTP 401 Unauthorized) pada {self.base_domain}.")
                    elif resp.status >= 400:
                        self._log(f"⚠️ Halaman target mengembalikan error HTTP {resp.status}.")
            except Exception as e:
                self._log(f"  ⚠️ Navigation warning (continuing): {e}")

            # Grab rendered HTML
            try:
                first_html = await page.content()
            except Exception:
                pass

            self._log(f"⏳ Waiting {self.wait_seconds}s for dynamic content to load...")
            await asyncio.sleep(self.wait_seconds)

            # Scroll + interact to trigger lazy 3D loading
            if self.scroll_pages:
                self._log("🖱️ Scrolling page to trigger lazy assets...")
                await self._scroll_and_interact(page)
                await asyncio.sleep(3)

            # Crawl iframes (e.g. Eyes on Solar System embed)
            self._log("🖼️ Checking iframes...")
            await self._crawl_iframes(page)

            # Final wait for any remaining requests
            await asyncio.sleep(5)

            await browser.close()

        elapsed = time.time() - start_time

        # Build local file map
        files = self._build_file_map()

        # Count 3D files
        threed_count = sum(
            1 for url in self.captured
            if Path(urlparse(url).path).suffix.lower() in THREED_EXTENSIONS
        )

        self._log(
            f"✅ Done! {len(files)} files captured "
            f"({threed_count} 3D assets), "
            f"{self.total_bytes // 1024}KB in {elapsed:.1f}s"
        )

        return {
            "html": first_html,
            "files": files,
            "stats": {
                "pages": 1,
                "assets": len(files),
                "total_kb": self.total_bytes // 1024,
                "elapsed": round(elapsed, 1),
                "threed_assets": threed_count,
            },
            "log": self.log,
        }
