import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
import cloudscraper # Đảm bảo đã có trong requirements.txt
from flask import Flask, Response, request, send_from_directory

# Thiết lập đường dẫn hệ thống để import crawl_to_m3u.py
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Import logic từ file crawl của bạn
try:
    from crawl_to_m3u import MAX_MATCHES, START_URL, crawl, merge_crawls
except ImportError:
    # Giá trị fallback nếu file crawl chưa sẵn sàng
    MAX_MATCHES = 100
    START_URL = "https://hoadaotv.info/"
    def crawl(**kwargs): return {"json": {"error": "Crawl script missing"}, "m3u": "", "stats": {}}
    def merge_crawls(*args, **kwargs): return {"json": {}, "m3u": "", "stats": {}}

app = Flask(__name__)

# Cấu hình giới hạn
MAX_STORAGE_UPLOAD_BYTES = 8 * 1024 * 1024  # 8MB
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")

# --- HELPER FUNCTIONS ---

def load_local_env() -> None:
    """Load biến môi trường từ file .env khi chạy local"""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")

load_local_env()

# --- SUPABASE CONFIG GETTERS ---

def supabase_url() -> str:
    raw = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    if not raw: raise RuntimeError("Thiếu SUPABASE_URL.")
    return raw

def supabase_key() -> str:
    key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY") or "").strip()
    if not key: raise RuntimeError("Thiếu SUPABASE_SERVICE_ROLE_KEY.")
    return key

def supabase_bucket() -> str:
    return (os.getenv("SUPABASE_STORAGE_BUCKET") or "link").strip().strip("/")

def supabase_public_bucket() -> bool:
    return os.getenv("SUPABASE_PUBLIC_BUCKET", "true").lower() in {"true", "1", "yes"}

def ensure_supabase_bucket() -> str:
    """Tự động kiểm tra và tạo Bucket nếu chưa có"""
    base_url = supabase_url()
    key = supabase_key()
    target = supabase_bucket()
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    
    try:
        # Kiểm tra danh sách bucket
        res = requests.get(f"{base_url}/storage/v1/bucket", headers=headers, timeout=10)
        if res.status_code == 200:
            if any(b.get("id") == target for b in res.json()):
                return target
        
        # Nếu không thấy, tạo mới
        payload = {"id": target, "name": target, "public": supabase_public_bucket()}
        requests.post(f"{base_url}/storage/v1/bucket", headers=headers, json=payload, timeout=10)
        return target
    except:
        return target

# --- URL GENERATORS ---

def get_final_storage_url(object_path: str) -> str:
    """Tự động lấy link Public hoặc Signed URL"""
    base_url = supabase_url()
    bucket = supabase_bucket()
    key = supabase_key()
    
    if supabase_public_bucket():
        return f"{base_url}/storage/v1/object/public/{quote(bucket)}/{quote(object_path, safe='/')}"
    
    # Nếu là private, tạo signed URL (hết hạn sau 1 giờ)
    expire = int(os.getenv("SUPABASE_SIGNED_URL_EXPIRES", "3600"))
    res = requests.post(
        f"{base_url}/storage/v1/object/sign/{quote(bucket)}/{quote(object_path, safe='/')}",
        headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"expiresIn": expire},
        timeout=10
    )
    if res.status_code == 200:
        path = res.json().get("signedURL", "")
        return f"{base_url}/storage/v1{path}"
    return ""

# --- API ROUTES ---

@app.route("/api/crawl", methods=["GET", "OPTIONS"])
def crawl_route():
    if request.method == "OPTIONS": return Response(status=204, headers={"Access-Control-Allow-Origin": "*"})
    
    link = request.args.get("link") or START_URL
    fmt = request.args.get("format", "json").lower()
    
    try:
        # Gọi hàm crawl từ crawl_to_m3u.py
        result = crawl(max_matches=100, source_url=link)
        
        if fmt == "m3u":
            return Response(result["m3u"], content_type="text/plain; charset=utf-8")
        return Response(json.dumps(result["json"], ensure_ascii=False), content_type="application/json")
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

@app.route("/api/supabase/upload", methods=["POST", "OPTIONS"])
def upload_route():
    if request.method == "OPTIONS": return Response(status=204, headers={"Access-Control-Allow-Origin": "*"})
    
    payload = request.get_json(silent=True) or {}
    filename = payload.get("filename", "output.txt")
    content = payload.get("content", "")
    
    if not content: return {"ok": False, "error": "Content is empty"}, 400

    try:
        bucket = ensure_supabase_bucket()
        folder = (os.getenv("SUPABASE_UPLOAD_DIR") or "").strip("/")
        object_path = f"{folder}/{filename}" if folder else filename
        
        # Upload
        res = requests.post(
            f"{supabase_url()}/storage/v1/object/{quote(bucket)}/{quote(object_path, safe='/')}",
            headers={
                "apikey": supabase_key(),
                "Authorization": f"Bearer {supabase_key()}",
                "x-upsert": "true"
            },
            data=content.encode("utf-8"),
            timeout=30
        )
        
        if res.status_code not in [200, 201]:
            return {"ok": False, "error": res.text}, res.status_code
            
        return {
            "ok": True,
            "url": get_final_storage_url(object_path),
            "path": object_path
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

@app.route("/", methods=["GET"])
def index():
    return send_from_directory(str(ROOT), "index.html")

@app.route("/assets/<path:path>")
def send_assets(path):
    return send_from_directory(str(ROOT / "assets"), path)

if __name__ == "__main__":
    app.run(port=5000, debug=True)
