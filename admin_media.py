"""Admin media library: upload/list/soft-delete via Emergent object storage.
Files served through /api/media/{id} (public — used in product/journal image URLs).
"""
from fastapi import APIRouter, HTTPException, UploadFile, File, Depends, Response, Request
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime, timezone
from typing import Optional
import os
import uuid
import logging
import requests

from admin_auth import get_current_admin, log_activity

logger = logging.getLogger(__name__)

_client = AsyncIOMotorClient(os.environ["MONGO_URL"])
_db = _client[os.environ["DB_NAME"]]

STORAGE_URL = "https://integrations.emergentagent.com/objstore/api/v1/storage"
APP_NAME = "gemovia"
_storage_key: Optional[str] = None

ALLOWED_TYPES = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
    "image/gif": "gif", "image/webp": "webp", "image/svg+xml": "svg",
    "application/pdf": "pdf", "video/mp4": "mp4", "video/webm": "webm",
}
MAX_SIZE = 20 * 1024 * 1024  # 20 MB


def _now():
    return datetime.now(timezone.utc).isoformat()


def init_storage() -> Optional[str]:
    """Initialize storage session key. Call once at startup."""
    global _storage_key
    if _storage_key:
        return _storage_key
    key = os.environ.get("EMERGENT_LLM_KEY")
    if not key:
        logger.warning("EMERGENT_LLM_KEY not set - object storage disabled")
        return None
    try:
        resp = requests.post(f"{STORAGE_URL}/init", json={"emergent_key": key}, timeout=30)
        resp.raise_for_status()
        _storage_key = resp.json()["storage_key"]
        logger.info("Object storage initialized")
        return _storage_key
    except Exception as e:
        logger.error(f"Storage init failed: {e}")
        return None


def _put_object(path: str, data: bytes, content_type: str) -> dict:
    key = init_storage()
    if not key:
        raise HTTPException(status_code=503, detail="Object storage unavailable")
    resp = requests.put(
        f"{STORAGE_URL}/objects/{path}",
        headers={"X-Storage-Key": key, "Content-Type": content_type},
        data=data,
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


def _get_object(path: str):
    key = init_storage()
    if not key:
        raise HTTPException(status_code=503, detail="Object storage unavailable")
    resp = requests.get(
        f"{STORAGE_URL}/objects/{path}",
        headers={"X-Storage-Key": key},
        timeout=60,
    )
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="File not found in storage")
    resp.raise_for_status()
    return resp.content, resp.headers.get("Content-Type", "application/octet-stream")


media_router = APIRouter(tags=["media"])


@media_router.post("/api/admin/media/upload")
async def upload_media(file: UploadFile = File(...), user: dict = Depends(get_current_admin)):
    ctype = (file.content_type or "").lower()
    if ctype not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {ctype}")
    data = await file.read()
    if len(data) > MAX_SIZE:
        raise HTTPException(status_code=413, detail=f"File exceeds max size of {MAX_SIZE // (1024*1024)} MB")

    ext = ALLOWED_TYPES[ctype]
    file_id = str(uuid.uuid4())
    path = f"{APP_NAME}/media/{file_id}.{ext}"

    result = _put_object(path, data, ctype)

    backend_url = os.environ.get("PUBLIC_BACKEND_URL", "").rstrip("/")
    doc = {
        "id": file_id,
        "storage_path": result["path"],
        "original_filename": file.filename,
        "content_type": ctype,
        "size": len(data),
        "url": f"{backend_url}/api/media/{file_id}" if backend_url else f"/api/media/{file_id}",
        "is_deleted": False,
        "uploaded_by": user["email"],
        "created_at": _now(),
    }
    await _db.media.insert_one(doc)
    doc.pop("_id", None)
    await log_activity(user, "uploaded", "media", file_id, {"filename": file.filename, "size": doc["size"]})
    return doc


@media_router.get("/api/admin/media")
async def list_media(user: dict = Depends(get_current_admin)):
    items = await _db.media.find({"is_deleted": False}, {"_id": 0}).sort([("created_at", -1)]).to_list(500)
    return items


@media_router.delete("/api/admin/media/{file_id}")
async def delete_media(file_id: str, user: dict = Depends(get_current_admin)):
    doc = await _db.media.find_one({"id": file_id})
    if not doc:
        raise HTTPException(status_code=404, detail="File not found")
    await _db.media.update_one({"id": file_id}, {"$set": {"is_deleted": True, "deleted_at": _now()}})
    await log_activity(user, "deleted", "media", file_id, {"filename": doc.get("original_filename")})
    return {"ok": True}


@media_router.get("/api/media/{file_id}")
async def serve_media(file_id: str):
    """Public endpoint: serves media by id (used in product images, journal covers, etc.)."""
    record = await _db.media.find_one({"id": file_id, "is_deleted": False}, {"_id": 0})
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    data, content_type = _get_object(record["storage_path"])
    return Response(
        content=data,
        media_type=record.get("content_type", content_type),
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
