"""Stripe payments router — Flow B (BYOK using emergentintegrations)."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ConfigDict, EmailStr
from typing import List, Optional
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient
import os
import uuid
import logging

EMERGENT_PAYMENTS_AVAILABLE = True
try:
    from emergentintegrations.payments.stripe.checkout import (
        StripeCheckout,
        CheckoutSessionRequest,
    )
except Exception:
    EMERGENT_PAYMENTS_AVAILABLE = False

    class CheckoutSessionRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class StripeCheckout:
        def __init__(self, *args, **kwargs):
            pass

        async def create_checkout_session(self, request):
            raise RuntimeError("Emergent payments package unavailable")

        async def get_checkout_status(self, session_id):
            raise RuntimeError("Emergent payments package unavailable")

logger = logging.getLogger(__name__)

stripe_router = APIRouter(prefix="/api")

mongo_url = os.environ.get("MONGO_URL")
DB_NAME = os.environ.get("DB_NAME", "test_database")
if mongo_url:
    try:
        _client = AsyncIOMotorClient(mongo_url)
        _db = _client[DB_NAME]
    except Exception:
        _client = None
        _db = None
else:
    class FakeCollection:
        def __init__(self):
            self._items = []

        async def find_one(self, q, projection=None):
            for it in self._items:
                if all(it.get(k) == v for k, v in q.items()):
                    return {k: v for k, v in it.items() if k != "_id"}
            return None

        async def insert_one(self, doc):
            self._items.append(doc)

        async def update_one(self, query, update, upsert=False):
            for it in self._items:
                if all(it.get(k) == v for k, v in query.items() if not k.startswith("$")):
                    if "$set" in update:
                        it.update(update["$set"])
                    return
            if upsert:
                new_doc = {**query.get("$setOnInsert", {}), **update.get("$set", {})}
                self._items.append(new_doc)

    class FakeDB:
        def __init__(self):
            self.products = FakeCollection()
            self.orders = FakeCollection()
            self.payment_transactions = FakeCollection()

    _client = None
    _db = FakeDB()


# ============ MODELS ============
class CartItemIn(BaseModel):
    slug: str
    qty: int = Field(1, ge=1, le=20)


class ShippingAddress(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    email: EmailStr
    phone: str
    line1: str
    line2: Optional[str] = ""
    city: str
    state: str
    postal_code: str
    country: str = "India"


class CheckoutSessionIn(BaseModel):
    items: List[CartItemIn]
    shipping: ShippingAddress
    origin_url: str


class OrderItem(BaseModel):
    product_id: str
    slug: str
    name: str
    gemstone_type: str
    carat: float
    image: str
    price: int  # rupees
    qty: int


class Order(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    items: List[OrderItem]
    subtotal: int  # rupees
    shipping_cost: int = 0
    total: int  # rupees
    currency: str = "inr"
    shipping: ShippingAddress
    status: str = "pending"  # pending | paid | failed | cancelled | expired
    payment_status: str = "pending"  # pending | paid | failed | expired
    session_id: Optional[str] = None
    payment_intent_id: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ============ HELPERS ============
def _stripe_client(request: Request) -> StripeCheckout:
    host_url = str(request.base_url)
    webhook_url = f"{host_url.rstrip('/')}/api/webhook/stripe"
    return StripeCheckout(
        api_key=os.environ["STRIPE_API_KEY"],
        webhook_url=webhook_url,
    )


async def _validate_and_price_cart(items_in: List[CartItemIn]):
    """Recompute totals from DB — never trust client-supplied amounts."""
    if not items_in:
        raise HTTPException(400, "Cart is empty")
    order_items: List[OrderItem] = []
    subtotal = 0
    for it in items_in:
        product = await _db.products.find_one({"slug": it.slug}, {"_id": 0})
        if not product:
            raise HTTPException(400, f"Product not found: {it.slug}")
        if product.get("stock", 0) < it.qty:
            raise HTTPException(400, f"Insufficient stock for {product['name']}")
        oi = OrderItem(
            product_id=product["id"],
            slug=product["slug"],
            name=product["name"],
            gemstone_type=product["gemstone_type"],
            carat=product["carat"],
            image=product["images"][0] if product.get("images") else "",
            price=int(product["price"]),
            qty=it.qty,
        )
        subtotal += oi.price * oi.qty
        order_items.append(oi)
    return order_items, subtotal


# ============ ROUTES ============
@stripe_router.post("/checkout/session")
async def create_checkout_session(payload: CheckoutSessionIn, request: Request):
    order_items, subtotal = await _validate_and_price_cart(payload.items)
    total = subtotal  # free shipping for now

    order = Order(
        items=order_items,
        subtotal=subtotal,
        total=total,
        shipping=payload.shipping,
    )
    doc = order.model_dump()

    origin = payload.origin_url.rstrip("/")
    success_url = f"{origin}/payment/success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{origin}/payment/cancel"

    checkout = _stripe_client(request)
    session_req = CheckoutSessionRequest(
        amount=float(total),
        currency="inr",
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={
            "order_id": order.id,
            "customer_email": payload.shipping.email,
            "item_count": str(sum(i.qty for i in order_items)),
        },
    )
    try:
        session = await checkout.create_checkout_session(session_req)
    except Exception as e:
        logger.exception("Stripe session creation failed")
        raise HTTPException(status_code=502, detail=f"Payment gateway error: {e}")

    doc["session_id"] = session.session_id
    await _db.orders.insert_one(doc)
    await _db.payment_transactions.insert_one({
        "session_id": session.session_id,
        "order_id": order.id,
        "amount": float(total),
        "currency": "inr",
        "status": "initiated",
        "payment_status": "pending",
        "customer_email": payload.shipping.email,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })

    return {
        "checkout_url": session.url,
        "session_id": session.session_id,
        "order_id": order.id,
    }


@stripe_router.get("/payments/status/{session_id}")
async def get_payment_status(session_id: str, request: Request):
    record = await _db.payment_transactions.find_one({"session_id": session_id}, {"_id": 0})
    if not record:
        raise HTTPException(status_code=404, detail="Transaction not found")

    if record.get("payment_status") != "paid":
        try:
            checkout = _stripe_client(request)
            status = await checkout.get_checkout_status(session_id)
            if status.payment_status == "paid" or status.status == "complete":
                now = datetime.now(timezone.utc).isoformat()
                await _db.payment_transactions.update_one(
                    {"session_id": session_id, "payment_status": {"$ne": "paid"}},
                    {"$set": {
                        "status": "completed",
                        "payment_status": "paid",
                        "updated_at": now,
                    }},
                )
                await _db.orders.update_one(
                    {"session_id": session_id, "payment_status": {"$ne": "paid"}},
                    {"$set": {"status": "paid", "payment_status": "paid", "updated_at": now}},
                )
                record = await _db.payment_transactions.find_one({"session_id": session_id}, {"_id": 0})
        except Exception as e:
            logger.warning(f"Stripe status fetch failed: {e}")

    return {
        "session_id": record["session_id"],
        "status": record["status"],
        "payment_status": record["payment_status"],
        "order_id": record.get("order_id"),
    }


@stripe_router.get("/orders/{order_id}")
async def get_order(order_id: str):
    order = await _db.orders.find_one({"id": order_id}, {"_id": 0})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@stripe_router.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("Stripe-Signature", "")
    try:
        checkout = _stripe_client(request)
        event = await checkout.handle_webhook(body, signature)
    except Exception as e:
        logger.warning(f"Webhook verification failed: {e}")
        raise HTTPException(status_code=400, detail="Invalid webhook payload")

    session_id = event.session_id
    payment_status = event.payment_status
    now = datetime.now(timezone.utc).isoformat()

    if payment_status == "paid":
        await _db.payment_transactions.update_one(
            {"session_id": session_id, "payment_status": {"$ne": "paid"}},
            {"$set": {"status": "completed", "payment_status": "paid", "updated_at": now}},
        )
        await _db.orders.update_one(
            {"session_id": session_id, "payment_status": {"$ne": "paid"}},
            {"$set": {"status": "paid", "payment_status": "paid", "updated_at": now}},
        )
    elif payment_status in ("failed", "unpaid"):
        await _db.payment_transactions.update_one(
            {"session_id": session_id},
            {"$set": {"status": "failed", "payment_status": "failed", "updated_at": now}},
        )
        await _db.orders.update_one(
            {"session_id": session_id},
            {"$set": {"status": "failed", "payment_status": "failed", "updated_at": now}},
        )

    return {"status": "ok"}
