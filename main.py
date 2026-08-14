"""
QPay + Meta Messenger Bot
---------------------------
This server IS your chatbot now -- it replaces Chatfuel entirely. It talks
directly to Facebook/Messenger and to QPay. It does four jobs:

1. GET /meta-webhook
   A one-time handshake Meta uses to verify this server is really yours.

2. POST /meta-webhook
   Receives two kinds of events from Meta:
     a) A comment on your Facebook Page post (if it matches your trigger
        keywords, we privately reply with a "Pay" button)
     b) A button click / postback (we create a QPay invoice and message
        the customer a pay link)

3. POST /qpay-callback
   QPay calls this automatically when a customer finishes paying. This
   server verifies the payment with QPay, then messages the customer
   directly on Messenger with a confirmation + your Facebook Group link.

4. POST /create-invoice, GET /payment-status/{order_id}
   Kept from the earlier version -- useful for manual testing with tools
   like Hoppscotch.

You should NOT need to edit this file to get started -- it uses QPay's
free SANDBOX environment by default. See the guide for the Meta setup
steps (Page, App, tokens, webhook subscription).
"""

import hashlib
import hmac
import json
import logging
import os
import uuid
from decimal import Decimal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from qpay_client.v2 import AsyncQPayClient, QPaySettings
from qpay_client.v2.enums import ObjectType
from qpay_client.v2.schemas import (
    InvoiceCreateSimpleRequest,
    Offset,
    PaymentCheckRequest,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("qpay_bot")

app = FastAPI(title="QPay Meta Messenger Bot")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# CONFIGURATION -- all of this is set as Environment Variables on Render,
# never edited directly in this file. See the guide for where to find each
# value in Meta's App Dashboard / your Facebook Page settings.
# ---------------------------------------------------------------------------

QPAY_ENV = os.environ.get("QPAY_ENV", "sandbox")
CALLBACK_BASE_URL = os.environ.get("CALLBACK_BASE_URL", "https://example.com")

# --- Meta / Messenger settings ---
META_VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "")       # you make this up yourself
META_PAGE_ACCESS_TOKEN = os.environ.get("META_PAGE_ACCESS_TOKEN", "")  # from Meta App Dashboard
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")           # from Meta App Dashboard
GRAPH_API_VERSION = "v25.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# Comma-separated list, e.g. "price,order,buy" -- a comment matches if it
# contains ANY of these words (case-insensitive). Mirrors your old Chatfuel
# "Comments with keywords" trigger.
TRIGGER_KEYWORDS = [
    k.strip().lower()
    for k in os.environ.get("META_TRIGGER_KEYWORDS", "").split(",")
    if k.strip()
]

# What you're selling -- kept simple as one fixed product for now.
PRODUCT_AMOUNT = float(os.environ.get("PRODUCT_AMOUNT", "15000"))
PRODUCT_DESCRIPTION = os.environ.get("PRODUCT_DESCRIPTION", "Order")

# The Facebook Group link sent after payment is confirmed.
FACEBOOK_GROUP_LINK = os.environ.get("FACEBOOK_GROUP_LINK", "")

# Shown on the /privacy page. Fill these in with your real details.
BUSINESS_NAME = os.environ.get("BUSINESS_NAME", "This business")
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "")

PAY_BUTTON_PAYLOAD = "QPAY_PAY"


def get_qpay_settings() -> QPaySettings:
    if QPAY_ENV == "production":
        return QPaySettings.production(
            username=os.environ["QPAY_USERNAME"],
            password=os.environ["QPAY_PASSWORD"],
            invoice_code=os.environ["QPAY_INVOICE_CODE"],
        )
    return QPaySettings.sandbox()


# In-memory "database". Resets on restart -- fine for testing, swap for a
# real database later.
INVOICES: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# META / MESSENGER HELPERS
# ---------------------------------------------------------------------------

def verify_meta_signature(raw_body: bytes, signature_header: str) -> None:
    """
    Confirms a webhook request genuinely came from Meta, not an impersonator.
    Meta signs every request using your App Secret.
    """
    if not META_APP_SECRET:
        return  # not configured yet -- skip verification (fine for early testing)

    if not signature_header or not signature_header.startswith("sha256="):
        raise HTTPException(status_code=403, detail="Missing signature")

    expected = hmac.new(
        META_APP_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    provided = signature_header.split("sha256=", 1)[1]

    if not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=403, detail="Invalid signature")


async def send_meta_message(recipient: dict, message: dict) -> None:
    """
    Low-level helper: POSTs to Meta's Send API.
    `recipient` is either {"id": "<PSID>"} for a normal Messenger user,
    or {"comment_id": "<comment id>"} for a private reply to a comment.
    """
    url = f"{GRAPH_API_BASE}/me/messages"
    params = {"access_token": META_PAGE_ACCESS_TOKEN}
    payload = {
        "recipient": recipient,
        "message": message,
        "messaging_type": "RESPONSE",
    }
    async with httpx.AsyncClient() as http_client:
        resp = await http_client.post(url, params=params, json=payload)
        logger.info("Meta Send API response: %s %s", resp.status_code, resp.text)
        resp.raise_for_status()


async def send_pay_button(recipient: dict) -> None:
    """Sends a message with a 'Pay Now' button that triggers the QPay flow."""
    await send_meta_message(
        recipient,
        {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "button",
                    "text": f"{PRODUCT_DESCRIPTION} -- {PRODUCT_AMOUNT:.0f}\u20ae",
                    "buttons": [
                        {
                            "type": "postback",
                            "title": "Pay with QPay",
                            "payload": PAY_BUTTON_PAYLOAD,
                        }
                    ],
                },
            }
        },
    )


async def create_qpay_invoice(order_id: str, amount: float, description: str) -> dict:
    """
    Shared invoice-creation logic, used by both /create-invoice (manual
    testing) and the real Messenger postback handler.
    """
    settings = get_qpay_settings()
    async with AsyncQPayClient(settings=settings) as client:
        invoice = await client.invoice_create(
            InvoiceCreateSimpleRequest(
                sender_invoice_no=order_id,
                invoice_receiver_code="terminal",
                invoice_description=description,
                amount=Decimal(str(amount)),
                callback_url=f"{CALLBACK_BASE_URL}/qpay-callback?order_id={order_id}",
            )
        )

    INVOICES[order_id] = {"invoice_id": invoice.invoice_id, "status": "PENDING"}

    return {
        "invoice_id": invoice.invoice_id,
        "qr_text": invoice.qr_text,
        "qr_image_base64": invoice.qr_image,
        "qpay_short_url": getattr(invoice, "qPay_shortUrl", None),
    }


async def notify_customer_payment_confirmed(psid: str) -> None:
    """Messages the customer directly once QPay confirms their payment."""
    text = "\U0001F389 Payment confirmed! Thanks for your order."
    if FACEBOOK_GROUP_LINK:
        text += f"\n\nJoin our group here: {FACEBOOK_GROUP_LINK}"
    await send_meta_message({"id": psid}, {"text": text})


# ---------------------------------------------------------------------------
# 1) META WEBHOOK VERIFICATION (Meta calls this once, when you connect the
#    webhook in the App Dashboard)
# ---------------------------------------------------------------------------
@app.get("/meta-webhook")
async def verify_meta_webhook(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == META_VERIFY_TOKEN:
        return PlainTextResponse(content=challenge or "")

    raise HTTPException(status_code=403, detail="Verification failed")


# ---------------------------------------------------------------------------
# 2) META WEBHOOK EVENTS (comments + button clicks)
# ---------------------------------------------------------------------------
@app.post("/meta-webhook")
async def receive_meta_webhook(request: Request):
    raw_body = await request.body()
    verify_meta_signature(raw_body, request.headers.get("X-Hub-Signature-256", ""))

    data = json.loads(raw_body)
    logger.info("Incoming webhook payload: %s", json.dumps(data))

    for entry in data.get("entry", []):
        # -- Comments on your Page's posts --
        for change in entry.get("changes", []):
            if change.get("field") == "feed":
                await handle_feed_change(change.get("value", {}))

        # -- Messenger events: button clicks, messages --
        for messaging_event in entry.get("messaging", []):
            await handle_messaging_event(messaging_event)

    return {"status": "ok"}


async def handle_feed_change(value: dict) -> None:
    """Triggered when someone comments on your Page's post."""
    logger.info("Feed change value: %s", json.dumps(value))

    if value.get("item") != "comment" or value.get("verb") != "add":
        logger.info(
            "Ignored: item=%r verb=%r (expected item='comment', verb='add')",
            value.get("item"), value.get("verb"),
        )
        return

    comment_text = (value.get("message") or "").lower()
    comment_id = value.get("comment_id")

    if not comment_id:
        logger.info("Ignored: no comment_id in payload")
        return

    if TRIGGER_KEYWORDS and not any(kw in comment_text for kw in TRIGGER_KEYWORDS):
        logger.info(
            "No keyword match. comment_text=%r trigger_keywords=%r",
            comment_text, TRIGGER_KEYWORDS,
        )
        return  # doesn't match any trigger keyword -- ignore

    logger.info("Keyword matched! Sending pay button to comment_id=%s", comment_id)
    # Private-reply with the Pay button. Note: Meta allows only ONE private
    # reply per comment, so retesting requires a fresh comment each time.
    await send_pay_button({"comment_id": comment_id})


async def handle_messaging_event(event: dict) -> None:
    """Triggered for button clicks (postbacks) and regular messages."""
    sender_id = event.get("sender", {}).get("id")
    postback = event.get("postback")

    if not sender_id or not postback:
        return

    if postback.get("payload") == PAY_BUTTON_PAYLOAD:
        invoice = await create_qpay_invoice(
            order_id=sender_id,
            amount=PRODUCT_AMOUNT,
            description=PRODUCT_DESCRIPTION,
        )
        link = invoice.get("qpay_short_url") or invoice.get("qr_text")
        await send_meta_message(
            {"id": sender_id},
            {"text": f"Qpay-ээр төлөх бол энд дарна уу. Хэрэв алдаа заасан тохиолдолд 1. Дэлгэцний баруун дээд буланд байрлах 3н цэг ---> 2. Open in external browser гэж дарна уу: {link}"},
        )


# ---------------------------------------------------------------------------
# 3) QPAY CALLBACK -- called automatically BY QPAY when a payment completes
# ---------------------------------------------------------------------------
@app.post("/qpay-callback")
async def qpay_callback(order_id: str):
    record = INVOICES.get(order_id)
    if not record:
        # QPay still expects "SUCCESS" even if we don't recognize the order,
        # otherwise it will keep retrying forever.
        return "SUCCESS"

    settings = get_qpay_settings()
    async with AsyncQPayClient(settings=settings) as client:
        result = await client.payment_check(
            PaymentCheckRequest(
                object_type=ObjectType.invoice,
                object_id=record["invoice_id"],
                offset=Offset(page_number=1, page_limit=100),
            )
        )

    if result.count > 0:
        record["status"] = "PAID"
        # order_id IS the customer's Messenger PSID (we set it that way in
        # handle_messaging_event above), so we can message them directly.
        await notify_customer_payment_confirmed(psid=order_id)

    return "SUCCESS"


# ---------------------------------------------------------------------------
# 4) MANUAL TESTING ENDPOINTS (unchanged from the Chatfuel-era version --
#    handy for testing with Hoppscotch without needing a real Messenger
#    conversation)
# ---------------------------------------------------------------------------
class CreateInvoiceRequest(BaseModel):
    order_id: str | None = None
    amount: float
    description: str


class CreateInvoiceResponse(BaseModel):
    invoice_id: str
    qr_text: str
    qr_image_base64: str
    qpay_short_url: str | None = None


@app.post("/create-invoice", response_model=CreateInvoiceResponse)
async def create_invoice(payload: CreateInvoiceRequest):
    order_id = payload.order_id or f"AUTO-{uuid.uuid4().hex[:12]}"
    invoice = await create_qpay_invoice(order_id, payload.amount, payload.description)
    return CreateInvoiceResponse(**invoice)


@app.get("/payment-status/{order_id}")
async def payment_status(order_id: str):
    record = INVOICES.get(order_id)
    if not record:
        raise HTTPException(status_code=404, detail="Unknown order_id")
    return {"order_id": order_id, "status": record["status"]}


@app.get("/")
async def health_check():
    return {"status": "ok", "qpay_env": QPAY_ENV}


# ---------------------------------------------------------------------------
# 5) PRIVACY POLICY -- required by Meta before your app can go Live.
#    Hosted right here so there's nothing extra to deploy. Visit
#    https://<your-render-url>/privacy to see it, and paste that same URL
#    into Meta's App Settings -> Basic -> Privacy Policy URL.
# ---------------------------------------------------------------------------
PRIVACY_POLICY_HTML = f"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Privacy Policy</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif;
          max-width: 700px; margin: 40px auto; padding: 0 20px; color: #222; line-height: 1.6; }}
  h1 {{ font-size: 1.6em; }}
  h2 {{ font-size: 1.15em; margin-top: 1.6em; }}
  footer {{ margin-top: 3em; color: #666; font-size: 0.9em; }}
</style>
</head>
<body>
<h1>Privacy Policy</h1>
<p>This policy explains how {BUSINESS_NAME} ("we", "us") handles information
when you interact with our Facebook Page and Messenger bot.</p>

<h2>What we collect</h2>
<p>When you comment on our posts or message our Page, we receive your
Facebook name and a unique Messenger ID (PSID) from Meta. We do not receive
your password, email, or friends list. When you choose to pay for an order,
we also process the order amount and description, and a payment status
(paid / not paid) from our payment provider, QPay.</p>

<h2>How we use it</h2>
<p>We use this information solely to respond to your comments and messages,
generate a payment request for orders you initiate, confirm when a payment
has been completed, and grant access to any purchased content or group
associated with that order (for example, a Facebook Group invite).</p>

<h2>Who we share it with</h2>
<p>Order amount and a reference ID are shared with QPay (our payment
processor) solely to generate and verify payment. We do not sell or share
your information with advertisers or any other third party.</p>

<h2>How long we keep it</h2>
<p>We retain order and payment records only as long as needed to fulfill
your order and for basic bookkeeping. You may ask us to delete your data
at any time using the contact details below.</p>

<h2>Your choices</h2>
<p>You can stop messaging our Page at any time. You can also block or
report our Page through Facebook's own tools. To request access to or
deletion of any data we hold about you, contact us using the details below.</p>

<h2>Contact</h2>
<p>{"Email: " + CONTACT_EMAIL if CONTACT_EMAIL else
   "Contact us via Facebook Messenger through our Page."}</p>

<footer>Last updated: this page reflects the current version of our bot's
data handling as described above.</footer>
</body>
</html>
"""


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_policy():
    return PRIVACY_POLICY_HTML
