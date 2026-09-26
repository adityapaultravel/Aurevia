import asyncio
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass
class UserMessage:
    text: str


@dataclass
class TextDelta:
    content: str


class StreamDone:
    pass


class LlmChat:
    def __init__(self, api_key: str, session_id: str, system_message: str = ""):
        self.api_key = api_key
        self.session_id = session_id
        self.system_message = system_message

    def with_model(self, provider: str, name: str):
        return self

    async def stream_message(self, user_message: UserMessage) -> AsyncIterator:
        # Very simple generator that yields a short response then completes
        await asyncio.sleep(0.01)
        yield TextDelta(content=f"Echo: {user_message.text}")
        return
