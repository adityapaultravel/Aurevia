"""GemVault Phase-3 Stripe checkout backend tests.
Covers: POST /api/checkout/session, GET /api/payments/status/{sid},
GET /api/orders/{id}, plus regression against Phase-2 endpoints.
"""
import os
import uuid
import pytest
import requests

# Resolve BASE_URL from frontend .env (deployed preview URL)
BASE_URL = os.environ.get("REACT_APP_BACKEND_URL")
if not BASE_URL:
    try:
        with open("/app/frontend/.env") as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = line.split("=", 1)[1].strip()
                    break
    except Exception:
        pass
BASE_URL = (BASE_URL or "").rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def s():
    sess = requests.Session()
    sess.headers.update({"Content-Type": "application/json"})
    return sess


def _valid_shipping():
    return {
        "name": "TEST_Reviewer",
        "email": f"test_{uuid.uuid4().hex[:8]}@example.com",
        "phone": "+919999999999",
        "line1": "12, Bandra West",
        "line2": "Apt 4B",
        "city": "Mumbai",
        "state": "MH",
        "postal_code": "400050",
        "country": "India",
    }


# --------- Checkout Session ---------
class TestCheckoutSession:
    def test_create_valid_session_persists_order_and_txn(self, s):
        payload = {
            "items": [{"slug": "burmese-pigeon-blood-ruby", "qty": 1}],
            "shipping": _valid_shipping(),
            "origin_url": BASE_URL,
        }
        r = s.post(f"{API}/checkout/session", json=payload, timeout=30)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "checkout_url" in data and data["checkout_url"].startswith("https://checkout.stripe.com/")
        assert "session_id" in data and data["session_id"].startswith("cs_")
        assert "order_id" in data and len(data["order_id"]) >= 8

        # Verify persistence via GET /api/orders/{id}
        og = s.get(f"{API}/orders/{data['order_id']}", timeout=15)
        assert og.status_code == 200, og.text
        order = og.json()
        assert order["id"] == data["order_id"]
        assert order["session_id"] == data["session_id"]
        assert order["status"] == "pending"
        assert order["payment_status"] == "pending"
        assert isinstance(order["items"], list) and len(order["items"]) == 1
        assert order["items"][0]["slug"] == "burmese-pigeon-blood-ruby"
        assert order["items"][0]["price"] == 185000  # DB price
        assert order["subtotal"] == 185000
        assert order["total"] == 185000
        assert order["currency"] == "inr"
        assert order["shipping"]["email"] == payload["shipping"]["email"]

        # Verify payment status is pending immediately
        ps = s.get(f"{API}/payments/status/{data['session_id']}", timeout=15)
        assert ps.status_code == 200, ps.text
        st = ps.json()
        assert st["session_id"] == data["session_id"]
        assert st["payment_status"] in ("pending", "unpaid")
        assert st.get("order_id") == data["order_id"]

    def test_server_recomputes_price_ignoring_client_amount(self, s):
        # Send a bogus 'price' field client-side (extra field) — server must ignore it
        payload = {
            "items": [{"slug": "colombian-muzo-emerald", "qty": 2, "price": 1}],
            "shipping": _valid_shipping(),
            "origin_url": BASE_URL,
        }
        r = s.post(f"{API}/checkout/session", json=payload, timeout=30)
        assert r.status_code == 200, r.text
        order_id = r.json()["order_id"]

        og = s.get(f"{API}/orders/{order_id}", timeout=15)
        assert og.status_code == 200
        order = og.json()
        # DB price is 165000, qty 2 → 330000
        assert order["items"][0]["price"] == 165000
        assert order["subtotal"] == 330000
        assert order["total"] == 330000

    def test_unknown_slug_returns_400(self, s):
        payload = {
            "items": [{"slug": "does-not-exist-slug-xyz", "qty": 1}],
            "shipping": _valid_shipping(),
            "origin_url": BASE_URL,
        }
        r = s.post(f"{API}/checkout/session", json=payload, timeout=15)
        assert r.status_code == 400, r.text
        assert "not found" in r.text.lower() or "product" in r.text.lower()

    def test_empty_items_returns_400(self, s):
        payload = {
            "items": [],
            "shipping": _valid_shipping(),
            "origin_url": BASE_URL,
        }
        r = s.post(f"{API}/checkout/session", json=payload, timeout=15)
        # Could be 400 (custom) or 422 (Pydantic min length) — 400 per current impl
        assert r.status_code in (400, 422), r.text

    def test_multi_item_totals(self, s):
        payload = {
            "items": [
                {"slug": "kashmir-cornflower-blue-sapphire", "qty": 1},  # 195000
                {"slug": "solitaire-round-brilliant-diamond", "qty": 1},  # 145000
            ],
            "shipping": _valid_shipping(),
            "origin_url": BASE_URL,
        }
        r = s.post(f"{API}/checkout/session", json=payload, timeout=30)
        assert r.status_code == 200, r.text
        order_id = r.json()["order_id"]
        og = s.get(f"{API}/orders/{order_id}", timeout=15)
        order = og.json()
        assert order["subtotal"] == 195000 + 145000
        assert order["total"] == 195000 + 145000
        assert len(order["items"]) == 2


# --------- Orders GET ---------
class TestOrdersGet:
    def test_invalid_order_id_returns_404(self, s):
        r = s.get(f"{API}/orders/nonexistent-order-id-xyz", timeout=15)
        assert r.status_code == 404


# --------- Payment Status ---------
class TestPaymentStatus:
    def test_unknown_session_returns_404(self, s):
        r = s.get(f"{API}/payments/status/cs_test_bogus_session_id", timeout=15)
        assert r.status_code == 404


# --------- Regression (Phase 2) ---------
class TestRegression:
    def test_categories(self, s):
        r = s.get(f"{API}/categories", timeout=15)
        assert r.status_code == 200 and len(r.json()) >= 6

    def test_products(self, s):
        r = s.get(f"{API}/products", timeout=15)
        assert r.status_code == 200 and len(r.json()) >= 12

    def test_journal_list(self, s):
        r = s.get(f"{API}/journal", timeout=15)
        assert r.status_code == 200 and len(r.json()) >= 4

    def test_testimonials(self, s):
        r = s.get(f"{API}/testimonials", timeout=15)
        assert r.status_code == 200 and len(r.json()) >= 4

    def test_contact(self, s):
        payload = {
            "name": "TEST_Reg",
            "email": f"test_{uuid.uuid4().hex[:8]}@example.com",
            "message": "Regression check",
        }
        r = s.post(f"{API}/contact", json=payload, timeout=15)
        assert r.status_code == 200 and r.json().get("ok") is True

    def test_newsletter(self, s):
        email = f"test_{uuid.uuid4().hex[:8]}@example.com"
        r = s.post(f"{API}/newsletter", json={"email": email}, timeout=15)
        assert r.status_code == 200 and r.json().get("ok") is True
