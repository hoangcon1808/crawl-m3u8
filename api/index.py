import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote
import requests
from flask import Flask, Response, request, send_from_directory
from dotenv import load_dotenv

# Thiết lập path để import crawl_to_m3u.py
current_dir = os.path.dirname(__file__)
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

try:
    import crawl_to_m3u
except ImportError:
    parent_dir = os.path.dirname(current_dir)
    sys.path.insert(0, parent_dir)
    import crawl_to_m3u

app = Flask(__name__)
load_dotenv()

# --- HELPERS ---
def to_clean_text(data):
    """Fix lỗi list []: Chuyển đổi dữ liệu sang chuỗi văn bản thuần túy"""
    if isinstance(data, list):
        return "\n".join(str(item).strip() for item in data if item)
    return str(data or "").strip()

def get_supabase_cfg():
    return {
        "url": os.getenv("SUPABASE_URL", "").strip().rstrip("/"),
        "key": os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip(),
        "bucket": os.getenv("SUPABASE_STORAGE_BUCKET", "link").strip(),
        "is_public": os.getenv("SUPABASE_PUBLIC_BUCKET", "true").lower() == "true",
        "folder": os.getenv("SUPABASE_UPLOAD_DIR", "").strip("/")
    }

def ensure_bucket():
    cfg = get_supabase_cfg()
    if not cfg["url"] or not cfg["key"]: return
    headers = {"apikey": cfg["key"], "Authorization": f"Bearer {cfg['key']}"}
    try:
        res = requests.get(f"{cfg['url']}/storage/v1/bucket", headers=headers, timeout=5)
        if res.status_code == 200 and not any(b['id'] == cfg['bucket'] for b in res.json()):
            requests.post(f"{cfg['url']}/storage/v1/bucket", headers=headers, 
                          json={"id": cfg['bucket'], "name": cfg['bucket'], "public": cfg['is_public']})
    except: pass

# --- ROUTES ---
@app.route("/api/crawl")
def route_crawl():
    link = request.args.get("link") or crawl_to_m3u.START_URL
    fmt = request.args.get("format", "json").lower()
    try:
        res = crawl_to_m3u.crawl(max_matches=100, source_url=link)
        if fmt in ["m3u", "txt"]:
            return Response(to_clean_text(res.get("m3u")), content_type="text/plain; charset=utf-8")
        return Response(json.dumps(res.get("json"), ensure_ascii=False), content_type="application/json")
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

@app.route("/api/merge", methods=["GET", "POST"])
def route_merge():
    data = request.get_json(silent=True) or {} if request.method == "POST" else request.args
    links = data.get("links") or request.args.getlist("link") or data.get("links", "").split()
    fmt = str(data.get("format", "json")).lower()
    try:
        result = crawl_to_m3u.merge_crawls(links)
        if fmt in ["m3u", "txt"]:
            return Response(to_clean_text(result.get("m3u")), content_type="text/plain; charset=utf-8")
        return Response(json.dumps(result.get("json"), ensure_ascii=False), content_type="application/json")
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

@app.route("/api/supabase/upload", methods=["POST"])
def route_upload():
    data = request.get_json(silent=True) or {}
    filename = data.get("filename", "playlist.m3u")
    content = to_clean_text(data.get("content", ""))
    if not content: return {"ok": False, "error": "Content empty"}, 400
    try:
        cfg = get_supabase_cfg()
        ensure_bucket()
        obj_path = f"{cfg['folder']}/{filename}" if cfg['folder'] else filename
        res = requests.post(f"{cfg['url']}/storage/v1/object/{quote(cfg['bucket'])}/{quote(obj_path, safe='/')}",
                            headers={"apikey": cfg['key'], "Authorization": f"Bearer {cfg['key']}", "x-upsert": "true"},
                            data=content.encode("utf-8"))
        url = f"{cfg['url']}/storage/v1/object/public/{quote(cfg['bucket'])}/{quote(obj_path, safe='/')}"
        return {"ok": True, "url": url}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

@app.route("/")
def index(): return send_from_directory(str(Path(current_dir).parent), "index.html")

@app.route("/assets/<path:p>")
def assets(p): return send_from_directory(str(Path(current_dir).parent / "assets"), p)
