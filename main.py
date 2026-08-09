"""
QPay Chatbot Bridge Server
---------------------------
This small server sits between your chatbot (Chatfuel now, Meta Messenger
later) and QPay. It does two jobs:

1. POST /create-invoice
   Your chatbot calls this when a customer wants to pay. This server asks
   QPay to create an invoice and sends back a QR code (as an image URL and
   as text) that the chatbot can show to the customer.

2. POST /qpay-callback
   QPay calls this automatically the moment a customer finishes paying.
   This server double-checks the payment with QPay, then (later) will be
   the place where we tell the chatbot to send a "Payment received!"
   message back to the customer.

You should NOT need to edit this file to get started -- it already uses
QPay's free SANDBOX (test) environment. Real credentials are only needed
later, when you're ready to accept real money (see the guide).
"""

import os
import uuid
from decimal import Decimal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from qpay_client.v2 import AsyncQPayClient, QPaySettings
from qpay_client.v2.enums import ObjectType
from qpay_client.v2.schemas import (
    InvoiceCreateSimpleRequest,
    Offset,
    PaymentCheckRequest,
)

app = FastAPI(title="QPay Chatbot Bridge")

# Allow browser-based tools (Hoppscotch, your future admin dashboard, etc.)
# and your chatbot platform to call this server directly. Without this,
# browsers silently block the request and show a generic "Network Error".
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
# By default this runs against QPay's free SANDBOX environment using their
# public test credentials -- no signup needed to try it out.
#
# When you're ready to go live, you set three environment variables on your
# hosting platform (Render, etc.) instead of editing this file:
#   QPAY_ENV=production
#   QPAY_USERNAME=<your real QPay merchant username>
#   QPAY_PASSWORD=<your real QPay merchant password>
#   QPAY_INVOICE_CODE=<your real QPay invoice code>
#
# CALLBACK_BASE_URL must be set to the public URL of THIS server once it's
# deployed (e.g. https://your-app-name.onrender.com). QPay uses this to know
# where to send payment notifications.

QPAY_ENV = os.environ.get("QPAY_ENV", "sandbox")
CALLBACK_BASE_URL = os.environ.get("CALLBACK_BASE_URL", "https://example.com")


def get_qpay_settings() -> QPaySettings:
    if QPAY_ENV == "production":
        return QPaySettings.production(
            username=os.environ["QPAY_USERNAME"],
            password=os.environ["QPAY_PASSWORD"],
            invoice_code=os.environ["QPAY_INVOICE_CODE"],
        )
    return QPaySettings.sandbox()


# In-memory "database" of invoices we've created, so the callback step can
# look them up. This resets every time the server restarts -- fine for
# testing, but for a real business you'd swap this for a real database
# later (a developer can help with that step when you're ready).
INVOICES: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# 1) CREATE INVOICE  -- called by your chatbot
# ---------------------------------------------------------------------------
class CreateInvoiceRequest(BaseModel):
    order_id: str | None = None   # any unique ID, e.g. "ORDER-1001". Auto-generated if not given.
    amount: float           # amount in MNT (Mongolian Tugrik), e.g. 15000
    description: str        # shown to the customer, e.g. "1x Coffee Mug"


class CreateInvoiceResponse(BaseModel):
    invoice_id: str
    qr_text: str
    qr_image_base64: str
    qpay_short_url: str | None = None


@app.post("/create-invoice", response_model=CreateInvoiceResponse)
async def create_invoice(payload: CreateInvoiceRequest):
    # If Chatfuel didn't send a real order_id (e.g. its "Test the Request"
    # preview button, which has no real user attached), generate one so
    # the request still succeeds.
    order_id = payload.order_id or f"AUTO-{uuid.uuid4().hex[:12]}"

    settings = get_qpay_settings()

    async with AsyncQPayClient(settings=settings) as client:
        invoice = await client.invoice_create(
            InvoiceCreateSimpleRequest(
                sender_invoice_no=order_id,
                invoice_receiver_code="terminal",
                invoice_description=payload.description,
                amount=Decimal(str(payload.amount)),
                callback_url=(
                    f"{CALLBACK_BASE_URL}/qpay-callback"
                    f"?order_id={order_id}"
                ),
            )
        )

    # Remember this invoice so /qpay-callback can find it later.
    INVOICES[order_id] = {
        "invoice_id": invoice.invoice_id,
        "status": "PENDING",
    }

    return CreateInvoiceResponse(
        invoice_id=invoice.invoice_id,
        qr_text=invoice.qr_text,
        qr_image_base64=invoice.qr_image,
        qpay_short_url=getattr(invoice, "qPay_shortUrl", None),
    )


# ---------------------------------------------------------------------------
# 2) PAYMENT STATUS -- your chatbot can poll this to check "has this order
#    been paid yet?" (useful if you're not ready to wire up the live
#    callback-to-chat step yet)
# ---------------------------------------------------------------------------
@app.get("/payment-status/{order_id}")
async def payment_status(order_id: str):
    record = INVOICES.get(order_id)
    if not record:
        raise HTTPException(status_code=404, detail="Unknown order_id")
    return {"order_id": order_id, "status": record["status"]}


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
        # --- NEXT STEP FOR LATER ---
        # This is the exact spot where, once you've connected a real
        # chatbot API key, you'd send a message back to the customer like
        # "Payment received! Your order is confirmed." A developer can
        # add that call here when you're ready to wire it up.

    # QPay requires HTTP 200 with the exact text "SUCCESS" in response.
    return "SUCCESS"


@app.get("/")
async def health_check():
    return {"status": "ok", "qpay_env": QPAY_ENV}
