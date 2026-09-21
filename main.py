"""
main.py - FastAPI Web Application for Website Frontend Downloader
"""
import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import (
    HTMLResponse, StreamingResponse, JSONResponse, FileResponse
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, HttpUrl

from detector import detect_framework
from crawler import WebCrawler, build_zip
from deep_crawler import DeepCrawler
from complete_crawler import CompleteCrawler

# ─── App setup ───────────────────────────────────────────────────────────────

app = FastAPI(title="Web Frontend Downloader", version="1.0.0")

# Mount static files (CSS, JS)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Templates
templates = Jinja2Templates(directory="templates")

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# In-memory job store  {job_id: {...}}
jobs: dict[str, dict] = {}


# ─── Models ──────────────────────────────────────────────────────────────────

class CrawlRequest(BaseModel):
    url: str
    max_pages: int = 30
    max_assets: int = 300
    deep_mode: bool = False       # use Playwright to capture 3D/WebGL assets
    wait_seconds: int = 15        # seconds to wait for dynamic content
    crawl_iframes: bool = True    # also crawl embedded iframes (e.g. Eyes on Solar System)
    complete_mode: bool = False   # POST slots + webpack chunks + CSS assets
    follow_links: bool = False    # in complete mode, also mirror linked same-origin pages


# ─── Background crawl task ───────────────────────────────────────────────────

async def run_crawl(job_id: str, url: str, max_pages: int, max_assets: int):
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

        # Detect framework
        detection = detect_framework(result["html"], result["headers"])
        job["detection"] = detection

        # Build ZIP
        parsed_host = re.sub(r"[^a-zA-Z0-9_\-]", "_", url.split("//")[-1].split("/")[0])
        zip_bytes = build_zip(result["files"], parsed_host)

        # Save ZIP
        zip_filename = f"{job_id}.zip"
        zip_path = DOWNLOAD_DIR / zip_filename
        zip_path.write_bytes(zip_bytes)

        job["status"] = "done"
        job["stats"] = result["stats"]
        job["zip_filename"] = zip_filename
        job["zip_size_kb"] = len(zip_bytes) // 1024
        job["log"] = result["log"]

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["log"].append(f"💥 Error: {e}")


async def run_deep_crawl(
    job_id: str,
    url: str,
    wait_seconds: int,
    crawl_iframes: bool,
):
    """Deep crawl using Playwright — captures 3D/WebGL assets and iframes."""
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
        # Override iframe behaviour
        crawler.crawl_iframes_enabled = crawl_iframes

        result = await crawler.crawl()

        # Detect framework from rendered HTML
        detection = detect_framework(result["html"], {})
        job["detection"] = detection

        # Build ZIP from captured files
        import zipfile, io as _io
        parsed_host = re.sub(r"[^a-zA-Z0-9_\-]", "_", url.split("//")[-1].split("/")[0])
        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for local_path, content in result["files"].items():
                zf.writestr(f"{parsed_host}/{local_path}", content)
        zip_bytes = buf.getvalue()

        zip_filename = f"{job_id}.zip"
        zip_path = DOWNLOAD_DIR / zip_filename
        zip_path.write_bytes(zip_bytes)

        job["status"] = "done"
        job["stats"] = result["stats"]
        job["zip_filename"] = zip_filename
        job["zip_size_kb"] = len(zip_bytes) // 1024
        job["log"] = result["log"]

    except Exception as e:
        import traceback
        job["status"] = "error"
        job["error"] = str(e)
        job["log"].append(f"💥 Error: {e}")
        job["log"].append(traceback.format_exc())


async def run_complete_crawl(
    job_id: str,
    url: str,
    wait_seconds: int,
    max_pages: int,
    follow_links: bool,
):
    """Complete crawl - POST-rendered slots, lazy webpack chunks, CSS assets."""
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
        job["log"].append(f"\U0001f4a5 Error: {e}")
        job["log"].append(traceback.format_exc())


# ─── API Endpoints ────────────────────────────────────────────────────────────

@app.post("/api/crawl")
async def start_crawl(req: CrawlRequest, background_tasks: BackgroundTasks):
    # Validate URL
    url = req.url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "id": job_id,
        "url": url,
        "mode": ("complete" if req.complete_mode
                 else "deep" if req.deep_mode else "normal"),
        "status": "queued",
        "log": [],
        "detection": None,
        "stats": None,
        "zip_filename": None,
    }

    if req.complete_mode:
        background_tasks.add_task(
            run_complete_crawl, job_id, url, req.wait_seconds,
            req.max_pages, req.follow_links,
        )
    elif req.deep_mode:
        background_tasks.add_task(
            run_deep_crawl, job_id, url, req.wait_seconds, req.crawl_iframes
        )
    else:
        background_tasks.add_task(run_crawl, job_id, url, req.max_pages, req.max_assets)

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
    zip_path = DOWNLOAD_DIR / job["zip_filename"]
    if not zip_path.exists():
        raise HTTPException(404, "ZIP file not found")

    site = re.sub(r"[^a-zA-Z0-9_\-]", "_", job["url"].split("//")[-1].split("/")[0])
    return FileResponse(
        path=str(zip_path),
        media_type="application/zip",
        filename=f"{site}_frontend.zip",
    )


# ─── Web UI ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse(request, "index.html")

