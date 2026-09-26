"""Admin auth: bcrypt + JWT (HttpOnly cookies), brute-force protection, RBAC."""
from fastapi import APIRouter, Request, Response, HTTPException, Depends
from pydantic import BaseModel, EmailStr, Field
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime, timezone, timedelta
from typing import Optional
import os
import uuid
import bcrypt
import jwt
import secrets
import logging

logger = logging.getLogger(__name__)

# ============ CONFIG ============
JWT_ALGORITHM = "HS256"
ACCESS_TTL_MIN = 60
REFRESH_TTL_DAYS = 7
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15

_client = AsyncIOMotorClient(os.environ["MONGO_URL"])
_db = _client[os.environ["DB_NAME"]]


def _jwt_secret() -> str:
    return os.environ["JWT_SECRET"]


# ============ HASHING ============
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ============ TOKENS ============
def _now():
    return datetime.now(timezone.utc)


def create_access_token(user_id: str, email: str, role: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "type": "access",
        "iat": _now(),
        "exp": _now() + timedelta(minutes=ACCESS_TTL_MIN),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


def create_refresh_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "type": "refresh",
        "iat": _now(),
        "exp": _now() + timedelta(days=REFRESH_TTL_DAYS),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


def _cookie_secure() -> bool:
    return os.environ.get("COOKIE_SECURE", "true").lower() != "false"


def _set_auth_cookies(response: Response, access: str, refresh: str):
    common = dict(httponly=True, secure=_cookie_secure(), samesite="lax", path="/")
    response.set_cookie("access_token", access, max_age=ACCESS_TTL_MIN * 60, **common)
    response.set_cookie("refresh_token", refresh, max_age=REFRESH_TTL_DAYS * 86400, **common)


def _clear_auth_cookies(response: Response):
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")


# ============ DEPENDENCIES ============
async def get_current_user(request: Request) -> dict:
    token = request.cookies.get("access_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = await _db.users.find_one({"id": payload["sub"]}, {"_id": 0, "password_hash": 0})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


async def get_current_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ============ BRUTE FORCE ============
def _identifier(request: Request, email: str) -> str:
    ip = request.client.host if request.client else "unknown"
    return f"{ip}:{email.lower()}"


async def _check_locked(identifier: str):
    doc = await _db.login_attempts.find_one({"identifier": identifier})
    if doc and doc.get("locked_until"):
        try:
            locked_until = datetime.fromisoformat(doc["locked_until"])
        except Exception:
            locked_until = None
        if locked_until and locked_until > _now():
            remaining = int((locked_until - _now()).total_seconds() / 60) + 1
            raise HTTPException(status_code=429, detail=f"Too many attempts. Try again in {remaining} minute(s).")


async def _record_failure(identifier: str):
    doc = await _db.login_attempts.find_one({"identifier": identifier})
    attempts = (doc or {}).get("attempts", 0) + 1
    update = {"attempts": attempts, "last_attempt": _now().isoformat()}
    if attempts >= MAX_FAILED_ATTEMPTS:
        update["locked_until"] = (_now() + timedelta(minutes=LOCKOUT_MINUTES)).isoformat()
        update["attempts"] = 0
    await _db.login_attempts.update_one(
        {"identifier": identifier}, {"$set": update, "$setOnInsert": {"identifier": identifier}}, upsert=True
    )


async def _clear_failures(identifier: str):
    await _db.login_attempts.delete_one({"identifier": identifier})


# ============ ACTIVITY LOG ============
async def log_activity(user: dict, action: str, resource: str, resource_id: Optional[str] = None, meta: Optional[dict] = None):
    """Insert an entry into the activity_log collection."""
    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user.get("id"),
        "user_email": user.get("email"),
        "action": action,  # created | updated | deleted | login | logout | uploaded
        "resource": resource,  # product | category | testimonial | journal | settings | media | user
        "resource_id": resource_id,
        "meta": meta or {},
        "at": _now().isoformat(),
    }
    await _db.activity_log.insert_one(doc)


# ============ MODELS ============
class LoginIn(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: str
    email: str
    name: str
    role: str


# ============ ROUTER ============
auth_router = APIRouter(prefix="/api/auth", tags=["auth"])


@auth_router.post("/login")
async def login(payload: LoginIn, request: Request, response: Response):
    identifier = _identifier(request, payload.email)
    await _check_locked(identifier)

    user = await _db.users.find_one({"email": payload.email.lower()})
    if not user or not verify_password(payload.password, user["password_hash"]):
        await _record_failure(identifier)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    await _clear_failures(identifier)
    access = create_access_token(user["id"], user["email"], user["role"])
    refresh = create_refresh_token(user["id"])
    _set_auth_cookies(response, access, refresh)
    await log_activity(user, "login", "auth")
    return {"id": user["id"], "email": user["email"], "name": user["name"], "role": user["role"]}


@auth_router.post("/logout")
async def logout(response: Response, user: dict = Depends(get_current_user)):
    _clear_auth_cookies(response)
    await log_activity(user, "logout", "auth")
    return {"ok": True}


@auth_router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    return {"id": user["id"], "email": user["email"], "name": user["name"], "role": user["role"]}


@auth_router.post("/refresh")
async def refresh_token(request: Request, response: Response):
    token = request.cookies.get("refresh_token")
    if not token:
        raise HTTPException(status_code=401, detail="No refresh token")
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Invalid token type")
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    user = await _db.users.find_one({"id": payload["sub"]}, {"_id": 0, "password_hash": 0})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    access = create_access_token(user["id"], user["email"], user["role"])
    common = dict(httponly=True, secure=_cookie_secure(), samesite="lax", path="/")
    response.set_cookie("access_token", access, max_age=ACCESS_TTL_MIN * 60, **common)
    return {"ok": True}


# ============ STARTUP ============
async def seed_admin():
    """Idempotent: create or update admin from env vars."""
    email = os.environ.get("ADMIN_EMAIL", "admin@gemovia.in").lower()
    password = os.environ.get("ADMIN_PASSWORD", "GemoviaAdmin@2026")
    existing = await _db.users.find_one({"email": email})
    if existing is None:
        doc = {
            "id": str(uuid.uuid4()),
            "email": email,
            "password_hash": hash_password(password),
            "name": "Aurevia Gems Admin",
            "role": "admin",
            "created_at": _now().isoformat(),
        }
        await _db.users.insert_one(doc)
        logger.info(f"Seeded admin user: {email}")
    elif not verify_password(password, existing["password_hash"]):
        await _db.users.update_one(
            {"email": email},
            {"$set": {"password_hash": hash_password(password), "role": "admin"}},
        )
        logger.info(f"Updated admin password for: {email}")


async def create_indexes():
    try:
        await _db.users.create_index("email", unique=True)
        await _db.users.create_index("id", unique=True)
        await _db.login_attempts.create_index("identifier", unique=True)
        await _db.activity_log.create_index([("at", -1)])
        await _db.site_settings.create_index("id", unique=True)
        await _db.media.create_index([("created_at", -1)])
    except Exception as e:
        logger.warning(f"Index creation warning: {e}")
