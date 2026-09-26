"""AI Gemologist chat — Claude Sonnet 4.6 via Emergent LLM key.
Streams SSE responses; persists history in Mongo for multi-turn.
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient
import os
import uuid
import json
import logging

EMERGENT_LLM_AVAILABLE = True
try:
    from emergentintegrations.llm.chat import LlmChat, UserMessage, TextDelta, StreamDone
except Exception:
    EMERGENT_LLM_AVAILABLE = False

    class UserMessage:
        def __init__(self, text: str):
            self.text = text

    class TextDelta:
        def __init__(self, content: str):
            self.content = content

    class StreamDone:
        pass

    class LlmChat:
        def __init__(self, *args, **kwargs):
            pass

        def with_model(self, provider, model_name):
            return self

        async def stream_message(self, user_message):
            yield TextDelta(content="Emergent LLM integration unavailable")
            yield StreamDone()

logger = logging.getLogger(__name__)

gemologist_router = APIRouter(prefix="/api/gemologist")

mongo_url = os.environ.get("MONGO_URL")
DB_NAME = os.environ.get("DB_NAME", "test_database")
if mongo_url:
    _client = AsyncIOMotorClient(mongo_url)
    _db = _client[DB_NAME]
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
            self.gemologist_chats = FakeCollection()

    _client = None
    _db = FakeDB()

MODEL_PROVIDER = "anthropic"
MODEL_NAME = "claude-sonnet-4-6"


class RecommendationContext(BaseModel):
    model_config = ConfigDict(extra="ignore")
    gemstone: Optional[str] = None
    planet: Optional[str] = None
    benefits: Optional[List[str]] = None
    metal: Optional[str] = None
    finger: Optional[str] = None
    day: Optional[str] = None
    mantra: Optional[str] = None
    weight_ratti: Optional[str] = None
    color: Optional[str] = None
    full_name: Optional[str] = None
    purpose: Optional[str] = None


class ChatIn(BaseModel):
    session_id: str
    message: str
    context: Optional[RecommendationContext] = None


def build_system_prompt(ctx: Optional[RecommendationContext], history: List[Dict[str, Any]]) -> str:
    base = (
        "You are Aarav Mehta, GemVault's Master Gemologist and Jyotish expert with 27 years of practice. "
        "You advise HNI clients on gemstone selection, wearing rituals, mineralogy, and Vedic astrology. "
        "Your tone: warm, precise, and quietly authoritative — like a family jeweller. Never salesy. "
        "Keep answers CONCISE (2–4 short paragraphs max). Use plain prose, occasional short lists. "
        "When appropriate, gently recommend booking a private consultation for anything requiring a full chart. "
        "Do not invent product prices or claim outcomes; frame benefits as 'traditional beliefs' where appropriate. "
        "Never break character. If asked about non-gemstone topics, briefly redirect to your expertise."
    )
    if ctx and ctx.gemstone:
        ctx_block = (
            "\n\nCLIENT CONTEXT (prepared just now by the atelier):\n"
            f"- Name: {ctx.full_name or 'the client'}\n"
            f"- Purpose: {ctx.purpose or 'general guidance'}\n"
            f"- Recommended gemstone: {ctx.gemstone}\n"
            f"- Ruling planet: {ctx.planet}\n"
            f"- Ideal metal: {ctx.metal}\n"
            f"- Finger: {ctx.finger}\n"
            f"- Ideal day to wear: {ctx.day}\n"
            f"- Weight range: {ctx.weight_ratti}\n"
            f"- Colour signature: {ctx.color}\n"
            f"- Mantra: {ctx.mantra}\n"
            f"- Traditional benefits: {', '.join(ctx.benefits or [])}\n"
            "Answer questions in light of this recommendation."
        )
        base += ctx_block
    if history:
        h_block = "\n\nRECENT CONVERSATION (for continuity):\n"
        for turn in history[-8:]:  # last 4 exchanges max to keep prompt small
            role = "Client" if turn["role"] == "user" else "You (Aarav)"
            h_block += f"{role}: {turn['content']}\n"
        base += h_block
    return base


async def _load_history(session_id: str) -> List[Dict[str, Any]]:
    doc = await _db.gemologist_chats.find_one({"session_id": session_id}, {"_id": 0})
    return doc.get("messages", []) if doc else []


async def _append_history(session_id: str, role: str, content: str, ctx: Optional[Dict[str, Any]] = None):
    now = datetime.now(timezone.utc).isoformat()
    turn = {"role": role, "content": content, "at": now}
    await _db.gemologist_chats.update_one(
        {"session_id": session_id},
        {
            "$setOnInsert": {"session_id": session_id, "created_at": now, "context": ctx or {}},
            "$set": {"updated_at": now},
            "$push": {"messages": turn},
        },
        upsert=True,
    )


@gemologist_router.post("/chat")
async def chat_stream(payload: ChatIn):
    if not payload.message.strip():
        raise HTTPException(400, "Message cannot be empty")

    api_key = os.environ.get("EMERGENT_LLM_KEY")
    if not api_key:
        raise HTTPException(500, "LLM key not configured")

    history = await _load_history(payload.session_id)
    ctx_dict = payload.context.model_dump() if payload.context else None
    await _append_history(payload.session_id, "user", payload.message, ctx_dict)

    system_prompt = build_system_prompt(payload.context, history)

    chat = LlmChat(
        api_key=api_key,
        session_id=payload.session_id,
        system_message=system_prompt,
    ).with_model(MODEL_PROVIDER, MODEL_NAME)

    async def event_generator():
        assistant_text = ""
        try:
            async for ev in chat.stream_message(UserMessage(text=payload.message)):
                if isinstance(ev, TextDelta):
                    assistant_text += ev.content
                    yield f"data: {json.dumps({'delta': ev.content})}\n\n"
                elif isinstance(ev, StreamDone):
                    break
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            logger.exception("Gemologist chat failed")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            if assistant_text:
                await _append_history(payload.session_id, "assistant", assistant_text)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@gemologist_router.get("/history/{session_id}")
async def get_history(session_id: str):
    doc = await _db.gemologist_chats.find_one({"session_id": session_id}, {"_id": 0})
    if not doc:
        return {"session_id": session_id, "messages": []}
    return doc
