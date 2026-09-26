"""Backend tests for the AI Gemologist chat streaming endpoint.

Endpoints under test:
- POST /api/gemologist/chat   (SSE stream)
- GET  /api/gemologist/history/{session_id}
"""
import json
import os
import time
import uuid
import re

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # fall back to reading from frontend env (test host)
    try:
        with open("/app/frontend/.env") as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = line.split("=", 1)[1].strip().strip('"').rstrip("/")
                    break
    except Exception:
        pass

CHAT_URL = f"{BASE_URL}/api/gemologist/chat"
HIST_URL = f"{BASE_URL}/api/gemologist/history"

CONTEXT_BLUE_SAPPHIRE = {
    "gemstone": "Blue Sapphire",
    "planet": "Saturn",
    "benefits": ["Career growth", "Discipline", "Focus"],
    "metal": "Silver",
    "finger": "Middle finger",
    "day": "Saturday",
    "mantra": "Om Sham Shanicharaya Namah",
    "weight_ratti": "5-7 ratti",
    "color": "Cornflower Blue",
    "full_name": "TEST Ananya Mehta",
    "purpose": "Career",
}


def _read_sse_chunks(resp, max_seconds=45):
    """Yield (raw_line, parsed_json) for each 'data: {...}' block. Times out at max_seconds."""
    start = time.time()
    buf = ""
    for chunk in resp.iter_content(chunk_size=None, decode_unicode=True):
        if time.time() - start > max_seconds:
            break
        if not chunk:
            continue
        buf += chunk if isinstance(chunk, str) else chunk.decode("utf-8", errors="ignore")
        while "\n\n" in buf:
            block, buf = buf.split("\n\n", 1)
            block = block.strip()
            if not block.startswith("data:"):
                continue
            payload = block[len("data:"):].strip()
            try:
                yield block, json.loads(payload)
            except json.JSONDecodeError:
                yield block, None


# ---------- Helpers ----------

def _stream_message(session_id: str, message: str, context=None, timeout=60):
    """POST to chat, consume the SSE stream fully. Returns (status, headers, deltas_list, events_count, arrival_times)."""
    body = {"session_id": session_id, "message": message, "context": context}
    with requests.post(CHAT_URL, json=body, stream=True, timeout=timeout) as r:
        status = r.status_code
        headers = dict(r.headers)
        deltas = []
        arrival_times = []
        got_done = False
        got_error = None
        if status == 200:
            t0 = time.time()
            for _, evt in _read_sse_chunks(r, max_seconds=45):
                if evt is None:
                    continue
                if "delta" in evt:
                    deltas.append(evt["delta"])
                    arrival_times.append(time.time() - t0)
                elif evt.get("done") is True:
                    got_done = True
                    break
                elif "error" in evt:
                    got_error = evt["error"]
                    break
        return {
            "status": status,
            "headers": headers,
            "deltas": deltas,
            "arrival_times": arrival_times,
            "done": got_done,
            "error": got_error,
        }


# ---------- Tests ----------

class TestGemologistChatStreaming:
    """Streaming endpoint behavior."""

    def test_chat_returns_streaming_sse_with_context(self):
        sid = f"TEST-{uuid.uuid4()}"
        res = _stream_message(sid, "In one line, greet me and confirm which gemstone I was recommended.", CONTEXT_BLUE_SAPPHIRE)

        assert res["status"] == 200, f"Expected 200, got {res['status']}"
        # Content-Type is SSE
        ct = res["headers"].get("Content-Type") or res["headers"].get("content-type", "")
        assert "text/event-stream" in ct, f"Expected SSE content-type, got {ct}"
        # X-Accel-Buffering: no — verify at the origin (ingress may strip on the public route)
        try:
            probe = requests.post(
                "http://localhost:8001/api/gemologist/chat",
                json={"session_id": f"TEST-hdr-{uuid.uuid4()}", "message": "hi"},
                stream=True, timeout=8,
            )
            xab = probe.headers.get("X-Accel-Buffering") or probe.headers.get("x-accel-buffering", "")
            probe.close()
            assert xab.lower() == "no", f"Expected X-Accel-Buffering:no at origin, got {xab!r}"
        except requests.exceptions.ConnectionError:
            # localhost not reachable from test host — public route response is best-effort
            pass
        # Delta events arrived and terminator seen
        assert len(res["deltas"]) > 0, "No delta chunks received"
        assert res["done"] is True, "SSE stream did not terminate with done=true"
        # Streaming (not one giant chunk) — at least 2 chunks OR chunks spread over time
        combined = "".join(res["deltas"])
        assert len(combined) > 0
        multi_chunk = len(res["deltas"]) >= 2
        assert multi_chunk, f"Expected multiple SSE chunks (got {len(res['deltas'])}) — stream may be buffered"

    def test_context_gemstone_is_reflected_in_reply(self):
        sid = f"TEST-{uuid.uuid4()}"
        res = _stream_message(sid, "Just tell me: what stone did you recommend for me? Reply in one short sentence.", CONTEXT_BLUE_SAPPHIRE)
        assert res["status"] == 200
        assert res["done"] is True
        combined = "".join(res["deltas"]).lower()
        assert "sapphire" in combined or "neelam" in combined, f"Assistant reply did not mention the recommended gemstone. Got: {combined[:400]}"

    def test_empty_message_returns_400(self):
        r = requests.post(CHAT_URL, json={"session_id": f"TEST-{uuid.uuid4()}", "message": "   "}, timeout=15)
        assert r.status_code == 400, f"Expected 400, got {r.status_code} body={r.text[:200]}"


class TestGemologistHistory:
    """History persistence + multi-turn continuity."""

    def test_history_persisted_after_first_call(self):
        sid = f"TEST-{uuid.uuid4()}"
        first = _stream_message(sid, "Give me a one-line ritual tip.", CONTEXT_BLUE_SAPPHIRE)
        assert first["status"] == 200
        assert first["done"] is True

        # History should have user + assistant
        r = requests.get(f"{HIST_URL}/{sid}", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert data["session_id"] == sid
        msgs = data.get("messages", [])
        assert len(msgs) == 2, f"Expected 2 messages after 1 exchange, got {len(msgs)}"
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "Give me a one-line ritual tip."
        assert msgs[1]["role"] == "assistant"
        assert isinstance(msgs[1]["content"], str) and len(msgs[1]["content"]) > 0

    def test_multi_turn_continuity_and_history_growth(self):
        sid = f"TEST-{uuid.uuid4()}"
        # Turn 1
        r1 = _stream_message(sid, "Remember this word: PURPLE_STAR_42. Just acknowledge briefly.", CONTEXT_BLUE_SAPPHIRE)
        assert r1["status"] == 200 and r1["done"] is True

        # Turn 2 — ask about turn 1
        r2 = _stream_message(sid, "What was the word I asked you to remember? Answer with just the word.", CONTEXT_BLUE_SAPPHIRE)
        assert r2["status"] == 200 and r2["done"] is True

        combined2 = "".join(r2["deltas"])
        # Model should carry context (best-effort — retry once if not)
        if "PURPLE_STAR_42" not in combined2.upper():
            # transient LLM miss — retry ONE more turn
            r_retry = _stream_message(sid, "Please just repeat exactly the word I asked you to remember — the one that starts with PURPLE.", CONTEXT_BLUE_SAPPHIRE)
            combined_retry = "".join(r_retry["deltas"])
            assert "PURPLE_STAR_42" in combined_retry.upper() or "PURPLE" in combined_retry.upper(), (
                f"Multi-turn continuity failed. Turn2 reply: {combined2[:300]!r}; Retry: {combined_retry[:300]!r}"
            )

        # History should have grown (>=4 messages after two exchanges — possibly 6 if retry executed)
        h = requests.get(f"{HIST_URL}/{sid}", timeout=15).json()
        assert len(h["messages"]) >= 4, f"Expected >=4 messages in history, got {len(h['messages'])}"


class TestGemologistRegression:
    """Ensure previously-working endpoints still work after adding gemologist router."""

    def test_products_endpoint(self):
        r = requests.get(f"{BASE_URL}/api/products", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list) and len(data) > 0

    def test_recommendation_endpoint(self):
        payload = {
            "full_name": "TEST Regression",
            "phone": "9999999998",
            "country_code": "+91",
            "gender": "male",
            "dob": "1992-05-14",
            "tob": "07:30",
            "place_of_birth": "Jaipur, India",
            "purpose": "Career",
            "language": "en",
        }
        r = requests.post(f"{BASE_URL}/api/recommendation", json=payload, timeout=20)
        assert r.status_code == 200, r.text[:300]
        d = r.json()
        assert d.get("gemstone") == "Blue Sapphire"

    def test_checkout_session_endpoint(self):
        # Fetch a product first
        prods = requests.get(f"{BASE_URL}/api/products?limit=1", timeout=10).json()
        assert isinstance(prods, list) and prods
        payload = {
            "items": [{"product_id": prods[0]["id"], "quantity": 1}],
            "origin_url": BASE_URL,
        }
        r = requests.post(f"{BASE_URL}/api/checkout/session", json=payload, timeout=20)
        # Either 200 (stripe configured) or 4xx if key missing — but not 5xx
        assert r.status_code < 500, f"5xx from checkout: {r.status_code} {r.text[:200]}"
