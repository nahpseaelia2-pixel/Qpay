"""
QPay + Meta Messenger Bot
---------------------------
This server IS your chatbot now -- it replaces Chatfuel entirely. It talks
directly to Facebook/Messenger and to QPay.

Supports MULTIPLE PRODUCTS, each with its own trigger keyword(s), price,
description, and destination Facebook Group. Configure products via
environment variables on Render:

    PRODUCT_1_KEYWORDS=no.1,no1
    PRODUCT_1_AMOUNT=15000
    PRODUCT_1_DESCRIPTION=Product 1
    PRODUCT_1_GROUP_LINK=https://facebook.com/groups/xxxx
    PRODUCT_1_VIDEO_KEY=videos/product1.mp4  (optional -- object key in your R2 bucket)

    PRODUCT_2_KEYWORDS=no.2,no2
    PRODUCT_2_AMOUNT=20000
    PRODUCT_2_DESCRIPTION=Product 2
    PRODUCT_2_GROUP_LINK=https://facebook.com/groups/yyyy

    ...and so on (PRODUCT_3_..., PRODUCT_4_..., no limit).

It does four jobs:

1. GET /meta-webhook
   A one-time handshake Meta uses to verify this server is really yours.

2. POST /meta-webhook
   Receives two kinds of events from Meta:
     a) A comment on your Facebook Page post -- checked against every
        configured product's keywords. First match wins; we privately
        reply with a "Pay" button for that specific product.
     b) A button click / postback -- we create a QPay invoice for that
        product and message the customer a pay link.

3. POST /qpay-callback
   QPay calls this automatically when a customer finishes paying. This
   server verifies the payment with QPay, then messages the customer
   directly on Messenger with a confirmation + THAT PRODUCT's Facebook
   Group link.

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
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import gspread
import httpx
import boto3
from botocore.config import Config as BotoConfig
from dataclasses import dataclass
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from google.oauth2.service_account import Credentials
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

# Optional secret that guards the /test-send-video testing endpoint below.
# If you set this, you must include ?secret=<the same value> when calling
# that endpoint, so a stranger who finds the URL can't get free video
# access. Leave unset while you're still testing if you want.
TEST_ENDPOINT_SECRET = os.environ.get("TEST_ENDPOINT_SECRET", "")

# Fallback Facebook Group link, used only if a specific product doesn't
# have its own PRODUCT_N_GROUP_LINK set.
FACEBOOK_GROUP_LINK = os.environ.get("FACEBOOK_GROUP_LINK", "")

# Public reply posted under a comment when its keyword matches (visible to
# everyone, in addition to the private Messenger message). Customize this
# however you like.
COMMENT_REPLY_TEXT = os.environ.get(
    "COMMENT_REPLY_TEXT",
    "Танд захиалгын мэдээллийг Messenger-ээр илгээлээ! \U0001F4E9",
)

# Shown on the /privacy page. Fill these in with your real details.
BUSINESS_NAME = os.environ.get("BUSINESS_NAME", "This business")
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "")

# Google Sheets logging (optional -- if not configured, orders just aren't
# logged anywhere; the bot still works fine without this).
# NOTE: we open the Sheet by its ID (not by name) -- opening by name requires
# Google Drive API access too, while opening by ID only needs the Sheets
# scope we already grant the service account, so this avoids an extra API.
GOOGLE_SHEETS_CREDENTIALS_JSON = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_JSON", "")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")

# --- Video delivery settings (Cloudflare R2) ---
# Videos are stored in a Cloudflare R2 bucket. When a customer pays, we
# generate a time-limited token; clicking it hits THIS server, which
# checks the token, then redirects to a short-lived, real presigned R2
# link. R2 has zero egress fees, so bandwidth costs nothing regardless of
# video length or how many customers watch.
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "")
# How long a video LINK (the one sent to the customer) stays valid.
VIDEO_LINK_EXPIRY_HOURS = float(os.environ.get("VIDEO_LINK_EXPIRY_HOURS", "48"))


@dataclass
class Product:
    index: int
    keywords: list[str]
    amount: float
    description: str
    group_link: str
    video_key: str = ""  # object key (filename/path) of this product's video in R2, e.g. "videos/product1.mp4"

    @property
    def payload(self) -> str:
        """The postback payload used on this product's Pay button, e.g.
        'QPAY_PAY_1', 'QPAY_PAY_2'."""
        return f"QPAY_PAY_{self.index}"


def load_products() -> list[Product]:
    """Reads PRODUCT_1_..., PRODUCT_2_..., etc. from environment variables.
    Stops at the first missing number, so products must be numbered without
    gaps starting from 1.

    Falls back to the old single-product variables (META_TRIGGER_KEYWORDS,
    PRODUCT_AMOUNT, PRODUCT_DESCRIPTION, FACEBOOK_GROUP_LINK) as "product 1"
    if no PRODUCT_1_KEYWORDS is set, so existing setups keep working
    unchanged.
    """
    products: list[Product] = []
    i = 1
    while True:
        keywords_raw = os.environ.get(f"PRODUCT_{i}_KEYWORDS")
        if not keywords_raw:
            break
        keywords = [k.strip().lower() for k in keywords_raw.split(",") if k.strip()]
        amount = float(os.environ.get(f"PRODUCT_{i}_AMOUNT", "0"))
        description = os.environ.get(f"PRODUCT_{i}_DESCRIPTION", f"Product {i}")
        group_link = os.environ.get(f"PRODUCT_{i}_GROUP_LINK", "")
        video_key = os.environ.get(f"PRODUCT_{i}_VIDEO_KEY", "")
        products.append(Product(i, keywords, amount, description, group_link, video_key))
        i += 1

    if not products:
        legacy_keywords = [
            k.strip().lower()
            for k in os.environ.get("META_TRIGGER_KEYWORDS", "").split(",")
            if k.strip()
        ]
        products.append(Product(
            index=1,
            keywords=legacy_keywords,
            amount=float(os.environ.get("PRODUCT_AMOUNT", "15000")),
            description=os.environ.get("PRODUCT_DESCRIPTION", "Order"),
            group_link=os.environ.get("FACEBOOK_GROUP_LINK", ""),
        ))

    return products


PRODUCTS = load_products()


def get_qpay_settings() -> QPaySettings:
    if QPAY_ENV == "production":
        return QPaySettings.production(
            username=os.environ["QPAY_USERNAME"],
            password=os.environ["QPAY_PASSWORD"],
            invoice_code=os.environ["QPAY_INVOICE_CODE"],
        )
    return QPaySettings.sandbox()


def get_orders_sheet():
    """Returns the first worksheet of the configured Google Sheet, or None
    if Google Sheets logging isn't set up."""
    if not GOOGLE_SHEETS_CREDENTIALS_JSON or not GOOGLE_SHEET_ID:
        return None
    creds_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(GOOGLE_SHEET_ID).sheet1


def get_r2_client():
    """Returns a boto3 S3-compatible client pointed at your Cloudflare R2
    bucket, or None if R2 isn't configured."""
    if not (R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY):
        return None
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=BotoConfig(signature_version="s3v4"),
        region_name="auto",
    )


# In-memory record of video-delivery tokens. Resets on restart -- fine for
# testing, swap for a real database later.
VIDEO_TOKENS: dict[str, dict] = {}


def create_video_token(video_key: str) -> str:
    """Creates a random, hard-to-guess token that grants access to a
    specific video for a limited time. Returns the token."""
    token = uuid.uuid4().hex
    VIDEO_TOKENS[token] = {
        "video_key": video_key,
        "created_at": datetime.now(timezone.utc),
    }
    return token


def log_new_order(order_id: str, amount: float, description: str, customer_name: str = "") -> None:
    """Appends a new row to the orders sheet. Silently does nothing if
    Google Sheets isn't configured."""
    sheet = get_orders_sheet()
    if not sheet:
        return
    sheet.append_row([
        order_id,
        customer_name,
        description,
        amount,
        "PENDING",
        datetime.now(timezone.utc).isoformat(),
    ])


def mark_order_paid(order_id: str) -> None:
    """Updates the matching row's status to PAID. Silently does nothing if
    Google Sheets isn't configured or the order_id isn't found."""
    sheet = get_orders_sheet()
    if not sheet:
        return
    try:
        cell = sheet.find(order_id)
        sheet.update_cell(cell.row, 5, "PAID")  # column 5 = status
    except Exception:
        pass  # order_id not found in the sheet -- ignore


async def get_customer_name(psid: str) -> str:
    """Fetches the customer's Facebook name via Meta's User Profile API.
    Returns an empty string if unavailable (e.g. not configured, or Meta
    declines to share it -- this can happen and is not an error)."""
    if not META_PAGE_ACCESS_TOKEN:
        return ""
    url = f"{GRAPH_API_BASE}/{psid}"
    params = {"fields": "first_name,last_name", "access_token": META_PAGE_ACCESS_TOKEN}
    try:
        async with httpx.AsyncClient() as http_client:
            resp = await http_client.get(url, params=params)
            data = resp.json()
        name = f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()
        return name
    except Exception:
        return ""


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


async def send_pay_button(recipient: dict, product: Product) -> None:
    """Sends a message with a 'Pay Now' button for a SPECIFIC product."""
    await send_meta_message(
        recipient,
        {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "button",
                    "text": f"{product.description} -- {product.amount:.0f}\u20ae",
                    "buttons": [
                        {
                            "type": "postback",
                            "title": "Qpay-ээр төлөх",
                            "payload": product.payload,
                        }
                    ],
                },
            }
        },
    )


async def like_comment(comment_id: str) -> None:
    """Likes a comment as the Page -- a visible thumbs-up reaction under
    the comment. Requires the pages_manage_engagement permission on your
    Page Access Token."""
    url = f"{GRAPH_API_BASE}/{comment_id}/likes"
    params = {"access_token": META_PAGE_ACCESS_TOKEN}
    async with httpx.AsyncClient() as http_client:
        resp = await http_client.post(url, params=params)
        logger.info("Like comment response: %s %s", resp.status_code, resp.text)
        resp.raise_for_status()


async def reply_to_comment(comment_id: str, message: str) -> None:
    """Posts a PUBLIC reply visible under the comment thread -- different
    from the private Messenger message sent via send_pay_button. Also
    requires pages_manage_engagement."""
    url = f"{GRAPH_API_BASE}/{comment_id}/comments"
    params = {"access_token": META_PAGE_ACCESS_TOKEN}
    payload = {"message": message}
    async with httpx.AsyncClient() as http_client:
        resp = await http_client.post(url, params=params, json=payload)
        logger.info("Reply to comment response: %s %s", resp.status_code, resp.text)
        resp.raise_for_status()


async def create_qpay_invoice(
    order_id: str,
    amount: float,
    description: str,
    customer_name: str = "",
    psid: str = "",
    group_link: str = "",
    video_key: str = "",
) -> dict:
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

    INVOICES[order_id] = {
        "invoice_id": invoice.invoice_id,
        "status": "PENDING",
        # psid defaults to order_id for the manual /create-invoice testing
        # endpoint, where there's no real Messenger user behind the order.
        "psid": psid or order_id,
        "group_link": group_link,
        "video_key": video_key,
    }
    log_new_order(order_id, amount, description, customer_name)

    return {
        "invoice_id": invoice.invoice_id,
        "qr_text": invoice.qr_text,
        "qr_image_base64": invoice.qr_image,
        "qpay_short_url": getattr(invoice, "qPay_shortUrl", None),
    }


async def notify_customer_payment_confirmed(
    psid: str, group_link: str = "", video_key: str = ""
) -> None:
    """Messages the customer directly once QPay confirms their payment."""
    text = "\U0001F389 Төлбөр төлөгдлөө!"
    link = group_link or FACEBOOK_GROUP_LINK
    if link:
        text += f"\n\nЭнэхүү Группд нэгдэж үргэлжлүүлэн үзээрэй: {link}"
    if video_key:
        token = create_video_token(video_key)
        video_link = f"{CALLBACK_BASE_URL}/watch/{token}"
        text += (
            f"\n\nВидеог доорх холбоосоор үзнэ үү "
            f"({VIDEO_LINK_EXPIRY_HOURS:.0f} цагийн дотор хүчинтэй): {video_link}"
        )
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


def find_matching_product(comment_text: str) -> Product | None:
    """Checks the comment against every configured product's keywords, in
    order. Returns the first match, or None if nothing matches."""
    for product in PRODUCTS:
        if product.keywords and any(kw in comment_text for kw in product.keywords):
            return product
    return None


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

    product = find_matching_product(comment_text)
    if not product:
        logger.info("No product keyword matched. comment_text=%r", comment_text)
        return  # doesn't match any product's keywords -- ignore

    logger.info(
        "Product %s matched! Sending pay button to comment_id=%s",
        product.index, comment_id,
    )

    # Like the comment and post a public reply, in addition to the private
    # Messenger message. These are independent, best-effort actions -- if
    # one fails, we still want the actual pay button to go out.
    try:
        await like_comment(comment_id)
    except Exception as e:
        logger.warning("Failed to like comment %s: %s", comment_id, e)

    try:
        await reply_to_comment(comment_id, COMMENT_REPLY_TEXT)
    except Exception as e:
        logger.warning("Failed to reply to comment %s: %s", comment_id, e)

    # Private-reply with the Pay button. Note: Meta allows only ONE private
    # reply per comment, so retesting requires a fresh comment each time.
    await send_pay_button({"comment_id": comment_id}, product)


async def handle_messaging_event(event: dict) -> None:
    """Triggered for button clicks (postbacks) and regular messages."""
    sender_id = event.get("sender", {}).get("id")
    postback = event.get("postback")

    if not sender_id or not postback:
        return

    payload = postback.get("payload", "")
    if not payload.startswith("QPAY_PAY_"):
        return

    try:
        product_index = int(payload.replace("QPAY_PAY_", "", 1))
    except ValueError:
        return

    product = next((p for p in PRODUCTS if p.index == product_index), None)
    if not product:
        logger.info("Postback for unknown product index=%s", product_index)
        return

    customer_name = await get_customer_name(sender_id)
    # Unique order_id per purchase (not just the PSID) -- this way, if the
    # same customer buys more than one product, each purchase gets tracked
    # separately instead of overwriting the last one.
    order_id = f"{sender_id}-{product_index}-{uuid.uuid4().hex[:6]}"

    invoice = await create_qpay_invoice(
        order_id=order_id,
        amount=product.amount,
        description=product.description,
        customer_name=customer_name,
        psid=sender_id,
        group_link=product.group_link,
        video_key=product.video_key,
    )
    link = invoice.get("qpay_short_url") or invoice.get("qr_text")
    await send_meta_message(
        {"id": sender_id},
        {"text": f"Qpay-ээр төлөх бол энд дарна уу: {link}\nХэрэв алдаа заасан тохиолдолд 1. Дэлгэцний буланд байрлах \u00b0\u00b0\u00b0 дарж 2. Open in external browser гэж дарна уу."},
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
        mark_order_paid(order_id)
        await notify_customer_payment_confirmed(
            psid=record.get("psid", order_id),
            group_link=record.get("group_link", ""),
            video_key=record.get("video_key", ""),
        )

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


@app.post("/test-send-video")
async def test_send_video(psid: str, product_index: int = 1, secret: str = ""):
    """
    TEST-ONLY endpoint: sends a product's payment-confirmation message
    (group link + time-limited video link) directly to a Messenger user,
    WITHOUT requiring a real QPay payment.

    Use this to confirm your R2 credentials, video tokens, and Messenger
    delivery all work correctly, independent of whether a real payment
    succeeded.

    If TEST_ENDPOINT_SECRET is set, you must pass the matching ?secret=...
    or this returns 403 -- this stops a stranger who finds your Render URL
    from using this to get free video access once you're live.
    """
    if TEST_ENDPOINT_SECRET and secret != TEST_ENDPOINT_SECRET:
        raise HTTPException(status_code=403, detail="Invalid or missing secret")

    product = next((p for p in PRODUCTS if p.index == product_index), None)
    if not product:
        raise HTTPException(status_code=404, detail=f"No product with index {product_index}")

    await notify_customer_payment_confirmed(
        psid=psid,
        group_link=product.group_link,
        video_key=product.video_key,
    )
    return {
        "status": "sent",
        "psid": psid,
        "product_index": product_index,
        "had_video": bool(product.video_key),
    }


# ---------------------------------------------------------------------------
# VIDEO DELIVERY -- time-limited links to videos stored in Cloudflare R2.
# The customer never sees the real R2 URL, only a token pointing at THIS
# server. We check the token's age here, and only then generate a short-
# lived (5 minute) direct R2 link and redirect to it. R2 has zero egress
# fees, so bandwidth for video streaming/downloading never touches Render
# or costs anything, regardless of video length or view count.
# ---------------------------------------------------------------------------
VIDEO_EXPIRED_HTML = """
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Link expired</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body { font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif;
         max-width: 480px; margin: 80px auto; padding: 0 20px; color: #222;
         text-align: center; }
  h1 { font-size: 1.4em; }
</style>
</head>
<body>
<h1>This link has expired</h1>
<p>Please contact us if you still need access to your video.</p>
</body>
</html>
"""

VIDEO_PLAYER_HTML = """
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Your video</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ margin: 0; background: #000; height: 100vh; display: flex;
          align-items: center; justify-content: center; }}
  video {{ max-width: 100%; max-height: 100vh; }}
</style>
</head>
<body>
<video controls controlsList="nodownload noremoteplayback" disablePictureInPicture
       oncontextmenu="return false;" playsinline>
  <source src="/video-stream/{token}" type="video/mp4">
  Your browser doesn't support video playback.
</video>
</body>
</html>
"""


@app.get("/watch/{token}", response_class=HTMLResponse)
async def watch_video(token: str):
    """Serves a simple page with a custom video player (download button
    disabled) instead of linking straight to the file -- linking directly
    to a video file makes the BROWSER show its own native player, which
    always includes a download button we can't remove. This wrapper page
    gives us control over that."""
    record = VIDEO_TOKENS.get(token)
    if not record:
        return HTMLResponse(content=VIDEO_EXPIRED_HTML, status_code=404)

    age = datetime.now(timezone.utc) - record["created_at"]
    if age.total_seconds() > VIDEO_LINK_EXPIRY_HOURS * 3600:
        return HTMLResponse(content=VIDEO_EXPIRED_HTML, status_code=410)

    return HTMLResponse(content=VIDEO_PLAYER_HTML.format(token=token))


@app.get("/video-stream/{token}")
async def get_video(token: str):
    record = VIDEO_TOKENS.get(token)
    if not record:
        return HTMLResponse(content=VIDEO_EXPIRED_HTML, status_code=404)

    age = datetime.now(timezone.utc) - record["created_at"]
    if age.total_seconds() > VIDEO_LINK_EXPIRY_HOURS * 3600:
        return HTMLResponse(content=VIDEO_EXPIRED_HTML, status_code=410)

    r2_client = get_r2_client()
    if not r2_client or not R2_BUCKET_NAME:
        raise HTTPException(status_code=503, detail="Video storage not configured")

    presigned_url = r2_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": R2_BUCKET_NAME, "Key": record["video_key"]},
        ExpiresIn=300,  # the actual R2 link is only valid for 5 minutes
    )
    return RedirectResponse(url=presigned_url)


@app.get("/")
async def health_check():
    return {"status": "ok", "qpay_env": QPAY_ENV, "products_configured": len(PRODUCTS)}


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
