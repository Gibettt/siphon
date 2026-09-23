"""
main.py - FastAPI Web Application for Website Frontend Downloader
Compatible with both local server and Vercel serverless deployment.
"""
import asyncio
import base64
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import (
    HTMLResponse, StreamingResponse, JSONResponse, FileResponse, Response
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, HttpUrl

from detector import detect_framework
from crawler import WebCrawler, build_zip

# Detect if running on Vercel (serverless)
IS_VERCEL = bool(os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"))

# Only import heavy crawlers if not on Vercel
if not IS_VERCEL:
    from deep_crawler import DeepCrawler
    from complete_crawler import CompleteCrawler

# ─── App setup ───────────────────────────────────────────────────────────────

app = FastAPI(title="Siphon - Web Frontend Downloader", version="1.0.0")

# Mount static files
STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# ─── Storage ─────────────────────────────────────────────────────────────────
# On Vercel: in-memory only (lost between requests, so we return ZIP inline)
# Locally: disk + in-memory job tracking

DOWNLOAD_DIR = Path("/tmp/downloads") if IS_VERCEL else Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# In-memory stores
jobs: dict[str, dict] = {}
zip_store: dict[str, bytes] = {}   # job_id -> zip bytes (for Vercel)


# ─── Models ──────────────────────────────────────────────────────────────────

class CrawlRequest(BaseModel):
    url: str
    max_pages: int = 30
    max_assets: int = 300
    deep_mode: bool = False
    wait_seconds: int = 15
    crawl_iframes: bool = True
    complete_mode: bool = False
    follow_links: bool = False


# ─── Core crawl logic ────────────────────────────────────────────────────────

async def do_crawl(job_id: str, url: str, max_pages: int, max_assets: int):
    """Run a normal crawl and populate the job dict."""
    job = jobs[job_id]
    job["status"] = "running"
    job["log"] = []
    job["started_at"] = time.time()

    def progress(msg: str):
        job["log"].append(msg)

    try:
        crawler = WebCrawler(
            start_url=url,
            max_pages=max_pages,
            max_assets=max_assets,
            progress_cb=progress,
        )
        result = await crawler.crawl()

        detection = detect_framework(result["html"], result["headers"])
        job["detection"] = detection

        parsed_host = re.sub(r"[^a-zA-Z0-9_\-]", "_", url.split("//")[-1].split("/")[0])
        zip_bytes = build_zip(result["files"], parsed_host)

        # Store ZIP
        zip_filename = f"{job_id}.zip"
        if IS_VERCEL:
            zip_store[job_id] = zip_bytes
        else:
            (DOWNLOAD_DIR / zip_filename).write_bytes(zip_bytes)

        job["status"] = "done"
        job["stats"] = result["stats"]
        job["zip_filename"] = zip_filename
        job["zip_size_kb"] = len(zip_bytes) // 1024
        job["log"] = result["log"]

    except Exception as e:
        import traceback
        job["status"] = "error"
        job["error"] = str(e)
        job["log"].append(f"Error: {e}")
        job["log"].append(traceback.format_exc())


async def do_deep_crawl(job_id: str, url: str, wait_seconds: int, crawl_iframes: bool):
    """Deep crawl using Playwright (local only)."""
    job = jobs[job_id]
    job["status"] = "running"
    job["log"] = []
    job["started_at"] = time.time()

    def progress(msg: str):
        job["log"].append(msg)

    try:
        crawler = DeepCrawler(
            start_url=url,
            wait_seconds=wait_seconds,
            scroll_pages=True,
            interact_3d=True,
            cross_origin=True,
            progress_cb=progress,
        )
        crawler.crawl_iframes_enabled = crawl_iframes
        result = await crawler.crawl()

        job["detection"] = detect_framework(result["html"], {})

        import zipfile, io as _io
        parsed_host = re.sub(r"[^a-zA-Z0-9_\-]", "_", url.split("//")[-1].split("/")[0])
        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for local_path, content in result["files"].items():
                zf.writestr(f"{parsed_host}/{local_path}", content)
        zip_bytes = buf.getvalue()

        zip_filename = f"{job_id}.zip"
        (DOWNLOAD_DIR / zip_filename).write_bytes(zip_bytes)

        job["status"] = "done"
        job["stats"] = result["stats"]
        job["zip_filename"] = zip_filename
        job["zip_size_kb"] = len(zip_bytes) // 1024
        job["log"] = result["log"]

    except Exception as e:
        import traceback
        job["status"] = "error"
        job["error"] = str(e)
        job["log"].append(f"Error: {e}")
        job["log"].append(traceback.format_exc())


async def do_complete_crawl(job_id: str, url: str, wait_seconds: int, max_pages: int, follow_links: bool):
    """Complete crawl (local only)."""
    job = jobs[job_id]
    job["status"] = "running"
    job["log"] = []
    job["started_at"] = time.time()

    def progress(msg: str):
        job["log"].append(msg)

    try:
        crawler = CompleteCrawler(
            start_url=url,
            wait_seconds=wait_seconds,
            max_pages=max_pages,
            follow_links=follow_links,
            progress_cb=progress,
        )
        result = await crawler.crawl()

        job["detection"] = detect_framework(result["html"], {})

        import zipfile, io as _io
        host = re.sub(r"[^a-zA-Z0-9_\-]", "_", url.split("//")[-1].split("/")[0])
        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for local_path, content in result["files"].items():
                zf.writestr(f"{host}/{local_path}", content)
        zip_bytes = buf.getvalue()

        zip_filename = f"{job_id}.zip"
        (DOWNLOAD_DIR / zip_filename).write_bytes(zip_bytes)

        job["status"] = "done"
        job["stats"] = result["stats"]
        job["zip_filename"] = zip_filename
        job["zip_size_kb"] = len(zip_bytes) // 1024
        job["log"] = result["log"]

    except Exception as e:
        import traceback
        job["status"] = "error"
        job["error"] = str(e)
        job["log"].append(f"Error: {e}")
        job["log"].append(traceback.format_exc())


# ─── API Endpoints ────────────────────────────────────────────────────────────

@app.post("/api/crawl")
async def start_crawl(req: CrawlRequest, background_tasks: BackgroundTasks):
    url = req.url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    # On Vercel: force normal mode, limit pages/assets for timeout safety
    force_normal = IS_VERCEL and (req.deep_mode or req.complete_mode)
    if IS_VERCEL:
        max_pages = min(req.max_pages, 5)
        max_assets = min(req.max_assets, 100)
    else:
        max_pages = req.max_pages
        max_assets = req.max_assets

    actual_mode = "normal" if (IS_VERCEL and force_normal) else (
        "complete" if req.complete_mode else "deep" if req.deep_mode else "normal"
    )

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "id": job_id,
        "url": url,
        "mode": actual_mode,
        "status": "queued",
        "log": [],
        "detection": None,
        "stats": None,
        "zip_filename": None,
        "forced_normal": force_normal,
    }

    if IS_VERCEL:
        # Vercel: run synchronously (no background tasks)
        await do_crawl(job_id, url, max_pages, max_assets)
        return jobs[job_id]
    else:
        # Local: run in background
        if req.complete_mode:
            background_tasks.add_task(
                do_complete_crawl, job_id, url, req.wait_seconds,
                max_pages, req.follow_links,
            )
        elif req.deep_mode:
            background_tasks.add_task(
                do_deep_crawl, job_id, url, req.wait_seconds, req.crawl_iframes
            )
        else:
            background_tasks.add_task(do_crawl, job_id, url, max_pages, max_assets)
        return {"job_id": job_id}


@app.get("/api/job/{job_id}")
async def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/api/download/{job_id}")
async def download_zip(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] != "done":
        raise HTTPException(400, "Job not finished yet")

    site = re.sub(r"[^a-zA-Z0-9_\-]", "_", job["url"].split("//")[-1].split("/")[0])
    filename = f"{site}_frontend.zip"

    # Try in-memory store first (Vercel), then disk
    if job_id in zip_store:
        return Response(
            content=zip_store[job_id],
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    zip_path = DOWNLOAD_DIR / job["zip_filename"]
    if not zip_path.exists():
        raise HTTPException(404, "ZIP file not found")
    return FileResponse(path=str(zip_path), media_type="application/zip", filename=filename)


# ─── Web UI ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse(request, "index.html")
