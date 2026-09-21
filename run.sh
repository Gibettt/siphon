#!/bin/bash
cd "$(dirname "$0")"
echo "🌐 Web Frontend Downloader"
echo "📡 Buka browser: http://localhost:8765"
python3 -m uvicorn main:app --host 0.0.0.0 --port 8765 --reload
