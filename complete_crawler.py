"""
complete_crawler.py - Crawler that captures a site *completely*, including the
parts a normal mirror silently drops.

DeepCrawler records whatever the browser happens to request on one page load.
That misses four whole classes of asset, each of which is enough to leave the
saved copy blank:

  1. POST-rendered slots. Salesforce Commerce Cloud (and similar) fill sections
     like "Produk Baru" via XHR POST to a controller. A GET-only mirror saves an
     empty <div> and the section never appears offline. We replay the POST and
     inline the response into the HTML.

  2. Lazily-imported webpack chunks. main.js calls __webpack_require__.e(4353)
     only when a carousel or modal actually opens. If chunk 4353 is missing the
     bundle throws ChunkLoadError and the *entire page* stops rendering -- a
     white screen, not a degraded one. We parse .e(<id>) out of every saved
     bundle and fetch the chunks transitively until the graph closes.

  3. Assets referenced only from CSS (url(), @font-face, @import) and from
     srcset/data-* attributes, which are never requested at the viewport the
     crawler happened to use.

  4. Same-origin pages linked from the page, so the mirror has more than one
     screen (opt-in via max_pages).

It also writes serve.py next to the output: python -m http.server answers POST
with a 501 error *page*, and the site's own JS injects that HTML into the DOM,
which is where stray "ERROR RESPONSE" blocks in a mirror come from.
"""

import asyncio
import re
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from playwright.async_api import async_playwright, Page, Response

# Assets worth saving. Anything else (analytics beacons, tracking pixels) only
# bloats the mirror.
CAPTURE_EXTENSIONS = {
    ".html", ".htm", ".css", ".js", ".mjs", ".json", ".xml", ".txt",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".avif", ".bmp",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp4", ".webm", ".ogg", ".mp3", ".wav", ".m4a",
    ".glb", ".gltf", ".obj", ".mtl", ".fbx", ".dae",
    ".wasm", ".bin", ".map", ".pdf",
}

# Third-party hosts that can never work offline. Capturing them costs time and
# their absence changes nothing in the rendered page.
SKIP_HOSTS = (
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "facebook.net", "facebook.com", "connect.facebook.net",
    "cloudflareinsights.com", "hotjar.com", "clarity.ms",
    "cquotient.com", "demdex.net", "omtrdc.net", "adsrvr.org",
    "criteo.com", "bing.com", "tiktok.com", "snapchat.com",
)

SERVE_PY = '''#!/usr/bin/env python3
"""Static server for this mirror.

Plain http.server answers POST with a 501 error PAGE. Sites that fetch content
over POST inject that HTML into the DOM, which is where stray "ERROR RESPONSE"
blocks come from. Answer POST with an empty 200 instead -- the slot is already
inlined into the HTML at download time.
"""
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler


class MirrorHandler(SimpleHTTPRequestHandler):
    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Allow", "GET, HEAD, POST, OPTIONS")
        self.end_headers()

    def log_message(self, fmt, *args):
        msg = fmt % args
        if " 404 " in msg or " 501 " in msg:
            sys.stderr.write("%s\\n" % msg)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    print("Serving on http://localhost:%d/" % port)
    HTTPServer(("127.0.0.1", port), MirrorHandler).serve_forever()
'''


def normalize_url(url: str) -> str:
    return urlunparse(urlparse(url)._replace(fragment=""))


def url_to_local_path(url: str) -> str:
    """Map a URL onto a local file path, mirroring the server's own layout.

    Query strings are dropped rather than hashed into the filename: CDN image
    transforms (?sw=456&sh=608) would otherwise produce a different file per
    viewport for the same picture, and none of them at the path the HTML asks
    for once the query is stripped during rewrite.
    """
    parsed = urlparse(url)
    path = parsed.path.lstrip("/") or "index.html"
    if path.endswith("/"):
        path += "index.html"
    elif "." not in Path(path).name:
        path += "/index.html"
    return re.sub(r'[<>:"|?*\\]', "_", path)


def should_capture(url: str) -> bool:
    if url.startswith(("data:", "blob:", "about:")):
        return False
    host = urlparse(url).netloc.lower()
    if any(h in host for h in SKIP_HOSTS):
        return False
    return True


class CompleteCrawler:
    def __init__(
        self,
        start_url: str,
        wait_seconds: int = 10,
        max_pages: int = 1,
        follow_links: bool = False,
        progress_cb: Optional[Callable] = None,
    ):
        self.start_url = start_url.rstrip("/")
        parsed = urlparse(self.start_url)
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.host = parsed.netloc

        self.wait_seconds = wait_seconds
        self.max_pages = max_pages
        self.follow_links = follow_links
        self.progress_cb = progress_cb

        self.captured: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        # slot div id -> HTML the page filled it with at runtime
        self.post_slots: dict[str, str] = {}
        self.seen_posts: list[str] = []
        self.log: list[str] = []
        self.total_bytes = 0
        self.stats = {
            "post_slots": 0,
            "chunks": 0,
            "css_assets": 0,
            "referenced": 0,
            "pages": 0,
        }

    def _log(self, msg: str):
        self.log.append(msg)
        if self.progress_cb:
            self.progress_cb(msg)

    # ── capture ──────────────────────────────────────────────────────────────

    async def _on_response(self, response: Response):
        url = response.url
        if response.status == 0 or response.status >= 400:
            return
        if not should_capture(url):
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

        self.captured[norm] = body
        self.content_types[norm] = response.headers.get("content-type", "")
        self.total_bytes += len(body)

        size_kb = len(body) // 1024
        if size_kb > 50:
            self._log(f"  📦 {size_kb}KB  {url[:76]}")

    # ── 1. POST-rendered slots ───────────────────────────────────────────────

    def _watch_posts(self, page: Page):
        """Record same-origin POSTs so the log can report what filled the slots.

        Registered before navigation: the POSTs that build the page fire during
        goto(), so a listener attached afterwards sees none of them.
        """
        def on_request(req):
            if req.method == "POST" and urlparse(req.url).netloc == self.host:
                self.seen_posts.append(req.url)

        page.on("request", on_request)

    async def _wait_for_slots(self, page: Page, extra_seconds: int = 20):
        """Poll until the runtime-filled containers stop appearing.

        These arrive by XHR whenever the recommendation service answers, which
        is not tied to networkidle: a fixed sleep catches them on one run and
        misses them on the next, producing a mirror whose product sections are
        silently empty. Poll instead, and stop as soon as the count settles.
        """
        stable = 0
        for _ in range(extra_seconds):
            before = len(self.post_slots)
            await self._capture_post_slots(page)
            if len(self.post_slots) == before:
                stable += 1
                # Two quiet seconds after something arrived means it is done.
                if stable >= 2 and self.post_slots:
                    return
            else:
                stable = 0
            await asyncio.sleep(1)

    async def _capture_post_slots(self, page: Page):
        """Read back containers the page filled at runtime.

        The saved HTML has only an empty <div> for these; the markup arrives by
        XHR. Reading the live DOM is more faithful than replaying the POST,
        which can return different products on a second call.

        This must not be gated on having observed the POST: by the time it runs
        the request is long finished, and on a cached load it may not happen at
        all while the slot is still filled.
        """
        try:
            slots = await page.evaluate(
                """() => {
                    const out = {};
                    document.querySelectorAll('[id]').forEach(el => {
                        const html = el.innerHTML.trim();
                        // A container the page filled at runtime: has an id,
                        // holds real markup, and is not a page-level wrapper.
                        if (html.length > 500 && el.children.length > 0 &&
                            !['BODY','HTML','HEAD'].includes(el.tagName) &&
                            /recomm|slot|carousel|product|recommendation/i.test(el.id)) {
                            out[el.id] = html;
                        }
                    });
                    return out;
                }"""
            )
        except Exception as e:
            self._log(f"  ⚠️ slot read failed: {e}")
            return

        # Keep only containers the *saved* HTML leaves empty. Anything already
        # present in the served markup would be duplicated by inlining it.
        for slot_id, html in (slots or {}).items():
            if slot_id in self.post_slots:
                continue
            self.post_slots[slot_id] = html
            self._log(f"  ✅ slot #{slot_id} ({len(html)//1024}KB)")
        self.stats["post_slots"] = len(self.post_slots)

    # ── 2. webpack chunks ────────────────────────────────────────────────────

    async def _fetch_webpack_chunks(self, client: httpx.AsyncClient):
        """Fetch lazily-imported chunks until the dependency graph closes.

        One missing chunk raises ChunkLoadError, which aborts the whole bundle
        and leaves a blank page -- so this has to reach a fixed point, not just
        pick up the chunks that happened to load.
        """
        js_urls = [u for u in self.captured if u.endswith(".js")]
        if not js_urls:
            return

        # Chunks live beside the bundle that imports them.
        base_dir = None
        for u in js_urls:
            if "/main" in u or "/app" in u or "/index" in u:
                base_dir = u.rsplit("/", 1)[0]
                break
        if base_dir is None:
            base_dir = max(js_urls, key=lambda u: len(self.captured[u])).rsplit("/", 1)[0]

        fetched = 0
        for round_no in range(1, 11):
            ids: set[str] = set()
            for url, body in list(self.captured.items()):
                if not url.endswith(".js"):
                    continue
                try:
                    js = body.decode("utf-8", errors="replace")
                except Exception:
                    continue
                ids |= set(re.findall(r"\.e\((\d{1,5})\)", js))
                for arr in re.findall(r"\.e\(\[([\d,]+)\]\)", js):
                    ids |= {i for i in arr.split(",") if i}

            missing = [
                i for i in ids
                if normalize_url(f"{base_dir}/{i}.js") not in self.captured
            ]
            if not missing:
                if round_no == 1:
                    self._log("🧩 No missing webpack chunks")
                else:
                    self._log(f"🧩 Chunk graph closed after {round_no - 1} round(s)")
                break

            self._log(f"🧩 Round {round_no}: fetching {len(missing)} chunk(s)")
            results = await asyncio.gather(
                *(self._fetch(client, f"{base_dir}/{i}.js") for i in missing),
                return_exceptions=True,
            )
            got = sum(1 for r in results if r is True)
            fetched += got
            if got == 0:
                self._log("  ⚠️ none resolved; stopping chunk discovery")
                break

        self.stats["chunks"] = fetched

    # ── 3. CSS / srcset assets ───────────────────────────────────────────────

    async def _fetch_css_assets(self, client: httpx.AsyncClient):
        """Fetch url(), @import and @font-face targets from captured CSS.

        These are requested only when a rule actually matches, so a single
        viewport load never pulls them all.
        """
        targets: set[str] = set()
        for url, body in list(self.captured.items()):
            ct = self.content_types.get(url, "")
            if not (url.endswith(".css") or "css" in ct):
                continue
            try:
                css = body.decode("utf-8", errors="replace")
            except Exception:
                continue
            for ref in re.findall(r'url\(\s*["\']?([^"\')]+)["\']?\s*\)', css):
                if ref.startswith(("data:", "#")):
                    continue
                targets.add(urljoin(url, ref.strip()))
            for ref in re.findall(r'@import\s+["\']([^"\']+)["\']', css):
                targets.add(urljoin(url, ref.strip()))

        todo = [
            t for t in targets
            if normalize_url(t) not in self.captured
            and should_capture(t)
            and Path(urlparse(t).path).suffix.lower() in CAPTURE_EXTENSIONS
        ]
        if not todo:
            return

        self._log(f"🎨 Fetching {len(todo)} asset(s) referenced only from CSS")
        results = await asyncio.gather(
            *(self._fetch(client, t) for t in todo), return_exceptions=True
        )
        # Accumulate: this runs again after the chunks bring in more CSS, and
        # assigning would discard the first pass's count.
        self.stats["css_assets"] += sum(1 for r in results if r is True)

    async def _fetch_html_assets(self, client: httpx.AsyncClient, html: str, page_url: str):
        """Fetch srcset candidates and data-* image URLs the browser skipped."""
        targets: set[str] = set()
        for ss in re.findall(r'srcset=["\']([^"\']+)["\']', html):
            for cand in ss.split(","):
                u = cand.strip().split()[0] if cand.strip() else ""
                if u and not u.startswith("data:"):
                    targets.add(urljoin(page_url, u))
        for attr in re.findall(r'data-(?:src|bg|image|lazy)=["\']([^"\']+)["\']', html):
            if not attr.startswith("data:"):
                targets.add(urljoin(page_url, attr))

        todo = [
            t for t in targets
            if normalize_url(t) not in self.captured
            and should_capture(t)
            and Path(urlparse(t).path).suffix.lower() in CAPTURE_EXTENSIONS
        ]
        if not todo:
            return
        self._log(f"🖼️ Fetching {len(todo)} lazy/srcset image(s)")
        await asyncio.gather(
            *(self._fetch(client, t) for t in todo), return_exceptions=True
        )

    async def _fetch_slot_assets(self, client: httpx.AsyncClient):
        """Fetch images referenced only inside the captured slot markup.

        The browser requests a slot's images at its own viewport, so the srcset
        candidates the inlined HTML actually points at are often never loaded.
        Without this the section renders with broken images.
        """
        if not self.post_slots:
            return
        targets: set[str] = set()
        for frag in self.post_slots.values():
            for attr in re.findall(r'(?:src|data-src)=["\']([^"\']+)["\']', frag):
                if not attr.startswith(("data:", "#")):
                    targets.add(urljoin(self.start_url, attr))
            for ss in re.findall(r'srcset=["\']([^"\']+)["\']', frag):
                for cand in ss.split(","):
                    u = cand.strip().split()[0] if cand.strip() else ""
                    if u and not u.startswith("data:"):
                        targets.add(urljoin(self.start_url, u))

        todo = [
            t for t in targets
            if normalize_url(t) not in self.captured
            and should_capture(t)
            and Path(urlparse(t).path).suffix.lower() in CAPTURE_EXTENSIONS
        ]
        if not todo:
            return
        self._log(f"🛍️ Fetching {len(todo)} image(s) used by dynamic slots")
        await asyncio.gather(
            *(self._fetch(client, t) for t in todo), return_exceptions=True
        )

    async def _fetch_referenced(self, client: httpx.AsyncClient):
        """Close the gap between what the HTML asks for and what was captured.

        A browser only requests what its own viewport and interactions reach:
        mega-menu art behind an unopened dropdown, banners for other
        breakpoints, icons in a collapsed footer. The saved HTML still points
        at all of them, so every one is a broken image offline. Walk the saved
        markup instead of the network log, and fetch whatever is still missing.
        Round count is generous because each fetched page brings its own
        references; the loop exits early once a round resolves nothing.
        """
        tried: set[str] = set()
        for round_no in range(1, 8):
            targets: set[str] = set()
            for url, body in list(self.captured.items()):
                ct = self.content_types.get(url, "")
                if not (url.endswith((".html", ".htm")) or "text/html" in ct):
                    continue
                try:
                    html = body.decode("utf-8", errors="replace")
                except Exception:
                    continue
                for ref in re.findall(
                    r'(?:src|href|data-src|data-bg|data-image|content)='
                    r'["\']([^"\']+\.(?:jpe?g|png|gif|webp|avif|svg|ico|css|js|woff2?|ttf))[^"\']*["\']',
                    html, re.I,
                ):
                    if not ref.startswith(("data:", "#", "mailto:", "tel:")):
                        targets.add(urljoin(url, ref))
                for ss in re.findall(r'srcset=["\']([^"\']+)["\']', html, re.I):
                    for cand in ss.split(","):
                        u = cand.strip().split()[0] if cand.strip() else ""
                        if u and not u.startswith("data:"):
                            targets.add(urljoin(url, u))

            todo = [
                t for t in targets
                if normalize_url(t.split("?")[0]) not in self.captured
                and t not in tried
                and should_capture(t)
            ]
            if not todo:
                if round_no == 1:
                    self._log("🔗 No unreferenced assets missing")
                break
            tried.update(todo)

            self._log(f"🔗 Round {round_no}: fetching {len(todo)} asset(s) "
                      f"referenced by saved HTML")
            results = await asyncio.gather(
                *(self._fetch(client, t) for t in todo), return_exceptions=True
            )
            got = sum(1 for r in results if r is True)
            self.stats["referenced"] += got
            if got == 0:
                break

    async def _fetch(self, client: httpx.AsyncClient, url: str) -> bool:
        norm = normalize_url(url)
        if norm in self.captured:
            return False
        try:
            r = await client.get(url, timeout=30.0, follow_redirects=True)
            if r.status_code != 200 or not r.content:
                return False
            self.captured[norm] = r.content
            self.content_types[norm] = r.headers.get("content-type", "")
            self.total_bytes += len(r.content)
            return True
        except Exception:
            return False

    # ── rewriting ────────────────────────────────────────────────────────────

    def _rewrite(self, text: str) -> str:
        """Point absolute URLs at the mirror and strip CDN transform queries."""
        text = text.replace(f"{self.origin}/", "/")
        text = text.replace(f"{self.origin}\\/", "\\/")
        # /path/img.jpg?sw=456&sh=608 -> /path/img.jpg, matching how the file
        # was saved on disk.
        text = re.sub(
            r'(/[^\s"\'<>()]+\.(?:jpe?g|png|gif|webp|avif|svg|css|js|woff2?|ttf))\?[^\s"\'<>()]*',
            r"\1",
            text,
        )
        return text

    def _inline_post_slots(self, html: str) -> tuple[str, int]:
        """Write each captured slot's markup into its empty container.

        Only empty containers are filled. A container the served HTML already
        populates needs nothing, and injecting there would duplicate it.
        """
        filled = 0
        for slot_id, frag in self.post_slots.items():
            pattern = re.compile(
                r'(<(?:div|section)[^>]*\bid=["\']' + re.escape(slot_id)
                + r'["\'][^>]*>)\s*(</(?:div|section)>)',
                re.I,
            )
            if not pattern.search(html):
                continue
            html = pattern.sub(
                lambda m: m.group(1) + self._rewrite(frag) + m.group(2), html, count=1
            )
            filled += 1
        return html, filled

    def _build_files(self) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        inlined = 0
        for url, body in self.captured.items():
            ct = self.content_types.get(url, "")
            ext = Path(urlparse(url).path).suffix.lower()
            path = url_to_local_path(url)

            if ext in {".html", ".htm"} or "text/html" in ct:
                try:
                    text = body.decode("utf-8", errors="replace")
                    text, n = self._inline_post_slots(text)
                    inlined += n
                    text = self._rewrite(text)
                    body = text.encode("utf-8")
                except Exception:
                    pass
            elif ext in {".js", ".mjs", ".css", ".json"} or "javascript" in ct or "css" in ct:
                try:
                    body = self._rewrite(body.decode("utf-8", errors="replace")).encode("utf-8")
                except Exception:
                    pass

            files[path] = body

        # Report slots actually written into the HTML, not merely observed.
        self.stats["post_slots"] = inlined
        if inlined:
            self._log(f"📝 Inlined {inlined} dynamic slot(s) into saved HTML")

        files["serve.py"] = SERVE_PY.encode("utf-8")
        return files

    # ── driver ───────────────────────────────────────────────────────────────

    async def crawl(self) -> dict:
        self._log(f"🚀 Complete crawl: {self.start_url}")
        started = time.time()
        first_html = ""

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-web-security"],
            )
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/140.0.0.0 Safari/537.36"),
                ignore_https_errors=True,
            )
            page = await context.new_page()
            page.on("response", self._on_response)
            self._watch_posts(page)

            self._log("🌐 Loading page...")
            try:
                await page.goto(self.start_url, wait_until="networkidle", timeout=60000)
            except Exception as e:
                self._log(f"  ⚠️ navigation: {e}")

            self._log(f"⏳ Waiting {self.wait_seconds}s for dynamic content...")
            await asyncio.sleep(self.wait_seconds)
            if self.seen_posts:
                self._log(f"🔁 Page made {len(self.seen_posts)} same-origin POST(s)")
            await self._wait_for_slots(page)

            self._log("🖱️ Scrolling to trigger lazy content...")
            try:
                for pct in (0.25, 0.5, 0.75, 1.0):
                    await page.evaluate(
                        f"window.scrollTo(0, document.body.scrollHeight * {pct})"
                    )
                    await asyncio.sleep(1.5)
                await page.evaluate("window.scrollTo(0, 0)")
                await asyncio.sleep(2)
            except Exception:
                pass

            # Open carousels and tabs: these are exactly what trigger the lazy
            # chunk imports that a plain load never requests.
            try:
                for sel in ("[class*=carousel] button", "[class*=slider] button",
                            "[class*=next]", "[role=tab]"):
                    for el in (await page.query_selector_all(sel))[:3]:
                        try:
                            await el.click(timeout=2000)
                            await asyncio.sleep(0.8)
                        except Exception:
                            pass
            except Exception:
                pass

            # Re-read slots: interaction may have filled more of them.
            await self._wait_for_slots(page, extra_seconds=8)
            if self.post_slots:
                self._log(f"🔁 Captured {len(self.post_slots)} runtime-filled slot(s)")

            try:
                first_html = await page.content()
            except Exception:
                pass

            links: list[str] = []
            if self.follow_links and self.max_pages > 1:
                try:
                    hrefs = await page.evaluate(
                        "() => [...document.querySelectorAll('a[href]')].map(a => a.href)"
                    )
                    seen = {normalize_url(self.start_url)}
                    for h in hrefs:
                        n = normalize_url(h)
                        if (urlparse(n).netloc == self.host and n not in seen
                                and "#" not in h):
                            seen.add(n)
                            links.append(n)
                        if len(links) >= self.max_pages - 1:
                            break
                except Exception:
                    pass

            for link in links:
                self._log(f"📄 Page: {link[:76]}")
                try:
                    p2 = await context.new_page()
                    p2.on("response", self._on_response)
                    await p2.goto(link, wait_until="networkidle", timeout=45000)
                    await asyncio.sleep(3)
                    await p2.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await asyncio.sleep(2)
                    await p2.close()
                    self.stats["pages"] += 1
                except Exception as e:
                    self._log(f"  ⚠️ {e}")

            await asyncio.sleep(3)
            await browser.close()

        self.stats["pages"] += 1

        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/140.0.0.0 Safari/537.36"),
            "Referer": self.start_url,
        }
        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            await self._fetch_webpack_chunks(client)
            await self._fetch_slot_assets(client)
            await self._fetch_css_assets(client)
            if first_html:
                await self._fetch_html_assets(client, first_html, self.start_url)
            # New CSS may have arrived with the chunks.
            await self._fetch_css_assets(client)
            # Last: anything the saved HTML still points at but nothing fetched.
            await self._fetch_referenced(client)

        files = self._build_files()
        elapsed = time.time() - started

        self._log(
            f"✅ {len(files)} files, {self.total_bytes // 1024}KB in {elapsed:.1f}s "
            f"({self.stats['post_slots']} POST slots, {self.stats['chunks']} chunks, "
            f"{self.stats['css_assets']} CSS assets, "
            f"{self.stats['referenced']} HTML-referenced)"
        )

        return {
            "html": first_html,
            "files": files,
            "stats": {
                "pages": self.stats["pages"],
                "assets": len(files),
                "total_kb": self.total_bytes // 1024,
                "elapsed": round(elapsed, 1),
                "post_slots": self.stats["post_slots"],
                "webpack_chunks": self.stats["chunks"],
                "css_assets": self.stats["css_assets"],
                "referenced_assets": self.stats["referenced"],
            },
            "log": self.log,
        }
