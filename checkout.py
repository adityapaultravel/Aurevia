import uuid
from dataclasses import dataclass


@dataclass
class CheckoutSessionRequest:
    amount: float
    currency: str
    success_url: str
    cancel_url: str
    metadata: dict


@dataclass
class CheckoutSession:
    session_id: str
    url: str


class StripeCheckout:
    def __init__(self, api_key: str, webhook_url: str):
        self.api_key = api_key
        self.webhook_url = webhook_url

    async def create_checkout_session(self, req: CheckoutSessionRequest):
        sid = f"sess_{uuid.uuid4().hex[:12]}"
        # Return a simple object with attributes expected by the app
        return CheckoutSession(session_id=sid, url=req.success_url.replace("{CHECKOUT_SESSION_ID}", sid))

    async def get_checkout_status(self, session_id: str):
        # Return an object with basic attributes
        class S:
            payment_status = "paid"
            status = "complete"

        return S()

    async def handle_webhook(self, body: bytes, signature: str):
        # Very naive: return a simple object
        class E:
            session_id = "unknown"
            payment_status = "paid"

        return E()
