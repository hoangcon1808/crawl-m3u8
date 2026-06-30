import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import quote_plus, unquote_plus  # Bổ sung thư viện mã hóa

import requests
import certifi
from flask import Flask, Response, request, send_from_directory
from dotenv import load_dotenv
from pymongo import MongoClient
from bson.objectid import ObjectId

# --- THIẾT LẬP ĐƯỜNG DẪN ---
ROOT = Path(__file__).resolve().parents[1]
current_dir = os.path.dirname(__file__)

if current_dir not in sys.path:
    sys.path.insert(0, current_dir)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import crawl_to_m3u
except ImportError as e:
    print(f"Lỗi import crawl_to_m3u: {e}")

app = Flask(__name__)
load_dotenv()

# --- HÀM XỬ LÝ LỖI KÝ TỰ ĐẶC BIỆT MONGODB ---
def escape_mongo_uri(uri):
    """
    Tự động tìm và mã hóa (escape) username/password trong chuỗi MONGO_URI
    sử dụng quote_plus theo đúng chuẩn RFC 3986.
    """
    # Biểu thức chính quy (Regex) để tách User và Password ra khỏi URI
    match = re.match(r'^(mongodb(?:\+srv)?://)(.*?):(.*)@([^@/]+(?:/.*)?)$', uri)
    if match:
        prefix = match.group(1)
        # unquote_plus để chống lỗi mã hóa 2 lần (nếu trước đó đã lỡ mã hóa)
        user = quote_plus(unquote_plus(match.group(2)))
        password = quote_plus(unquote_plus(match.group(3)))
        rest = match.group(4)
        return f"{prefix}{user}:{password}@{rest}"
    return uri

# --- CẤU HÌNH & KẾT NỐI MONGODB ---
mongo_client = None

def get_mongo_cfg():
    raw_uri = os.getenv("MONGO_URI", "").strip()
    return {
        "uri": escape_mongo_uri(raw_uri),  # Đưa URI qua hàm xử lý lỗi
        "db_name": os.getenv("MONGO_DB_NAME", "crawl_database").strip(),
        "collection": os.getenv("MONGO_COLLECTION_NAME", "stream_links").strip(),
        "enable_ttl": os.getenv("MONGO_ENABLE_TTL", "true").lower() == "true",
        "ttl_hours": int(os.getenv("MONGO_TTL_HOURS", "24").strip())
    }

def get_mongo_collection():
    global mongo_client
    cfg = get_mongo_cfg()
    
    if not cfg["uri"]:
        raise ValueError("Thiếu cấu hình MONGO_URI trên Vercel")
    
    if mongo_client is None:
        mongo_client = MongoClient(
            cfg["uri"],
            tlsCAFile=certifi.where(),
            serverSelectionTimeoutMS=10000,
            connectTimeoutMS=10000,
            retryWrites=True
        )
        
    db = mongo_client[cfg["db_name"]]
    return db[cfg["collection"]], cfg

def ensure_ttl_index():
    try:
        collection, cfg = get_mongo_collection()
        if cfg["enable_ttl"]:
            collection.create_index("expireAt", expireAfterSeconds=0)
    except Exception as e:
        print(f"Bỏ qua lỗi tạo Index: {e}")

# --- HÀM TRỢ GIÚP ---
def to_clean_text(data):
    if isinstance(data, list):
        return "\n".join(str(item).strip() for item in data if item)
    return str(data or "").strip()

# --- CÁC ĐẦU MỤC API (ROUTES) ---

@app.route("/api/crawl")
def route_crawl():
    link = request.args.get("link") or crawl_to_m3u.START_URL
    fmt = request.args.get("format", "json").lower()
    max_m = int(request.args.get("max", 80))
    try:
        result = crawl_to_m3u.crawl(max_matches=max_m, source_url=link)
        if fmt in ["m3u", "txt"]:
            return Response(to_clean_text(result.get("m3u")), content_type="text/plain; charset=utf-8")
        return Response(json.dumps(result.get("json"), ensure_ascii=False), content_type="application/json")
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

@app.route("/api/merge", methods=["POST", "GET"])
def route_merge():
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

@app.route("/api/database/save", methods=["POST"])
def route_save_to_mongo():
    try:
        ensure_ttl_index()
        
        data = request.get_json(silent=True) or {}
        filename = data.get("filename", "playlist.m3u")
        content = to_clean_text(data.get("content", ""))
        
        if not content:
            return {"ok": False, "error": "Nội dung trống"}, 400
            
        collection, cfg = get_mongo_collection()
        
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip("-")
        expiration_time = datetime.utcnow() + timedelta(hours=cfg["ttl_hours"])
        
        document = {
            "filename": safe_name,
            "content": content,
            "createdAt": datetime.utcnow(),
            "expireAt": expiration_time
        }
        
        result = collection.insert_one(document)
        doc_id = str(result.inserted_id)
        public_url = f"{request.host_url}playlist/{doc_id}"
        
        return {"ok": True, "url": public_url}
        
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

@app.route("/playlist/<doc_id>", methods=["GET"])
def route_serve_playlist(doc_id):
    try:
        collection, _ = get_mongo_collection()
        document = collection.find_one({"_id": ObjectId(doc_id)})
        
        if not document:
            return Response("Playlist không tồn tại hoặc đã hết hạn", status=404)
            
        return Response(document["content"], content_type="text/plain; charset=utf-8")
        
    except Exception as e:
        return Response(f"Lỗi hệ thống: {str(e)}", status=500)

# --- PHỤC VỤ GIAO DIỆN (FRONT-END) ---
@app.route("/")
def serve_index():
    return send_from_directory(str(ROOT), "index.html")

@app.route("/assets/<path:path>")
def serve_assets(path):
    return send_from_directory(str(ROOT / "assets"), path)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
