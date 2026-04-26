import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

import requests
from flask import Flask, Response, request, send_from_directory
from dotenv import load_dotenv

# Thêm thư mục hiện tại vào path để tránh lỗi import trên Vercel
current_dir = os.path.dirname(__file__)
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

# Import logic crawl
try:
    import crawl_to_m3u
except ImportError:
    # Nếu file crawl_to_m3u.py nằm ở thư mục gốc (ngoài api/)
    parent_dir = os.path.dirname(current_dir)
    sys.path.insert(0, parent_dir)
    import crawl_to_m3u

app = Flask(__name__)
load_dotenv()

# --- CẤU HÌNH ---
MAX_STORAGE_UPLOAD_BYTES = 8 * 1024 * 1024

def get_env(key, default=""):
    return os.environ.get(key, default).strip()

def get_cors_headers(cache=True):
    headers = {"Access-Control-Allow-Origin": "*"}
    headers["Cache-Control"] = "public, s-maxage=60" if cache else "no-store"
    return headers

# --- HELPER: XỬ LÝ ĐỊNH DẠNG TEXT/M3U ---
def to_clean_text(data):
    """Sửa lỗi list []: Chuyển đổi mọi loại dữ liệu sang chuỗi văn bản thuần túy"""
    if isinstance(data, list):
        # Nối các phần tử bằng dấu xuống dòng, bỏ các dòng trống
        return "\n".join(str(item).strip() for item in data if item)
    if data is None:
        return ""
    return str(data).strip()

# --- SUPABASE UTILS ---
def get_supabase_config():
    return {
        "url": get_env("SUPABASE_URL").rstrip("/"),
        "key": get_env("SUPABASE_SERVICE_ROLE_KEY") or get_env("SUPABASE_KEY"),
        "bucket": get_env("SUPABASE_STORAGE_BUCKET", "link"),
        "is_public": get_env("SUPABASE_PUBLIC_BUCKET", "true").lower() == "true",
        "folder": get_env("SUPABASE_UPLOAD_DIR").strip("/")
    }

def ensure_bucket():
    cfg = get_supabase_config()
    if not cfg["url"] or not cfg["key"]: return cfg["bucket"]
    headers = {"apikey": cfg["key"], "Authorization": f"Bearer {cfg['key']}"}
    try:
        res = requests.get(f"{cfg['url']}/storage/v1/bucket", headers=headers, timeout=5)
        if res.status_code == 200:
            if not any(b['id'] == cfg['bucket'] for b in res.json()):
                requests.post(f"{cfg['url']}/storage/v1/bucket", headers=headers, 
                             json={"id": cfg['bucket'], "name": cfg['bucket'], "public": cfg['is_public']})
    except: pass
    return cfg['bucket']

# --- ROUTES ---

@app.route("/api/crawl")
def route_crawl():
    link = request.args.get("link") or crawl_to_m3u.START_URL
    fmt = request.args.get("format", "json").lower()
    try:
        res = crawl_to_m3u.crawl(max_matches=100, source_url=link)
        if fmt == "m3u":
            # Đảm bảo trả về TEXT thuần, không phải LIST
            return Response(to_clean_text(res.get("m3u", "#EXTM3U")), 
                            content_type="text/plain; charset=utf-8", headers=get_cors_headers())
        return Response(json.dumps(res.get("json", []), ensure_ascii=False), 
                        content_type="application/json", headers=get_cors_headers())
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

@app.route("/api/merge", methods=["GET", "POST"])
def route_merge():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        links = data.get("links") or [data.get("link")]
        fmt = str(data.get("format", "json")).lower()
    else:
        links = request.args.getlist("link") or request.args.get("links", "").split()
        fmt = request.args.get("format", "json").lower()

    try:
        result = crawl_to_m3u.merge_crawls(links)
        if fmt in ["m3u", "txt"]:
            # FIX LỖI: Nối list thành chuỗi có xuống dòng
            content = to_clean_text(result.get("m3u", "#EXTM3U"))
            return Response(content, content_type="text/plain; charset=utf-8", headers=get_cors_headers(False))
        return Response(json.dumps(result.get("json", []), ensure_ascii=False), 
                        content_type="application/json", headers=get_cors_headers(False))
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

@app.route("/api/supabase/upload", methods=["POST"])
def route_upload():
    data = request.get_json(silent=True) or {}
    filename = data.get("filename", "output.m3u")
    # Đảm bảo nội dung upload là chuỗi văn bản sạch
    content = to_clean_text(data.get("content", ""))
    
    if not content: return {"ok": False, "error": "Content is empty"}, 400

    try:
        cfg = get_supabase_config()
        bucket = ensure_bucket()
        clean_name = re.sub(r"[^A-Za-z0-9._-]+", "-", filename)
        obj_path = f"{cfg['folder']}/{clean_name}" if cfg['folder'] else clean_name
        
        headers = {"apikey": cfg['key'], "Authorization": f"Bearer {cfg['key']}", "x-upsert": "true"}
        up_res = requests.post(
            f"{cfg['url']}/storage/v1/object/{quote(bucket)}/{quote(obj_path, safe='/')}",
            headers=headers, data=content.encode("utf-8")
        )
        
        if up_res.status_code not in [200, 201]:
            return {"ok": False, "error": up_res.text}, 400
            
        # Trả về URL cuối cùng
        final_url = f"{cfg['url']}/storage/v1/object/public/{quote(bucket)}/{quote(obj_path, safe='/')}"
        return {"ok": True, "url": final_url}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# Giao diện Client
@app.route("/")
def index():
    return send_from_directory(str(Path(current_dir).parent), "index.html")

@app.route("/assets/<path:path>")
def assets(path):
    return send_from_directory(str(Path(current_dir).parent / "assets"), path)

if __name__ == "__main__":
    app.run(port=5000, debug=True)
