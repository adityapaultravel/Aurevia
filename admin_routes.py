"""Admin CRUD routes: products, categories, testimonials, journal, settings, inquiries, newsletter, activity.
Also exposes a public GET /api/settings for the public site to pull dynamic content.
"""
from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient
import csv
import io
import os
import uuid

from admin_auth import get_current_admin, log_activity

_client = AsyncIOMotorClient(os.environ["MONGO_URL"])
_db = _client[os.environ["DB_NAME"]]


def _now():
    return datetime.now(timezone.utc).isoformat()


# ============ SITE SETTINGS ============
class SiteSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    site_name: str = "Aurevia Gems"
    announcement: str = "COMPLIMENTARY INSURED WORLDWIDE SHIPPING · MALCA-AMIT"
    hero_kicker: str = "Aurevia Gems · Where Destiny Meets Divinity"
    hero_title_line1: str = "Your Destined"
    hero_title_line2: str = "Gemstone Awaits."
    hero_description: str = (
        "Deep within the earth, every gemstone was formed with its own character, energy, and purpose. "
        "At Aurevia Gems, we help you discover the one destined to walk alongside your journey—bringing beauty, "
        "balance, and meaning to every chapter of your life. Through the wisdom of Vedic astrology, we help "
        "you find the gemstone truly meant for you."
    )
    contact_email: str = "gemoviaforyou@gmail.com"
    whatsapp_number: str = "919999999999"
    instagram_url: str = "https://instagram.com/gemovia_"
    instagram_handle: str = "@gemovia_"
    business_hours: str = "Monday – Saturday · 10:00 to 19:00 IST"
    seo_title: str = "Aurevia Gems | Rare Certified Gemstones · Vedic Recommendations"
    seo_description: str = "A private atelier of rare, ethically sourced gemstones. Independently certified — certification provided on request."
    seo_keywords: str = "gemstones, ruby, emerald, sapphire, vedic astrology, certified gemstones"
    og_image: Optional[str] = None
    favicon: Optional[str] = None


DEFAULT_SETTINGS_ID = "singleton"

public_router = APIRouter(prefix="/api", tags=["public"])
content_router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(get_current_admin)])


async def seed_settings():
    existing = await _db.site_settings.find_one({"id": DEFAULT_SETTINGS_ID})
    if not existing:
        doc = SiteSettings().model_dump()
        doc["id"] = DEFAULT_SETTINGS_ID
        doc["updated_at"] = _now()
        await _db.site_settings.insert_one(doc)


@public_router.get("/settings")
async def get_public_settings():
    doc = await _db.site_settings.find_one({"id": DEFAULT_SETTINGS_ID}, {"_id": 0})
    if not doc:
        await seed_settings()
        doc = await _db.site_settings.find_one({"id": DEFAULT_SETTINGS_ID}, {"_id": 0})
    return doc


@content_router.get("/settings")
async def admin_get_settings():
    return await get_public_settings()


@content_router.put("/settings")
async def admin_update_settings(payload: SiteSettings, user: dict = Depends(get_current_admin)):
    doc = payload.model_dump()
    doc["updated_at"] = _now()
    await _db.site_settings.update_one(
        {"id": DEFAULT_SETTINGS_ID},
        {"$set": doc, "$setOnInsert": {"id": DEFAULT_SETTINGS_ID}},
        upsert=True,
    )
    await log_activity(user, "updated", "settings", DEFAULT_SETTINGS_ID)
    updated = await _db.site_settings.find_one({"id": DEFAULT_SETTINGS_ID}, {"_id": 0})
    return updated


# ============ DASHBOARD STATS ============
@content_router.get("/stats")
async def dashboard_stats():
    products = await _db.products.find({}, {"_id": 0, "id": 1, "name": 1, "slug": 1, "stock": 1, "status": 1}).to_list(1000)
    orders = await _db.orders.find({}, {"_id": 0, "order_status": 1}).to_list(5000)
    low = [p for p in products if 0 < int(p.get("stock") or 0) <= 3]
    out = [p for p in products if int(p.get("stock") or 0) == 0 or p.get("status") == "Out of Stock"]
    status_counts = {s: 0 for s in VALID_ORDER_STATUSES}
    for o in orders:
        status_counts[o.get("order_status") or "Pending"] = status_counts.get(o.get("order_status") or "Pending", 0) + 1
    return {
        "products": len(products),
        "categories": await _db.categories.count_documents({}),
        "testimonials": await _db.testimonials.count_documents({}),
        "journal": await _db.journal.count_documents({}),
        "inquiries": await _db.inquiries.count_documents({}),
        "newsletter": await _db.newsletter.count_documents({}),
        "orders": len(orders),
        "paid_orders": await _db.orders.count_documents({"payment_status": "paid"}),
        "low_stock": len(low),
        "out_of_stock": len(out),
        "low_stock_items": sorted(low + out, key=lambda p: int(p.get("stock") or 0))[:8],
        "order_status_counts": status_counts,
    }


# ============ INQUIRIES + NEWSLETTER + ORDERS + ACTIVITY (literal paths first) ============
@content_router.get("/inquiries")
async def list_inquiries():
    return await _db.inquiries.find({}, {"_id": 0}).sort([("created_at", -1)]).to_list(500)


@content_router.delete("/inquiries/{item_id}")
async def delete_inquiry(item_id: str, user: dict = Depends(get_current_admin)):
    await _db.inquiries.delete_one({"id": item_id})
    await log_activity(user, "deleted", "inquiry", item_id)
    return {"ok": True}


@content_router.get("/newsletter")
async def list_newsletter():
    return await _db.newsletter.find({}, {"_id": 0}).sort([("subscribed_at", -1)]).to_list(500)


@content_router.delete("/newsletter/{email}")
async def delete_newsletter(email: str, user: dict = Depends(get_current_admin)):
    await _db.newsletter.delete_one({"email": email.lower()})
    await log_activity(user, "deleted", "newsletter_sub", email)
    return {"ok": True}


@content_router.get("/orders")
async def list_orders(status: Optional[str] = None, q: Optional[str] = None):
    query = {}
    if status:
        query["payment_status"] = status.lower()
    docs = await _db.orders.find(query, {"_id": 0}).sort([("created_at", -1)]).to_list(500)
    if q:
        needle = q.lower()
        docs = [
            d for d in docs
            if needle in (d.get("id") or "").lower()
            or needle in ((d.get("shipping") or {}).get("name") or "").lower()
            or needle in ((d.get("shipping") or {}).get("email") or "").lower()
        ]
    return docs


VALID_ORDER_STATUSES = ["Pending", "Processing", "Shipped", "Delivered", "Cancelled"]


class OrderStatusIn(BaseModel):
    order_status: str


@content_router.patch("/orders/{order_id}/status")
async def update_order_status(order_id: str, payload: OrderStatusIn, user: dict = Depends(get_current_admin)):
    if payload.order_status not in VALID_ORDER_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of {VALID_ORDER_STATUSES}")
    result = await _db.orders.update_one(
        {"id": order_id},
        {"$set": {"order_status": payload.order_status, "order_status_updated_at": _now()}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Order not found")
    await log_activity(user, "updated_status", "order", order_id, {"label": payload.order_status})
    # If moving to Delivered/Shipped, decrement stock on referenced products (idempotent guard)
    if payload.order_status in ("Shipped", "Delivered"):
        doc = await _db.orders.find_one({"id": order_id}, {"_id": 0})
        if doc and not doc.get("stock_applied"):
            for it in (doc.get("items") or []):
                slug = it.get("slug")
                qty = int(it.get("qty") or 0)
                if slug and qty > 0:
                    await _db.products.update_one({"slug": slug}, {"$inc": {"stock": -qty}, "$set": {"updated_at": _now()}})
                    await _db.products.update_one(
                        {"slug": slug, "stock": {"$lte": 0}, "status": {"$nin": ["Reserved", "Sold"]}},
                        {"$set": {"stock": 0, "status": "Out of Stock"}},
                    )
            await _db.orders.update_one({"id": order_id}, {"$set": {"stock_applied": True}})
    return await _db.orders.find_one({"id": order_id}, {"_id": 0})


# ============ INVENTORY ============
VALID_STOCK_STATUSES = ["Available", "Reserved", "Sold", "Out of Stock"]


class StockDeltaIn(BaseModel):
    delta: Optional[int] = None
    stock: Optional[int] = None


@content_router.patch("/products/{item_id}/stock")
async def update_product_stock(item_id: str, payload: StockDeltaIn, user: dict = Depends(get_current_admin)):
    if payload.delta is None and payload.stock is None:
        raise HTTPException(status_code=400, detail="Provide either delta or stock")
    doc = await _db.products.find_one({"id": item_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Product not found")
    if payload.stock is not None:
        new_stock = max(0, int(payload.stock))
    else:
        new_stock = max(0, int(doc.get("stock", 0)) + int(payload.delta))
    update = {"stock": new_stock, "updated_at": _now()}
    # Auto-flip status when hitting zero and it isn't already sold/reserved by hand
    if new_stock == 0 and doc.get("status") not in ("Reserved", "Sold"):
        update["status"] = "Out of Stock"
    elif new_stock > 0 and doc.get("status") == "Out of Stock":
        update["status"] = "Available"
    await _db.products.update_one({"id": item_id}, {"$set": update})
    await log_activity(user, "updated_stock", "product", item_id, {"label": doc.get("name"), "stock": new_stock})
    return await _db.products.find_one({"id": item_id}, {"_id": 0})


class StockStatusIn(BaseModel):
    status: str


@content_router.patch("/products/{item_id}/status")
async def update_product_status(item_id: str, payload: StockStatusIn, user: dict = Depends(get_current_admin)):
    if payload.status not in VALID_STOCK_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of {VALID_STOCK_STATUSES}")
    doc = await _db.products.find_one({"id": item_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Product not found")
    update = {"status": payload.status, "updated_at": _now()}
    if payload.status == "Out of Stock":
        update["stock"] = 0
    await _db.products.update_one({"id": item_id}, {"$set": update})
    await log_activity(user, "updated_status", "product", item_id, {"label": doc.get("name"), "status": payload.status})
    return await _db.products.find_one({"id": item_id}, {"_id": 0})


@content_router.get("/inventory/summary")
async def inventory_summary():
    all_products = await _db.products.find({}, {"_id": 0}).to_list(500)
    total = len(all_products)
    total_stock = sum(int(p.get("stock") or 0) for p in all_products)
    low_stock_threshold = 3
    return {
        "total_skus": total,
        "total_stock": total_stock,
        "available": sum(1 for p in all_products if (p.get("status") or "Available") == "Available"),
        "reserved": sum(1 for p in all_products if p.get("status") == "Reserved"),
        "sold": sum(1 for p in all_products if p.get("status") == "Sold"),
        "out_of_stock": sum(1 for p in all_products if int(p.get("stock") or 0) == 0 or p.get("status") == "Out of Stock"),
        "low_stock": sum(1 for p in all_products if 0 < int(p.get("stock") or 0) <= low_stock_threshold),
        "low_stock_threshold": low_stock_threshold,
    }


@content_router.get("/activity")
async def list_activity(limit: int = 200):
    return await _db.activity_log.find({}, {"_id": 0}).sort([("at", -1)]).limit(limit).to_list(limit)


# ============ CSV EXPORTS ============
def _csv_response(rows: list, headers: list, filename: str):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    for r in rows:
        w.writerow(r)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@content_router.get("/export/inventory.csv")
async def export_inventory_csv(user: dict = Depends(get_current_admin)):
    products = await _db.products.find({}, {"_id": 0}).sort([("name", 1)]).to_list(1000)
    rows = [[
        p.get("name"), p.get("slug"), p.get("gemstone_type"), p.get("category") or "", p.get("price"), p.get("carat"),
        p.get("shape") or "", p.get("color") or "", p.get("clarity") or "", p.get("cut") or "", p.get("origin") or "",
        p.get("treatment") or "", p.get("certificate_number") or "", "yes" if p.get("certificate_url") else "no", p.get("stock", 0),
        p.get("status") or ("Out of Stock" if int(p.get("stock") or 0) == 0 else "Available"),
        "yes" if p.get("featured") else "no", p.get("updated_at") or p.get("created_at") or "",
    ] for p in products]
    headers = ["Name", "Slug", "Type", "Category", "Price (INR)", "Carat", "Shape", "Color", "Clarity", "Cut", "Origin",
               "Treatment", "Certificate No.", "Certificate Uploaded", "Stock", "Status", "Featured", "Last Updated"]
    await log_activity(user, "exported", "inventory_csv", None, {"count": len(rows)})
    return _csv_response(rows, headers, f"aurevia-gems-inventory-{datetime.now(timezone.utc).date()}.csv")


@content_router.get("/export/orders.csv")
async def export_orders_csv(user: dict = Depends(get_current_admin)):
    orders = await _db.orders.find({}, {"_id": 0}).sort([("created_at", -1)]).to_list(5000)
    rows = []
    for o in orders:
        sh = o.get("shipping") or {}
        items = "; ".join(f"{it.get('name') or it.get('slug')} x{it.get('qty', 0)}" for it in (o.get("items") or []))
        rows.append([
            o.get("id"), o.get("created_at"), sh.get("name") or "", sh.get("email") or "", sh.get("phone") or "",
            f"{sh.get('city') or ''}, {sh.get('state') or ''}, {sh.get('country') or ''}",
            items, sum(int(it.get("qty") or 0) for it in (o.get("items") or [])),
            o.get("total"), (o.get("payment_status") or "pending"), o.get("order_status") or "Pending",
        ])
    headers = ["Order ID", "Created", "Customer", "Email", "Phone", "Location", "Items", "Qty", "Total (INR)", "Payment", "Order Status"]
    await log_activity(user, "exported", "orders_csv", None, {"count": len(rows)})
    return _csv_response(rows, headers, f"aurevia-gems-orders-{datetime.now(timezone.utc).date()}.csv")


# ============ EXPORT / BACKUP ============
@content_router.get("/backup")
async def export_backup(user: dict = Depends(get_current_admin)):
    collections = ["products", "categories", "testimonials", "journal", "site_settings", "inquiries", "newsletter", "orders", "activity_log", "media"]
    dump = {}
    for c in collections:
        dump[c] = await _db[c].find({}, {"_id": 0}).to_list(5000)
    await log_activity(user, "exported", "backup", None, {"collections": collections})
    return {"exported_at": _now(), "collections": dump}


class RestoreIn(BaseModel):
    collections: dict


@content_router.post("/restore")
async def restore_backup(payload: RestoreIn, user: dict = Depends(get_current_admin)):
    restored = {}
    for cname, items in payload.collections.items():
        if cname not in ("products", "categories", "testimonials", "journal", "site_settings"):
            continue
        count = 0
        for item in items:
            if not item.get("id"):
                continue
            await _db[cname].update_one({"id": item["id"]}, {"$set": item}, upsert=True)
            count += 1
        restored[cname] = count
    await log_activity(user, "restored", "backup", None, restored)
    return {"ok": True, "restored": restored}


# ============ RESOURCE CRUD (parameterized paths LAST) ============
class ProductIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    slug: str
    description: str = ""
    gemstone_type: str
    category: Optional[str] = None                    # Precious / Semi-Precious
    price: int
    carat: float = 0
    origin: str = ""
    color: str = ""
    treatment: str = ""
    shape: Optional[str] = None                       # Oval, Round, Cushion, Emerald cut, ...
    clarity: Optional[str] = None                     # VVS1, VS1, SI1, Eye-clean, ...
    cut: Optional[str] = None                         # Excellent, Very Good, Good, Fair
    certificate_number: Optional[str] = None
    certificate_url: Optional[str] = None
    certificate_type: Optional[str] = None            # pdf | image
    status: str = "Available"                         # Available / Reserved / Sold / Out of Stock
    images: List[str] = []
    stock: int = 10
    featured: bool = False


class CategoryIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    slug: str
    description: str = ""
    image: str = ""


class TestimonialIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    quote: str
    author: str
    role: str = ""
    city: str = ""
    stone: str = ""


class JournalIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    slug: str
    title: str
    excerpt: str
    body: str
    cover: str
    category: str = "Journal"
    read_time: int = 4
    author: str = "Aurevia Gems Atelier"


RESOURCE_MAP = {
    "products": {"collection": "products", "model": ProductIn, "key": "slug", "search_fields": ["name", "slug", "gemstone_type", "certificate_number"]},
    "categories": {"collection": "categories", "model": CategoryIn, "key": "slug", "search_fields": ["name", "slug"]},
    "testimonials": {"collection": "testimonials", "model": TestimonialIn, "key": None, "search_fields": ["author", "quote", "city"]},
    "journal": {"collection": "journal", "model": JournalIn, "key": "slug", "search_fields": ["title", "slug", "excerpt"]},
}


def _get_resource(name: str):
    if name not in RESOURCE_MAP:
        raise HTTPException(status_code=404, detail="Resource not found")
    return RESOURCE_MAP[name]


@content_router.get("/{resource}")
async def list_resource(resource: str, q: Optional[str] = Query(None)):
    res = _get_resource(resource)
    query = {}
    if q:
        query = {"$or": [{f: {"$regex": q, "$options": "i"}} for f in res["search_fields"]]}
    return await _db[res["collection"]].find(query, {"_id": 0}).sort([("_id", -1)]).to_list(500)


@content_router.post("/{resource}")
async def create_resource(resource: str, payload: dict, user: dict = Depends(get_current_admin)):
    res = _get_resource(resource)
    validated = res["model"](**payload).model_dump()
    validated["id"] = str(uuid.uuid4())
    validated["created_at"] = _now()
    if res.get("key"):
        existing = await _db[res["collection"]].find_one({res["key"]: validated[res["key"]]})
        if existing:
            raise HTTPException(status_code=409, detail=f"Entry with this {res['key']} already exists")
    if resource == "journal":
        validated["published_at"] = _now()
    await _db[res["collection"]].insert_one(validated)
    validated.pop("_id", None)
    await log_activity(
        user, "created", resource[:-1], validated["id"],
        {"label": validated.get("name") or validated.get("title") or validated.get("author")},
    )
    return validated


@content_router.put("/{resource}/{item_id}")
async def update_resource(resource: str, item_id: str, payload: dict, user: dict = Depends(get_current_admin)):
    res = _get_resource(resource)
    validated = res["model"](**payload).model_dump()
    validated["updated_at"] = _now()
    result = await _db[res["collection"]].update_one({"id": item_id}, {"$set": validated})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Item not found")
    await log_activity(user, "updated", resource[:-1], item_id)
    return await _db[res["collection"]].find_one({"id": item_id}, {"_id": 0})


@content_router.delete("/{resource}/{item_id}")
async def delete_resource(resource: str, item_id: str, user: dict = Depends(get_current_admin)):
    res = _get_resource(resource)
    doc = await _db[res["collection"]].find_one({"id": item_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Item not found")
    await _db[res["collection"]].delete_one({"id": item_id})
    await log_activity(
        user, "deleted", resource[:-1], item_id,
        {"label": doc.get("name") or doc.get("title") or doc.get("author")},
    )
    return {"ok": True}
