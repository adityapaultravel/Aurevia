"""Backend tests for /api/recommendation endpoint + regression on core endpoints."""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # Read from frontend .env as fallback (tests are run from repo)
    with open("/app/frontend/.env") as fh:
        for line in fh:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                break

API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def base_payload():
    return {
        "full_name": "TEST Ravi Sharma",
        "phone": "9999999999",
        "country_code": "+91",
        "gender": "male",
        "dob": "1990-05-15",
        "tob": "10:30",
        "place_of_birth": "Jaipur, India",
        "purpose": "Career",
        "language": "English",
    }


# ============ Recommendation happy path ============
class TestRecommendationCareer:
    def test_career_returns_blue_sapphire_saturn(self, base_payload):
        r = requests.post(f"{API}/recommendation", json=base_payload, timeout=15)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["gemstone"] == "Blue Sapphire"
        assert data["planet"] == "Saturn"
        assert data["planet_symbol"] == "\u2644"  # ♄
        assert data["metal"] == "Silver / Panchdhatu"
        assert data["finger"] == "Middle Finger"
        assert data["day"] == "Saturday"
        assert "Career Growth" in data["benefits"]
        assert isinstance(data["benefits"], list) and len(data["benefits"]) >= 1
        assert data["mantra"] == "Om Sham Shanaischaraya Namah"
        assert data["gemstone_slug"] == "kashmir-cornflower-blue-sapphire"
        assert "TEST Ravi Sharma".split()[0] in data["reasoning"] or "Ravi" in data["reasoning"]


# ============ All 8 purposes mapping ============
PURPOSE_EXPECTED = [
    ("Career",           "Blue Sapphire",   "Saturn",  "kashmir-cornflower-blue-sapphire"),
    ("Business",         "Emerald",         "Mercury", "colombian-muzo-emerald"),
    ("Marriage",         "Diamond",         "Venus",   "solitaire-round-brilliant-diamond"),
    ("Health",           "Ruby",            "Sun",     "burmese-pigeon-blood-ruby"),
    ("Education",        "Emerald",         "Mercury", "colombian-muzo-emerald"),
    ("Finance",          "Yellow Sapphire", "Jupiter", None),
    ("Spiritual Growth", "Cat\u2019s Eye",  "Ketu",    None),
    ("General Guidance", "Yellow Sapphire", "Jupiter", None),
]


@pytest.mark.parametrize("purpose,gem,planet,slug", PURPOSE_EXPECTED)
def test_purpose_mapping(base_payload, purpose, gem, planet, slug):
    payload = {**base_payload, "purpose": purpose}
    r = requests.post(f"{API}/recommendation", json=payload, timeout=15)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["gemstone"] == gem, f"Expected {gem} for {purpose}, got {data['gemstone']}"
    assert data["planet"] == planet
    assert data.get("gemstone_slug") == slug, f"Slug mismatch for {purpose}: {data.get('gemstone_slug')}"


# ============ Validation ============
class TestRecommendationValidation:
    def test_invalid_purpose_returns_400(self, base_payload):
        payload = {**base_payload, "purpose": "SomethingElse"}
        r = requests.post(f"{API}/recommendation", json=payload, timeout=10)
        assert r.status_code == 400
        assert "purpose" in r.json().get("detail", "").lower()

    def test_missing_full_name_returns_422(self, base_payload):
        payload = {k: v for k, v in base_payload.items() if k != "full_name"}
        r = requests.post(f"{API}/recommendation", json=payload, timeout=10)
        assert r.status_code == 422

    def test_missing_phone_returns_422(self, base_payload):
        payload = {k: v for k, v in base_payload.items() if k != "phone"}
        r = requests.post(f"{API}/recommendation", json=payload, timeout=10)
        assert r.status_code == 422

    def test_missing_dob_returns_422(self, base_payload):
        payload = {k: v for k, v in base_payload.items() if k != "dob"}
        r = requests.post(f"{API}/recommendation", json=payload, timeout=10)
        assert r.status_code == 422


# ============ Persistence ============
class TestRecommendationPersistence:
    def test_document_inserted_into_recommendations_collection(self, base_payload):
        """After a successful POST, expect the doc to be persisted in db.recommendations."""
        import uuid
        unique_name = f"TEST persist {uuid.uuid4().hex[:8]}"
        payload = {**base_payload, "full_name": unique_name, "purpose": "Marriage"}
        r = requests.post(f"{API}/recommendation", json=payload, timeout=15)
        assert r.status_code == 200

        # Verify via Mongo directly (motor -> use pymongo sync for simplicity)
        from pymongo import MongoClient
        from dotenv import load_dotenv as _ld
        _ld("/app/backend/.env")
        mc = MongoClient(os.environ["MONGO_URL"])
        db = mc[os.environ["DB_NAME"]]
        doc = db.recommendations.find_one({"full_name": unique_name})
        assert doc is not None
        assert doc["purpose"] == "Marriage"
        assert doc["gemstone"] == "Diamond"
        assert doc["planet"] == "Venus"
        assert "id" in doc
        assert "created_at" in doc
        # Cleanup
        db.recommendations.delete_many({"full_name": {"$regex": "^TEST "}})
        mc.close()


# ============ Regression on existing endpoints ============
class TestRegression:
    def test_products_endpoint(self):
        r = requests.get(f"{API}/products", timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list) and len(data) > 0

    def test_journal_endpoint(self):
        r = requests.get(f"{API}/journal", timeout=10)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_testimonials_endpoint(self):
        r = requests.get(f"{API}/testimonials", timeout=10)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_categories_endpoint(self):
        r = requests.get(f"{API}/categories", timeout=10)
        assert r.status_code == 200

    def test_contact_endpoint(self):
        r = requests.post(f"{API}/contact", json={
            "name": "TEST Reg Contact",
            "email": "test_reg@example.com",
            "message": "regression",
        }, timeout=10)
        assert r.status_code == 200
        assert r.json().get("ok") is True

    def test_newsletter_endpoint(self):
        r = requests.post(f"{API}/newsletter", json={"email": "test_reg_news@example.com"}, timeout=10)
        assert r.status_code == 200
        assert r.json().get("ok") is True

    def test_checkout_session_endpoint(self):
        # Existing Stripe checkout endpoint should still function
        payload = {
            "items": [{"slug": "burmese-pigeon-blood-ruby", "qty": 1}],
            "shipping": {
                "name": "TEST Regression",
                "email": "test_reg@example.com",
                "phone": "9999999999",
                "line1": "1 Test Rd",
                "city": "Jaipur",
                "state": "RJ",
                "postal_code": "302001",
                "country": "India",
            },
            "origin_url": BASE_URL,
        }
        r = requests.post(f"{API}/checkout/session", json=payload, timeout=20)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "url" in data or "session_id" in data
