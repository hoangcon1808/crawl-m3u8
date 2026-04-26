import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from flask import Flask, Response, request, send_from_directory
from dotenv import load_dotenv

# Xác định thư mục gốc của dự án
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Import các hàm từ crawl_to_m3u.py
try:
    from crawl_to_m3u import MAX_MATCHES, START_URL, crawl, merge_crawls
except ImportError:
    # Giá trị dự phòng nếu không tìm thấy file crawl
    MAX_MATCHES = 100
    START_URL = "https://hoadaotv.info/"
    def crawl(**kwargs): return {"json": [], "m3u": "#EXTM3U", "stats": {}}
    def merge_crawls(*args, **kwargs): return {"json": [], "m3u": "#EXTM3U", "stats": {}}

app = Flask(__name__)
load_dotenv() # Load .env nếu chạy local

# --- CẤU HÌNH HỆ THỐNG ---
MAX_STORAGE_UPLOAD_BYTES = 8 * 1024 * 1024

def get_cors_headers(is_cache=True):
    headers = {"Access-Control-Allow-Origin": "*"}
    if is_cache:
        headers["Cache-Control"] = "public, s-maxage=60, stale-while-revalidate=120"
    else:
        headers["Cache-Control"] = "no-store, no-cache, must-revalidate, post-check=0, pre-check=0"
    return headers

# --- SUPABASE UTILITIES ---

def get_supabase_env():
    return {
        "url": (os.getenv("SUPABASE_URL") or "").strip().rstrip("/"),
        "key": (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY") or "").strip(),
        "bucket": (os.getenv("SUPABASE_STORAGE_BUCKET") or "link").strip(),
        "is_public": os.getenv("SUPABASE_PUBLIC_BUCKET", "true").lower() in ["true", "1", "yes"],
        "upload_dir": (os.getenv("SUPABASE_UPLOAD_DIR") or "").strip("/")
    }

def ensure_bucket_exists():
    """Tự động kiểm tra và tạo Bucket nếu chưa tồn tại"""
    env = get_supabase_env()
    if not env["url"] or not env["key"]:
        return env["bucket"]
        
    headers = {"apikey": env["key"], "Authorization": f"Bearer {env['key']}"}
    try:
        # Kiểm tra danh sách bucket hiện có
        res = requests.get(f"{env['url']}/storage/v1/bucket", headers=headers, timeout=5)
        if res.status_code == 200:
            buckets = res.json()
            if not any(b.get("id") == env["bucket"] for b in buckets):
                # Tạo mới nếu không tìm thấy
                requests.post(
                    f"{env['url']}/storage/v1/bucket", 
                    headers=headers, 
                    json={"id": env["bucket"], "name": env["bucket"], "public": env["is_public"]},
                    timeout=5
                )
    except Exception as e:
        print(f"Lỗi kiểm tra bucket: {e}")
    return env["bucket"]

def generate_storage_url(object_path):
    """Tạo link trả về (Public URL hoặc Signed URL)"""
    env = get_supabase_env()
    if env["is_public"]:
        return f"{env['url']}/storage/v1/object/public/{quote(env['bucket'])}/{quote(object_path, safe='/')}"
    
    # Logic tạo Signed URL cho Private Bucket
    headers = {"apikey": env["key"], "Authorization": f"Bearer {env['key']}"}
    expires = int(os.getenv("SUPABASE_SIGNED_URL_EXPIRES", "3600"))
    res = requests.post(
        f"{env['url']}/storage/v1/object/sign/{quote(env['bucket'])}/{quote(object_path, safe='/')}",
        headers=headers,
        json={"expiresIn": expires},
        timeout=10
    )
    if res.status_code == 200:
        return f"{env['url']}/storage/v1{res.json().get('signedURL')}"
    return ""

# --- API ROUTES ---

def finalize_text_output(data):
    """CHỐT: Chuyển đổi list hoặc dữ liệu thô sang chuỗi văn bản sạch để fix lỗi hiển thị []"""
    if isinstance(data, list):
        return "\n".join(str(item) for item in data)
    return str(data or "")

@app.route("/api/crawl", methods=["GET", "OPTIONS"])
def route_crawl():
    if request.method == "OPTIONS":
        return Response(status=204, headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET"})
    
    source = request.args.get("link") or request.args.get("url") or START_URL
    fmt = request.args.get("format", "json").lower()
    
    try:
        result = crawl(max_matches=100, source_url=source)
        if fmt == "m3u":
            content = finalize_text_output(result.get("m3u", "#EXTM3U"))
            return Response(content, content_type="text/plain; charset=utf-8", headers=get_cors_headers())
        
        return Response(json.dumps(result.get("json", []), ensure_ascii=False), 
                        content_type="application/json", headers=get_cors_headers())
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

@app.route("/api/merge", methods=["GET", "POST", "OPTIONS"])
def route_merge():
    if request.method == "OPTIONS":
        return Response(status=204, headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST"})
    
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        links = payload.get("links") or [payload.get("link")]
        fmt = str(payload.get("format", "json")).lower()
    else:
        links = request.args.getlist("link") or request.args.get("links", "").split()
        fmt = request.args.get("format", "json").lower()

    if not links or not any(links):
        return {"ok": False, "error": "No links provided"}, 400

    try:
        result = merge_crawls(links)
        if fmt in ["m3u", "txt"]:
            content = finalize_text_output(result.get("m3u", "#EXTM3U"))
            return Response(content, content_type="text/plain; charset=utf-8", headers=get_cors_headers(False))
        
        return Response(json.dumps(result.get("json", []), ensure_ascii=False), 
                        content_type="application/json", headers=get_cors_headers(False))
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

@app.route("/api/supabase/upload", methods=["POST", "OPTIONS"])
def route_upload():
    if request.method == "OPTIONS":
        return Response(status=204, headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "POST"})
    
    data = request.get_json(silent=True) or {}
    filename = data.get("filename", "output.m3u")
    content = finalize_text_output(data.get("content", ""))
    
    if not content:
        return {"ok": False, "error": "Content is empty"}, 400

    try:
        env = get_supabase_env()
        bucket = ensure_bucket_exists()
        
        # Xây dựng đường dẫn file
        clean_filename = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip("-")
        object_path = f"{env['upload_dir']}/{clean_filename}" if env['upload_dir'] else clean_filename
        
        # Thực hiện Upload bằng Requests (không cần thư viện Supabase cồng kềnh)
        res = requests.post(
            f"{env['url']}/storage/v1/object/{quote(bucket)}/{quote(object_path, safe='/')}",
            headers={
                "apikey": env["key"], 
                "Authorization": f"Bearer {env['key']}",
                "x-upsert": "true",
                "Content-Type": "text/plain; charset=utf-8"
            },
            data=content.encode("utf-8"),
            timeout=30
        )
        
        if res.status_code not in [200, 201]:
            return {"ok": False, "error": res.text}, res.status_code
            
        return {
            "ok": True,
            "url": generate_storage_url(object_path),
            "filename": clean_filename,
            "path": object_path
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# --- STATIC CLIENT ROUTES ---

@app.route("/")
def serve_index():
    return send_from_directory(str(ROOT), "index.html")

@app.route("/assets/<path:path>")
def serve_assets(path):
    return send_from_directory(str(ROOT / "assets"), path)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
