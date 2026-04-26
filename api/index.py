import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

import requests
from flask import Flask, Response, request, send_from_directory
from dotenv import load_dotenv

# 1. Thiết lập đường dẫn để Import module crawl_to_m3u.py
ROOT = Path(__file__).resolve().parents[1]
current_dir = os.path.dirname(__file__)
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

# Import logic cào từ file crawl_to_m3u.py
try:
    import crawl_to_m3u
except ImportError:
    # Trường hợp chạy trên môi trường Vercel/Local khác nhau
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import crawl_to_m3u

app = Flask(__name__)
load_dotenv() # Tự động load biến môi trường từ file .env nếu có

# --- CẤU HÌNH TRỢ GIÚP ---

def to_clean_text(data):
    """
    CHỐT: Chuyển đổi List [] hoặc dữ liệu thô sang văn bản xuống dòng sạch sẽ.
    Fix lỗi app hiển thị dạng ['link1', 'link2'].
    """
    if isinstance(data, list):
        return "\n".join(str(item).strip() for item in data if item)
    return str(data or "").strip()

def get_supabase_cfg():
    """Lấy cấu hình Supabase từ môi trường"""
    return {
        "url": os.getenv("SUPABASE_URL", "").strip().rstrip("/"),
        "key": os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip(),
        "bucket": os.getenv("SUPABASE_STORAGE_BUCKET", "link").strip(),
        "is_public": os.getenv("SUPABASE_PUBLIC_BUCKET", "true").lower() == "true",
        "folder": os.getenv("SUPABASE_UPLOAD_DIR", "exports").strip("/")
    }

def ensure_bucket():
    """Tự động kiểm tra và tạo Bucket trên Supabase nếu chưa có"""
    cfg = get_supabase_cfg()
    if not cfg["url"] or not cfg["key"]: return cfg["bucket"]
    
    headers = {"apikey": cfg["key"], "Authorization": f"Bearer {cfg['key']}"}
    try:
        res = requests.get(f"{cfg['url']}/storage/v1/bucket", headers=headers, timeout=5)
        if res.status_code == 200:
            buckets = res.json()
            if not any(b.get("id") == cfg["bucket"] for b in buckets):
                requests.post(
                    f"{cfg['url']}/storage/v1/bucket", 
                    headers=headers, 
                    json={"id": cfg["bucket"], "name": cfg["bucket"], "public": cfg["is_public"]},
                    timeout=5
                )
    except Exception as e:
        print(f"Lỗi Bucket: {e}")
    return cfg["bucket"]

# --- CÁC ĐẦU MỤC API (ROUTES) ---

@app.route("/api/crawl")
def route_crawl():
    """Cào dữ liệu từ 1 đường link duy nhất"""
    link = request.args.get("link") or crawl_to_m3u.START_URL
    fmt = request.args.get("format", "json").lower()
    max_m = int(request.args.get("max", 80))
    
    try:
        result = crawl_to_m3u.crawl(max_matches=max_m, source_url=link)
        if fmt in ["m3u", "txt"]:
            # Trả về văn bản thuần túy cho trình duyệt in ra màn hình
            return Response(to_clean_text(result.get("m3u")), content_type="text/plain; charset=utf-8")
        
        return Response(json.dumps(result.get("json"), ensure_ascii=False), content_type="application/json")
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

@app.route("/api/merge", methods=["POST", "GET"])
def route_merge():
    """Gộp dữ liệu từ nhiều đường link"""
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        links = data.get("links") or [data.get("link")]
        fmt = str(data.get("format", "json")).lower()
        max_m = int(data.get("max", 80))
    else:
        links = request.args.getlist("link") or request.args.get("links", "").split()
        fmt = request.args.get("format", "json").lower()
        max_m = int(request.args.get("max", 80))

    try:
        result = crawl_to_m3u.merge_crawls(links, max_matches=max_m)
        if fmt in ["m3u", "txt"]:
            return Response(to_clean_text(result.get("m3u")), content_type="text/plain; charset=utf-8")
        
        return Response(json.dumps(result.get("json"), ensure_ascii=False), content_type="application/json")
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

@app.route("/api/supabase/upload", methods=["POST"])
def route_upload():
    """Tải nội dung lên Supabase Storage với cấu hình in ra màn hình (không tải về)"""
    data = request.get_json(silent=True) or {}
    filename = data.get("filename", "playlist.m3u")
    content = to_clean_text(data.get("content", ""))
    
    if not content:
        return {"ok": False, "error": "Nội dung trống"}, 400

    cfg = get_supabase_cfg()
    if not cfg["url"]:
        return {"ok": False, "error": "Thiếu thông tin cấu hình Supabase"}, 500

    try:
        bucket = ensure_bucket()
        # Làm sạch tên file và xây dựng đường dẫn lưu trữ
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip("-")
        obj_path = f"{cfg['folder']}/{safe_name}" if cfg['folder'] else safe_name
        
        # Thực hiện tải lên với Content-Type là text/plain để trình duyệt hiển thị nội dung trực tiếp
        response = requests.post(
            f"{cfg['url']}/storage/v1/object/{quote(bucket)}/{quote(obj_path, safe='/')}",
            headers={
                "apikey": cfg["key"], 
                "Authorization": f"Bearer {cfg['key']}", 
                "x-upsert": "true",
                "Content-Type": "text/plain; charset=utf-8" # QUAN TRỌNG: Để không bị tải về
            },
            data=content.encode("utf-8"),
            timeout=30
        )
        
        if response.status_code not in [200, 201]:
            return {"ok": False, "error": response.text}, response.status_code
            
        # Trả về link công khai
        final_url = f"{cfg['url']}/storage/v1/object/public/{quote(bucket)}/{quote(obj_path, safe='/')}"
        return {"ok": True, "url": final_url}
        
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# --- CUNG CẤP GIAO DIỆN (CLIENT) ---

@app.route("/")
def serve_index():
    """Trả về file index.html từ thư mục gốc"""
    return send_from_directory(str(ROOT), "index.html")

@app.route("/assets/<path:path>")
def serve_assets(path):
    """Trả về các file assets (css, js, img)"""
    return send_from_directory(str(ROOT / "assets"), path)

if __name__ == "__main__":
    # Chạy cục bộ (Local)
    app.run(host="127.0.0.1", port=5000, debug=True)
