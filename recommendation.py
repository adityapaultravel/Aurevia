"""Astrological gemstone recommendation router."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, EmailStr, ConfigDict
from typing import Optional, List
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient
import os
import uuid

recommendation_router = APIRouter(prefix="/api")

_client = AsyncIOMotorClient(os.environ["MONGO_URL"])
_db = _client[os.environ["DB_NAME"]]


class RecommendationInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    full_name: str
    email: Optional[EmailStr] = None
    phone: str
    country_code: str = "+91"
    gender: str  # male | female | other
    dob: str  # YYYY-MM-DD
    tob: str  # HH:MM
    place_of_birth: str
    purpose: str
    weight_kg: Optional[float] = None
    language: str = "English"


class Recommendation(BaseModel):
    gemstone: str
    gemstone_slug: Optional[str] = None
    planet: str
    planet_symbol: str
    benefits: List[str]
    metal: str
    finger: str
    day: str
    mantra: str
    weight_ratti: str
    color: str
    reasoning: str


PURPOSE_MAP = {
    "Career":            ("Blue Sapphire",     "Saturn",  "♄",  ["Career Growth", "Discipline", "Financial Stability"],       "Silver / Panchdhatu", "Middle Finger", "Saturday",  "Om Sham Shanaischaraya Namah",         "5\u20137 Ratti", "Deep Royal Blue"),
    "Business":          ("Emerald",           "Mercury", "☿",  ["Sharp Intellect", "Business Acumen", "Communication"],      "Gold",                "Little Finger", "Wednesday", "Om Bum Budhaya Namah",                  "3\u20135 Ratti", "Vivid Green"),
    "Marriage":          ("Diamond",           "Venus",   "♀",  ["Harmonious Relationships", "Beauty", "Luxury"],             "Platinum / White Gold","Ring Finger",   "Friday",    "Om Shum Shukraya Namah",                "0.5\u20131.5 Carat", "Colourless Fire"),
    "Health":            ("Ruby",              "Sun",     "☀",  ["Vitality", "Confidence", "Recovery"],                       "Gold",                "Ring Finger",   "Sunday",    "Om Suryaya Namah",                       "3\u20135 Ratti", "Pigeon Blood Red"),
    "Education":         ("Emerald",           "Mercury", "☿",  ["Learning Speed", "Memory", "Analytical Thinking"],          "Gold",                "Little Finger", "Wednesday", "Om Bum Budhaya Namah",                  "3\u20135 Ratti", "Deep Green"),
    "Finance":           ("Yellow Sapphire",   "Jupiter", "♃",  ["Wealth Attraction", "Wisdom", "Prosperity"],                "Gold",                "Index Finger",  "Thursday",  "Om Brim Brihaspataye Namah",            "5\u20137 Ratti", "Golden Yellow"),
    "Spiritual Growth":  ("Cat\u2019s Eye",   "Ketu",    "☋",  ["Intuition", "Detachment", "Higher Awareness"],              "Silver / Panchdhatu", "Middle Finger", "Tuesday",   "Om Ketave Namah",                        "3\u20135 Ratti", "Honey with Chatoyance"),
    "General Guidance":  ("Yellow Sapphire",   "Jupiter", "♃",  ["Overall Prosperity", "Divine Grace", "Good Fortune"],       "Gold",                "Index Finger",  "Thursday",  "Om Brim Brihaspataye Namah",            "5\u20137 Ratti", "Golden Yellow"),
}

GEMSTONE_TO_SLUG = {
    "Ruby": "burmese-pigeon-blood-ruby",
    "Emerald": "colombian-muzo-emerald",
    "Blue Sapphire": "kashmir-cornflower-blue-sapphire",
    "Yellow Sapphire": None,  # not in catalog yet
    "Diamond": "solitaire-round-brilliant-diamond",
    "Pearl": "south-sea-golden-pearl",
    "Hessonite": "ceylon-hessonite-garnet",
    "Cat\u2019s Eye": None,
    "Red Coral": None,
}


def build_reasoning(name: str, purpose: str, planet: str, gemstone: str) -> str:
    first = name.split()[0] if name else "seeker"
    return (
        f"Dear {first}, based on your intent — {purpose.lower()} — the ruling planet "
        f"in your chart is {planet}. Its indicated gemstone is {gemstone}, "
        f"resonating with your goal on both mineralogical and traditional Jyotish levels. "
        f"For best results, energise the stone on the recommended day and wear it in direct contact with the skin."
    )


@recommendation_router.post("/recommendation", response_model=Recommendation)
async def create_recommendation(payload: RecommendationInput):
    if payload.purpose not in PURPOSE_MAP:
        raise HTTPException(status_code=400, detail="Invalid purpose.")
    gem, planet, sym, benefits, metal, finger, day, mantra, ratti, color = PURPOSE_MAP[payload.purpose]
    reasoning = build_reasoning(payload.full_name, payload.purpose, planet, gem)

    result = Recommendation(
        gemstone=gem,
        gemstone_slug=GEMSTONE_TO_SLUG.get(gem),
        planet=planet,
        planet_symbol=sym,
        benefits=benefits,
        metal=metal,
        finger=finger,
        day=day,
        mantra=mantra,
        weight_ratti=ratti,
        color=color,
        reasoning=reasoning,
    )

    doc = {
        "id": str(uuid.uuid4()),
        **payload.model_dump(),
        **result.model_dump(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await _db.recommendations.insert_one(doc)
    return result
