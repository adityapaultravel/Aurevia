from fastapi import FastAPI, APIRouter, HTTPException, Query
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import socket
import re
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional
import uuid
from datetime import datetime, timezone


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

if not os.environ.get('MONGO_URL') or not os.environ.get('DB_NAME') or not os.environ.get('JWT_SECRET'):
    raise SystemExit(
        "\n[Aurevia Gems] Missing configuration: backend/.env not found or incomplete.\n"
        "  1. Copy backend/.env.example to backend/.env\n"
        "  2. Set MONGO_URL (e.g. mongodb://localhost:27017), DB_NAME and JWT_SECRET\n"
        "  See README.md → 'Run locally'.\n"
    )

# Attempt to connect to MongoDB; if unavailable, fall back to an in-memory fake DB
mongo_url = os.environ.get('MONGO_URL')
DB_NAME = os.environ.get('DB_NAME', 'test_database')


class FakeCursor:
    def __init__(self, items):
        self._items = items

    def sort(self, *args, **kwargs):
        return self

    def limit(self, n):
        self._items = self._items[:n]
        return self

    async def to_list(self, length):
        return self._items


class FakeCollection:
    def __init__(self, items=None):
        self._items = items or []

    def find(self, query=None, projection=None):
        return FakeCursor([{k: v for k, v in it.items() if k != '_id'} for it in self._items])

    async def find_one(self, q, projection=None):
        for it in self._items:
            match = True
            for k, v in q.items():
                if it.get(k) != v:
                    match = False
                    break
            if match:
                return {k: v for k, v in it.items() if k != '_id'}
        return None

    async def count_documents(self, q=None):
        return len(self._items)

    async def insert_many(self, docs):
        self._items.extend(docs)

    async def insert_one(self, doc):
        self._items.append(doc)

    async def update_one(self, *args, **kwargs):
        return None


class FakeDB:
    def __init__(self):
        self.products = FakeCollection([])
        self.categories = FakeCollection([])
        self.journal = FakeCollection([])
        self.testimonials = FakeCollection([])
        self.gemologist_chats = FakeCollection([])
        self.recommendations = FakeCollection([])
        self.orders = FakeCollection([])
        self.payment_transactions = FakeCollection([])
        self.inquiries = FakeCollection([])
        self.newsletter = FakeCollection([])


def _parse_mongo_host_port(url: str):
    # Very small parser for mongodb://host:port/...; handles simple cases
    m = re.match(r"mongodb://([^/]+)", url)
    if not m:
        return None, None
    host_part = m.group(1).split(',')[0]
    if host_part.startswith('['):
        # IPv6 [::1]:27017
        hp = host_part.rsplit(']', 1)[0].lstrip('[')
        rest = host_part.rsplit(']', 1)[1]
        port = 27017
        if rest.startswith(':'):
            try:
                port = int(rest.lstrip(':'))
            except Exception:
                port = 27017
        return hp, port
    if ':' in host_part:
        host, port = host_part.split(':', 1)
        try:
            return host, int(port)
        except Exception:
            return host, 27017
    return host_part, 27017


if mongo_url:
    host, port = _parse_mongo_host_port(mongo_url)
    reachable = False
    if host and port:
        try:
            sock = socket.create_connection((host, port), timeout=1)
            sock.close()
            reachable = True
        except Exception:
            reachable = False
    if reachable:
        try:
            client = AsyncIOMotorClient(mongo_url, serverSelectionTimeoutMS=2000)
            db = client[DB_NAME]
        except Exception:
            db = FakeDB()
    else:
        db = FakeDB()
else:
    db = FakeDB()

app = FastAPI(title="GemVault API")
api_router = APIRouter(prefix="/api")
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from stripe_payments import stripe_router  # noqa: E402
from recommendation import recommendation_router  # noqa: E402
from gemologist_chat import gemologist_router  # noqa: E402
from admin_auth import auth_router, seed_admin, create_indexes  # noqa: E402
from admin_routes import public_router, content_router, seed_settings  # noqa: E402
from admin_media import media_router, init_storage  # noqa: E402


# ============ MODELS ============
class Product(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    slug: str
    description: str
    gemstone_type: str
    category: Optional[str] = None                # 'Precious' / 'Semi-Precious'
    price: int  # in INR (paise-free integer rupees)
    carat: float
    origin: str
    color: str
    treatment: str
    shape: Optional[str] = None
    clarity: Optional[str] = None
    cut: Optional[str] = None
    certificate_number: Optional[str] = None
    certificate_url: Optional[str] = None
    certificate_type: Optional[str] = None        # pdf | image
    status: Optional[str] = "Available"           # Available / Reserved / Sold / Out of Stock
    images: List[str]
    stock: int = 10
    featured: bool = False
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: Optional[str] = None


class Category(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    slug: str
    image: str
    description: str


class JournalPost(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    slug: str
    title: str
    excerpt: str
    body: str
    cover: str
    category: str
    read_time: int = 4
    author: str = "GemVault Atelier"
    published_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Testimonial(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    quote: str
    author: str
    role: str
    city: str
    stone: str


class ContactInquiry(BaseModel):
    name: str
    email: str
    phone: Optional[str] = None
    interest: Optional[str] = None
    message: str


class NewsletterSignup(BaseModel):
    email: str


# ============ ROUTES ============
@api_router.get("/")
async def root():
    return {"message": "GemVault API"}


@api_router.get("/categories", response_model=List[Category])
async def get_categories():
    cats = await db.categories.find({}, {"_id": 0}).to_list(100)
    return cats


@api_router.get("/products", response_model=List[Product])
async def get_products(
    gemstone_type: Optional[str] = Query(None),
    min_price: Optional[int] = Query(None),
    max_price: Optional[int] = Query(None),
    min_carat: Optional[float] = Query(None),
    max_carat: Optional[float] = Query(None),
    search: Optional[str] = Query(None),
    sort: Optional[str] = Query("featured"),
    featured: Optional[bool] = Query(None),
    limit: int = Query(100),
):
    query = {}
    if gemstone_type and gemstone_type.lower() != "all":
        query["gemstone_type"] = {"$regex": f"^{gemstone_type}$", "$options": "i"}
    if min_price is not None or max_price is not None:
        price_q = {}
        if min_price is not None:
            price_q["$gte"] = min_price
        if max_price is not None:
            price_q["$lte"] = max_price
        query["price"] = price_q
    if min_carat is not None or max_carat is not None:
        carat_q = {}
        if min_carat is not None:
            carat_q["$gte"] = min_carat
        if max_carat is not None:
            carat_q["$lte"] = max_carat
        query["carat"] = carat_q
    if search:
        query["$or"] = [
            {"name": {"$regex": search, "$options": "i"}},
            {"description": {"$regex": search, "$options": "i"}},
            {"gemstone_type": {"$regex": search, "$options": "i"}},
        ]
    if featured is not None:
        query["featured"] = featured

    sort_map = {
        "price_asc": [("price", 1)],
        "price_desc": [("price", -1)],
        "carat_asc": [("carat", 1)],
        "carat_desc": [("carat", -1)],
        "featured": [("featured", -1), ("created_at", -1)],
        "newest": [("created_at", -1)],
    }
    sort_by = sort_map.get(sort, sort_map["featured"])
    cursor = db.products.find(query, {"_id": 0}).sort(sort_by).limit(limit)
    return await cursor.to_list(limit)


@api_router.get("/products/{slug}", response_model=Product)
async def get_product(slug: str):
    product = await db.products.find_one({"slug": slug}, {"_id": 0})
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@api_router.get("/products/{slug}/related", response_model=List[Product])
async def get_related_products(slug: str, limit: int = 4):
    product = await db.products.find_one({"slug": slug}, {"_id": 0})
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    cursor = db.products.find(
        {"gemstone_type": product["gemstone_type"], "slug": {"$ne": slug}},
        {"_id": 0},
    ).limit(limit)
    return await cursor.to_list(limit)


@api_router.post("/products", response_model=Product)
async def create_product(payload: Product):
    doc = payload.model_dump()
    doc["id"] = str(uuid.uuid4())
    doc["created_at"] = datetime.now(timezone.utc).isoformat()
    await db.products.insert_one(doc)
    return doc


@api_router.get("/journal", response_model=List[JournalPost])
async def get_journal(limit: int = 20):
    cursor = db.journal.find({}, {"_id": 0}).sort([("published_at", -1)]).limit(limit)
    return await cursor.to_list(limit)


@api_router.get("/journal/{slug}", response_model=JournalPost)
async def get_journal_post(slug: str):
    post = await db.journal.find_one({"slug": slug}, {"_id": 0})
    if not post:
        raise HTTPException(status_code=404, detail="Journal post not found")
    return post


@api_router.get("/testimonials", response_model=List[Testimonial])
async def get_testimonials():
    return await db.testimonials.find({}, {"_id": 0}).to_list(50)


@api_router.post("/contact")
async def submit_contact(payload: ContactInquiry):
    doc = payload.model_dump()
    doc["id"] = str(uuid.uuid4())
    doc["created_at"] = datetime.now(timezone.utc).isoformat()
    await db.inquiries.insert_one(doc)
    return {"ok": True, "id": doc["id"]}


@api_router.post("/newsletter")
async def newsletter_signup(payload: NewsletterSignup):
    await db.newsletter.update_one(
        {"email": payload.email.lower()},
        {"$set": {"email": payload.email.lower(), "subscribed_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True,
    )
    return {"ok": True}


# ============ SEED ============
CATEGORIES_SEED = [
    {"name": "Ruby", "slug": "ruby", "description": "The passionate king of gems", "image": "https://images.pexels.com/photos/13307186/pexels-photo-13307186.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=650&w=940"},
    {"name": "Emerald", "slug": "emerald", "description": "The vivid green of paradise", "image": "https://images.pexels.com/photos/29495765/pexels-photo-29495765.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=650&w=940"},
    {"name": "Sapphire", "slug": "sapphire", "description": "Celestial blue mystique", "image": "https://images.pexels.com/photos/32988651/pexels-photo-32988651.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=650&w=940"},
    {"name": "Diamond", "slug": "diamond", "description": "Forever brilliant", "image": "https://images.pexels.com/photos/8516783/pexels-photo-8516783.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=650&w=940"},
    {"name": "Hessonite", "slug": "hessonite", "description": "The cinnamon stone", "image": "https://images.pexels.com/photos/6614217/pexels-photo-6614217.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=650&w=940"},
    {"name": "Pearl", "slug": "pearl", "description": "Ocean's timeless gift", "image": "https://images.pexels.com/photos/9429425/pexels-photo-9429425.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=650&w=940"},
    {"name": "Cat's Eye", "slug": "cats-eye", "description": "The mesmerising band of light", "image": "https://images.pexels.com/photos/3638410/pexels-photo-3638410.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=650&w=940"},
    {"name": "Coral", "slug": "coral", "description": "The living flame from ancient reefs", "image": "https://images.pexels.com/photos/7894809/pexels-photo-7894809.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=650&w=940"},
    {"name": "Yellow Sapphire", "slug": "yellow-sapphire", "description": "Golden brilliance of celestial joy", "image": "https://images.pexels.com/photos/7612652/pexels-photo-7612652.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=650&w=940"},
]

RUBY_IMG = "https://images.pexels.com/photos/13307186/pexels-photo-13307186.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=1200&w=1600"
EMERALD_IMG = "https://images.pexels.com/photos/29495765/pexels-photo-29495765.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=1200&w=1600"
SAPPHIRE_IMG = "https://images.pexels.com/photos/32988651/pexels-photo-32988651.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=1200&w=1600"
DIAMOND_IMG = "https://images.pexels.com/photos/8516783/pexels-photo-8516783.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=1200&w=1600"
HESSONITE_IMG = "https://images.pexels.com/photos/6614217/pexels-photo-6614217.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=1200&w=1600"
PEARL_IMG = "https://images.pexels.com/photos/9429425/pexels-photo-9429425.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=1200&w=1600"
CATS_EYE_IMG = "https://static.prod-images.emergentagent.com/jobs/3de17db7-2f5c-44ef-b107-8696f8b0c80c/images/b738325a24aa6bd0fee9504ab6d9f8ee1bbc342f5e8c8d4aa253319b444d5cf4.jpeg"
CORAL_IMG = "https://images.pexels.com/photos/7894809/pexels-photo-7894809.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=1200&w=1600"
YELLOW_SAPPHIRE_IMG = "https://static.prod-images.emergentagent.com/jobs/3de17db7-2f5c-44ef-b107-8696f8b0c80c/images/25e6ff74a7621de4b76f5557121d5580e73c27638ba6a809323d9b857a23a540.jpeg"

PRODUCTS_SEED = [
    # Ruby
    {"name": "Burmese Pigeon Blood Ruby", "slug": "burmese-pigeon-blood-ruby", "gemstone_type": "Ruby", "price": 185000, "carat": 3.15, "origin": "Myanmar", "color": "Pigeon Blood Red", "treatment": "Unheated", "featured": True, "images": [RUBY_IMG, PEARL_IMG, DIAMOND_IMG], "description": "A rare unheated Burmese ruby with the coveted pigeon blood hue. Independently certified — certificate provided on request. This gem embodies passion, protection, and royal heritage. Cushion cut with exceptional clarity and internal fire."},
    {"name": "Mozambique Vivid Ruby", "slug": "mozambique-vivid-ruby", "gemstone_type": "Ruby", "price": 78000, "carat": 2.10, "origin": "Mozambique", "color": "Vivid Red", "treatment": "Heat Treated", "featured": True, "images": [RUBY_IMG, EMERALD_IMG]},
    # Emerald
    {"name": "Colombian Muzo Emerald", "slug": "colombian-muzo-emerald", "gemstone_type": "Emerald", "price": 165000, "carat": 2.85, "origin": "Colombia", "color": "Deep Green", "treatment": "Minor Oil", "featured": True, "images": [EMERALD_IMG, SAPPHIRE_IMG, RUBY_IMG], "description": "Sourced from the legendary Muzo mines, this emerald reveals a mesmerizing 'jardin' of natural inclusions—each one a fingerprint of Colombian earth. Octagonal step cut. Independently certified — certificate on request."},
    {"name": "Zambian Deep Green Emerald", "slug": "zambian-deep-green-emerald", "gemstone_type": "Emerald", "price": 62000, "carat": 3.40, "origin": "Zambia", "color": "Bluish Green", "treatment": "Minor Oil", "featured": False, "images": [EMERALD_IMG, DIAMOND_IMG]},
    # Sapphire
    {"name": "Kashmir Cornflower Blue Sapphire", "slug": "kashmir-cornflower-blue-sapphire", "gemstone_type": "Sapphire", "price": 195000, "carat": 3.05, "origin": "Kashmir", "color": "Cornflower Blue", "treatment": "Unheated", "featured": True, "images": [SAPPHIRE_IMG, EMERALD_IMG, PEARL_IMG], "description": "The most coveted origin in sapphire history. This Kashmir specimen displays a velvety cornflower blue with the signature 'sleepy' luminescence. Truly museum grade."},
    {"name": "Ceylon Royal Blue Sapphire", "slug": "ceylon-royal-blue-sapphire", "gemstone_type": "Sapphire", "price": 88000, "carat": 2.75, "origin": "Sri Lanka", "color": "Royal Blue", "treatment": "Unheated", "featured": True, "images": [SAPPHIRE_IMG, RUBY_IMG]},
    # Diamond
    {"name": "Solitaire Round Brilliant Diamond", "slug": "solitaire-round-brilliant-diamond", "gemstone_type": "Diamond", "price": 145000, "carat": 1.20, "origin": "South Africa", "color": "D Colourless", "treatment": "None", "featured": True, "images": [DIAMOND_IMG, PEARL_IMG, SAPPHIRE_IMG], "description": "A hearts-and-arrows precision-cut round brilliant. D color, VVS1 clarity, triple excellent finish. Independently certified — certificate on request. The definitive engagement stone."},
    {"name": "Emerald Cut White Diamond", "slug": "emerald-cut-white-diamond", "gemstone_type": "Diamond", "price": 92000, "carat": 1.55, "origin": "Botswana", "color": "F Near Colourless", "treatment": "None", "featured": False, "images": [DIAMOND_IMG, EMERALD_IMG]},
    # Hessonite
    {"name": "Ceylon Hessonite Garnet", "slug": "ceylon-hessonite-garnet", "gemstone_type": "Hessonite", "price": 12500, "carat": 5.20, "origin": "Sri Lanka", "color": "Honey Cinnamon", "treatment": "Untreated", "featured": True, "images": [HESSONITE_IMG, RUBY_IMG, DIAMOND_IMG], "description": "Known as 'Gomed' in Vedic astrology, this warm honey-hued hessonite is revered for its cosmic properties and understated beauty. Oval mixed cut."},
    {"name": "Fanta Orange Hessonite", "slug": "fanta-orange-hessonite", "gemstone_type": "Hessonite", "price": 8500, "carat": 4.15, "origin": "Tanzania", "color": "Fanta Orange", "treatment": "Untreated", "featured": False, "images": [HESSONITE_IMG, PEARL_IMG]},
    # Pearl
    {"name": "South Sea Golden Pearl", "slug": "south-sea-golden-pearl", "gemstone_type": "Pearl", "price": 45000, "carat": 12.00, "origin": "Philippines", "color": "Golden", "treatment": "Natural", "featured": True, "images": [PEARL_IMG, DIAMOND_IMG, SAPPHIRE_IMG], "description": "A rare 12mm South Sea golden pearl with mirror-perfect surface and deep body colour. Cultured over four years in pristine Philippine waters."},
    {"name": "Akoya Japanese Pearl", "slug": "akoya-japanese-pearl", "gemstone_type": "Pearl", "price": 3200, "carat": 8.50, "origin": "Japan", "color": "White Silver", "treatment": "Natural", "featured": False, "images": [PEARL_IMG, EMERALD_IMG]},
    # Cat's Eye
    {"name": "Ceylon Cat's Eye Chrysoberyl", "slug": "ceylon-cats-eye-chrysoberyl", "gemstone_type": "Cat's Eye", "price": 95000, "carat": 2.20, "origin": "Sri Lanka", "color": "Golden Green", "treatment": "None", "featured": True, "images": [CATS_EYE_IMG, PEARL_IMG], "description": "A premium Sri Lankan cat's eye chrysoberyl with a strong, silky chatoyancy and a warm golden-green body colour. Ideal for a signature ring or talisman."},
    {"name": "Madagascar Cat's Eye", "slug": "madagascar-cats-eye", "gemstone_type": "Cat's Eye", "price": 42000, "carat": 1.75, "origin": "Madagascar", "color": "Olive Gold", "treatment": "None", "featured": False, "images": [CATS_EYE_IMG, YELLOW_SAPPHIRE_IMG]},
    # Coral
    {"name": "Italian Mediterranean Red Coral", "slug": "italian-mediterranean-red-coral", "gemstone_type": "Coral", "price": 24000, "carat": 6.5, "origin": "Italy", "color": "Fiery Red", "treatment": "Stabilized", "featured": True, "images": [CORAL_IMG, PEARL_IMG], "description": "An intensely saturated red Mediterranean coral piece, expertly polished for use in fine jewellery and heirloom pieces."},
    {"name": "Japanese Pink Coral", "slug": "japanese-pink-coral", "gemstone_type": "Coral", "price": 18500, "carat": 5.25, "origin": "Japan", "color": "Soft Pink", "treatment": "Stabilized", "featured": False, "images": [CORAL_IMG, DIAMOND_IMG]},
    # Yellow Sapphire
    {"name": "Ceylon Honey Yellow Sapphire", "slug": "ceylon-honey-yellow-sapphire", "gemstone_type": "Yellow Sapphire", "price": 87000, "carat": 3.10, "origin": "Sri Lanka", "color": "Warm Yellow", "treatment": "Heated", "featured": True, "images": [YELLOW_SAPPHIRE_IMG, SAPPHIRE_IMG], "description": "A luminous Ceylon yellow sapphire with rich honey tones and excellent clarity. Perfect for a bold signet or statement gem."},
    {"name": "Madagascar Canary Yellow Sapphire", "slug": "madagascar-canary-yellow-sapphire", "gemstone_type": "Yellow Sapphire", "price": 56000, "carat": 2.30, "origin": "Madagascar", "color": "Canary Yellow", "treatment": "Heated", "featured": False, "images": [YELLOW_SAPPHIRE_IMG, EMERALD_IMG]},
]

DEFAULT_DESCRIPTION = "A meticulously hand-selected gemstone, ethically sourced from the world's finest mining regions. Certified for authenticity and quality by our in-house gemologists, this stone represents the intersection of natural rarity and timeless craftsmanship."


JOURNAL_SEED = [
    {
        "slug": "kashmir-sapphire-legend",
        "title": "The Sleepy Blue: Why Kashmir Sapphires Remain Unmatched",
        "excerpt": "For six weeks in 1881, a Himalayan landslide revealed the most extraordinary sapphires ever mined. A century later, they still set the standard.",
        "body": "In the summer of 1881, a landslide high in the Zanskar range exposed a pocket of cornflower-blue sapphires so extraordinary that gemologists still refer to their velvety, 'sleepy' luminescence as the Kashmir standard. Within seven years the mine was exhausted. Every Kashmir sapphire that now enters the market — perhaps two or three each year at the great auction houses — traces back to that brief, brilliant window. At Aurevia Gems, we treat Kashmir provenance as sacred. Each stone is accompanied by independent laboratory certification and the full chain of custody — provided on request. To hold one is to hold a fragment of geological luck.",
        "cover": "https://images.pexels.com/photos/32988651/pexels-photo-32988651.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=1200&w=1600",
        "category": "Provenance",
        "read_time": 6,
        "author": "Aarav Mehta, GG",
    },
    {
        "slug": "muzo-emerald-jardin",
        "title": "Reading a Jardin: The Language of Colombian Emeralds",
        "excerpt": "Inclusions are not flaws — they are fingerprints. Learn how to read a Muzo emerald like a gemologist.",
        "body": "The French word 'jardin' means garden, and in emeralds it refers to the mossy tapestry of internal inclusions unique to each stone. In Colombian Muzo emeralds, this jardin is not a defect but a certificate of authenticity: three-phase inclusions of liquid, gas and crystal that cannot be replicated in a laboratory. Reading a jardin correctly separates a $5,000 emerald from a $500,000 one. In this piece our senior gemologist walks through six identifying markers he uses under 10x magnification.",
        "cover": "https://images.pexels.com/photos/29495765/pexels-photo-29495765.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=1200&w=1600",
        "category": "Craft",
        "read_time": 8,
        "author": "Priya Iyer",
    },
    {
        "slug": "vedic-astrology-gomed",
        "title": "Gomed & Rahu: The Astrology of Hessonite",
        "excerpt": "In Vedic tradition, hessonite garnet is prescribed to pacify the shadow planet Rahu. A gemologist's take on the science and the ceremony.",
        "body": "Astrological gemstones sit at the intersection of tradition and mineralogy. Hessonite — known in Sanskrit as 'Gomed' — is prescribed for Rahu, the northern lunar node associated with ambition and illusion. Whether or not one subscribes to Jyotisha, the stone itself is worth understanding: its warm cinnamon body colour comes from trace manganese, and its distinctive 'heat wave' inclusions (called scapolite needles) make it one of the easiest gems to authenticate.",
        "cover": "https://images.pexels.com/photos/6614217/pexels-photo-6614217.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=1200&w=1600",
        "category": "Tradition",
        "read_time": 5,
        "author": "Vikram Rao",
    },
    {
        "slug": "ethical-sourcing-2026",
        "title": "The 2026 Ethical Sourcing Report",
        "excerpt": "Our annual disclosure: every mine, every dealer, every certificate. Radical transparency is not a marketing claim — it is our operating manual.",
        "body": "This year we visited 14 mines across seven countries. We publish the coordinates, the names of the pit chiefs, and the fair-trade premiums we paid. Read the full report to see why we believe transparency is the only defensible future for the gemstone trade.",
        "cover": "https://images.pexels.com/photos/33257665/pexels-photo-33257665.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=1200&w=1600",
        "category": "Ethics",
        "read_time": 12,
        "author": "GemVault Atelier",
    },
]


TESTIMONIALS_SEED = [
    {"quote": "They didn't sell me a gemstone. They introduced me to it — its origin, its cutter, its history.", "author": "Ananya M.", "role": "Collector", "city": "Bengaluru", "stone": "Kashmir Sapphire, 3.05 ct"},
    {"quote": "The unheated Burmese ruby I purchased has appreciated 40% in three years. But more than that, it is genuinely beautiful.", "author": "Rohan K.", "role": "Investor", "city": "Mumbai", "stone": "Pigeon Blood Ruby, 2.80 ct"},
    {"quote": "For my wife's 25th anniversary I wanted a Muzo emerald. GemVault sourced one that no other house in Delhi could produce.", "author": "Vikram S.", "role": "Private Client", "city": "New Delhi", "stone": "Colombian Emerald, 4.10 ct"},
    {"quote": "The concierge team travelled to Jaipur with me to inspect the stone in person. That level of service is extinct almost everywhere.", "author": "Meera P.", "role": "Bride", "city": "Chennai", "stone": "Solitaire Diamond, 2.05 ct"},
]


@app.on_event("startup")
async def seed_db():
    existing_categories = [c["slug"] for c in await db.categories.find({}, {"_id": 0, "slug": 1}).to_list(200)]
    missing_categories = [c for c in CATEGORIES_SEED if c["slug"] not in existing_categories]
    if missing_categories:
        docs = [Category(**c).model_dump() for c in missing_categories]
        await db.categories.insert_many(docs)
        logger.info(f"Seeded {len(docs)} categories")

    existing_products = [p["slug"] for p in await db.products.find({}, {"_id": 0, "slug": 1}).to_list(500)]
    missing_products = [p for p in PRODUCTS_SEED if p["slug"] not in existing_products]
    if missing_products:
        docs = []
        for p in missing_products:
            data = dict(p)
            if "description" not in data:
                data["description"] = DEFAULT_DESCRIPTION
            docs.append(Product(**data).model_dump())
        await db.products.insert_many(docs)
        logger.info(f"Seeded {len(docs)} products")
    if await db.journal.count_documents({}) == 0:
        docs = [JournalPost(**j).model_dump() for j in JOURNAL_SEED]
        await db.journal.insert_many(docs)
        logger.info(f"Seeded {len(docs)} journal posts")
    if await db.testimonials.count_documents({}) == 0:
        docs = [Testimonial(**t).model_dump() for t in TESTIMONIALS_SEED]
        await db.testimonials.insert_many(docs)
        logger.info(f"Seeded {len(docs)} testimonials")

    # Admin + Settings + Storage + Indexes
    try:
        await create_indexes()
        await seed_admin()
        await seed_settings()
        init_storage()
    except Exception as e:
        logger.error(f"Admin/CMS init error: {e}")


app.include_router(api_router)
app.include_router(stripe_router)
app.include_router(recommendation_router)
app.include_router(gemologist_router)
app.include_router(auth_router)
app.include_router(public_router)
app.include_router(content_router)
app.include_router(media_router)

# Serve minimal static frontend
app.mount("/static", StaticFiles(directory=ROOT_DIR / "static"), name="static")


@app.get("/")
async def app_index():
    return FileResponse(ROOT_DIR / "static" / "index.html")

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=[o.strip() for o in os.environ.get('CORS_ORIGINS', 'http://localhost:3000').split(',') if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


@app.on_event("shutdown")
async def shutdown_db_client():
    if 'client' in globals() and client is not None:
        try:
            client.close()
        except Exception:
            pass
