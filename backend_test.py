"""GemVault Phase-2 backend API tests.
Covers new endpoints (journal, testimonials, contact, newsletter) and
regression against products/categories.
"""
import os
import uuid
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://gemvault-preview.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

# Read backend URL from frontend .env if env var not set
if "REACT_APP_BACKEND_URL" not in os.environ:
    try:
        with open("/app/frontend/.env") as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                    API = f"{BASE_URL}/api"
                    break
    except Exception:
        pass


@pytest.fixture(scope="module")
def s():
    sess = requests.Session()
    sess.headers.update({"Content-Type": "application/json"})
    return sess


# ---------- Journal ----------
class TestJournal:
    def test_list_journal(self, s):
        r = s.get(f"{API}/journal", timeout=15)
        assert r.status_code == 200, r.text
        data = r.json()
        assert isinstance(data, list)
        assert len(data) >= 4, f"expected >=4 posts, got {len(data)}"
        required = {"slug", "title", "excerpt", "body", "cover", "category", "read_time", "author"}
        missing = required - set(data[0].keys())
        assert not missing, f"missing fields: {missing}"

    def test_get_journal_by_slug(self, s):
        r = s.get(f"{API}/journal/kashmir-sapphire-legend", timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["slug"] == "kashmir-sapphire-legend"
        assert "Kashmir" in d["title"]
        assert len(d["body"]) > 200

    def test_journal_invalid_slug_404(self, s):
        r = s.get(f"{API}/journal/nope-does-not-exist", timeout=15)
        assert r.status_code == 404


# ---------- Testimonials ----------
class TestTestimonials:
    def test_list_testimonials(self, s):
        r = s.get(f"{API}/testimonials", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert len(data) >= 4
        for t in data:
            for k in ("quote", "author", "role", "city", "stone"):
                assert k in t and t[k], f"missing/empty {k} in {t}"


# ---------- Contact ----------
class TestContact:
    def test_submit_contact_valid(self, s):
        payload = {
            "name": "TEST_Reviewer",
            "email": f"test_{uuid.uuid4().hex[:8]}@example.com",
            "message": "Interested in a Kashmir sapphire.",
        }
        r = s.post(f"{API}/contact", json=payload, timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d.get("ok") is True
        assert "id" in d and isinstance(d["id"], str) and len(d["id"]) >= 8

    def test_submit_contact_missing_required(self, s):
        r = s.post(f"{API}/contact", json={"email": "x@y.com"}, timeout=15)
        assert r.status_code in (400, 422)


# ---------- Newsletter ----------
class TestNewsletter:
    def test_newsletter_signup(self, s):
        email = f"test_{uuid.uuid4().hex[:8]}@example.com"
        r = s.post(f"{API}/newsletter", json={"email": email}, timeout=15)
        assert r.status_code == 200, r.text
        assert r.json().get("ok") is True

    def test_newsletter_upsert_idempotent(self, s):
        email = f"test_{uuid.uuid4().hex[:8]}@example.com"
        r1 = s.post(f"{API}/newsletter", json={"email": email}, timeout=15)
        r2 = s.post(f"{API}/newsletter", json={"email": email}, timeout=15)
        assert r1.status_code == 200 and r2.status_code == 200


# ---------- Regression ----------
class TestRegression:
    def test_categories(self, s):
        r = s.get(f"{API}/categories", timeout=15)
        assert r.status_code == 200
        assert len(r.json()) >= 6

    def test_products_list(self, s):
        r = s.get(f"{API}/products", timeout=15)
        assert r.status_code == 200
        assert len(r.json()) >= 12

    def test_product_by_slug(self, s):
        r = s.get(f"{API}/products/burmese-pigeon-blood-ruby", timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert d["slug"] == "burmese-pigeon-blood-ruby"
        assert d["gemstone_type"] == "Ruby"

    def test_product_related(self, s):
        r = s.get(f"{API}/products/burmese-pigeon-blood-ruby/related", timeout=15)
        assert r.status_code == 200
        rel = r.json()
        assert all(p["slug"] != "burmese-pigeon-blood-ruby" for p in rel)
        assert all(p["gemstone_type"] == "Ruby" for p in rel)
