"""Free trial and film credits, priced by film length: one credit buys `seconds_per_credit` seconds of film.

Every account gets one free film up to `free_film_max_seconds`; longer films, and every film after it, use credits.
Credits are sold as packs through Stripe Checkout; Stripe's webhook adds them once the payment clears.
Stored as library/<user>/billing.json beside the character library and drafts.

A film is priced on the length the user asked for, so the cost is known before it starts. Its credits (or the free
film) are held from storyboard approval, where paid video starts, until it finishes, which spends them, or stops
without a video, which releases them. The hold stops one balance funding several films at once.
Billing is enforced only when sign-in and STRIPE_SECRET_KEY are both configured, so local development stays free.
"""

import json
import logging
import math
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from video_automation.auth import require_user
from video_automation.character_library import _user_dir


logger = logging.getLogger(__name__)
PRICING = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "pricing.yml").read_text(encoding="utf-8"))
# The longest film the studio allows; a length that cannot be read is priced as this, never as free.
MAX_FILM_SECONDS = 120
# A hold older than this no longer blocks new films, in case a run died without releasing it.
HOLD_SECONDS = 24 * 3600
router = APIRouter()
_lock = threading.Lock()


class CheckoutRequest(BaseModel):
    pack_id: str


def billing_enabled(user_id: str | None) -> bool:
    return user_id is not None and bool(os.getenv("STRIPE_SECRET_KEY"))


def film_seconds(duration: int | str | None) -> int:
    """The requested length in seconds, from the request's int or the graph state's "30 seconds"."""
    match = re.match(r"\s*(\d+)", str(duration or ""))
    return int(match.group(1)) if match else MAX_FILM_SECONDS


def credits_for(seconds: int) -> int:
    return max(1, math.ceil(seconds / PRICING["seconds_per_credit"]))


def _file(user_id: str):
    return _user_dir(user_id) / "billing.json"


def _load(user_id: str) -> dict:
    path = _file(user_id)
    account = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return {"free_film_used": False, "credits": 0, "holds": {}, "charged_films": {}, "payments": [], **account}


@contextmanager
def _editing(user_id: str):
    """Read, change, and write one account under a lock; nothing is written if the change raises."""
    with _lock:
        account = _load(user_id)
        yield account
        path = _file(user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(account, indent=2), encoding="utf-8")
        temporary.replace(path)


def _active_holds(account: dict) -> list[dict]:
    return [hold for hold in account["holds"].values() if time.time() - hold["at"] < HOLD_SECONDS]


def _free_film_left(account: dict) -> bool:
    return not account["free_film_used"] and not any(hold["free"] for hold in _active_holds(account))


def _spare_credits(account: dict) -> int:
    return account["credits"] - sum(hold["credits"] for hold in _active_holds(account))


def _payment(account: dict, seconds: int) -> dict | None:
    """How a film of this length would be paid: the free film, credits, or None when the account cannot afford it."""
    if seconds <= PRICING["free_film_max_seconds"] and _free_film_left(account):
        return {"free": True, "credits": 0}
    needed = credits_for(seconds)
    return {"free": False, "credits": needed} if _spare_credits(account) >= needed else None


def _payment_required(account: dict, seconds: int) -> HTTPException:
    needed = credits_for(seconds)
    spare = max(0, _spare_credits(account))
    message = f"A {seconds}-second film needs {needed} {'credit' if needed == 1 else 'credits'}; you have {spare}."
    if _free_film_left(account):
        message = f"Your free film can be up to {PRICING['free_film_max_seconds']} seconds. {message}"
    return HTTPException(status_code=402, detail={"error": message, "code": "payment_required", "credits_needed": needed})


def require_film_allowance(user_id: str | None, seconds: int) -> None:
    """Refuse to start a film the account could not pay to finish."""
    if not billing_enabled(user_id):
        return
    account = _load(user_id)
    if _payment(account, seconds) is None:
        raise _payment_required(account, seconds)


def hold_film(user_id: str | None, thread_id: str, seconds: int) -> None:
    """Reserve the free film or the film's credits before paid video starts; a film already holding them keeps them."""
    if not billing_enabled(user_id):
        return
    with _editing(user_id) as account:
        hold = account["holds"].get(thread_id)
        if hold is None:
            payment = _payment(account, seconds)
            if payment is None:
                raise _payment_required(account, seconds)
            hold = account["holds"][thread_id] = {**payment, "seconds": seconds}
        hold["at"] = time.time()


def release_film(user_id: str | None, thread_id: str) -> None:
    if not billing_enabled(user_id):
        return
    with _editing(user_id) as account:
        account["holds"].pop(thread_id, None)


def settle_film(user_id: str | None, thread_id: str, response: dict) -> None:
    """Spend a held film's payment once it has a video, keep the hold while it waits on a review, otherwise release it."""
    if not billing_enabled(user_id):
        return
    with _editing(user_id) as account:
        if thread_id not in account["holds"]:
            return
        if str(response.get("status", "")).endswith("_review"):
            return
        hold = account["holds"].pop(thread_id)
        if not response.get("video_url") or thread_id in account["charged_films"]:
            return
        if hold["free"]:
            account["free_film_used"] = True
            account["charged_films"][thread_id] = "free"
            return
        spent = min(hold["credits"], account["credits"])
        if spent < hold["credits"]:
            logger.warning("Film %s finished for %s with %s of %s credits left", thread_id, user_id, spent, hold["credits"])
        account["credits"] -= spent
        account["charged_films"][thread_id] = spent


def add_credits(user_id: str, credits: int, payment_id: str) -> bool:
    """Add a paid pack once; Stripe retries webhooks, so a payment already counted is ignored."""
    with _editing(user_id) as account:
        if payment_id in account["payments"]:
            return False
        account["payments"].append(payment_id)
        account["credits"] += credits
    logger.info("Added %s film credits for %s (payment %s)", credits, user_id, payment_id)
    return True


def _pack(pack_id: str) -> dict:
    pack = next((item for item in PRICING["packs"] if item["id"] == pack_id), None)
    if pack is None:
        raise HTTPException(status_code=404, detail="Unknown credit pack.")
    return pack


def _public_pack(pack: dict) -> dict:
    return {
        "id": pack["id"], "name": pack["name"], "credits": pack["credits"],
        "amount_cents": pack["amount_cents"], "currency": PRICING["currency"],
    }


@router.get("/api/billing")
def billing_status(user_id: str | None = Depends(require_user)) -> dict:
    if not billing_enabled(user_id):
        return {"enabled": False}
    account = _load(user_id)
    return {
        "enabled": True,
        "free_film_left": _free_film_left(account),
        "free_film_max_seconds": PRICING["free_film_max_seconds"],
        "seconds_per_credit": PRICING["seconds_per_credit"],
        "credits": account["credits"],
        # Credits not held by a film that is still being made.
        "spare_credits": max(0, _spare_credits(account)),
        "packs": [_public_pack(pack) for pack in PRICING["packs"]],
    }


@router.post("/api/billing/checkout")
def create_checkout(checkout: CheckoutRequest, request: Request, user_id: str | None = Depends(require_user)) -> dict:
    if not billing_enabled(user_id):
        raise HTTPException(status_code=404, detail="Billing is not enabled.")
    import stripe

    pack = _pack(checkout.pack_id)
    # Behind a proxy the request URL can be the internal one; APP_BASE_URL is the address people actually use.
    base_url = (os.getenv("APP_BASE_URL") or str(request.base_url)).rstrip("/")
    session = stripe.checkout.Session.create(
        api_key=os.environ["STRIPE_SECRET_KEY"],
        mode="payment",
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": PRICING["currency"],
                "unit_amount": pack["amount_cents"],
                "product_data": {
                    "name": f"{pack['name']}: {pack['credits']} film credits",
                    "description": f"{pack['credits'] * PRICING['seconds_per_credit']} seconds of film",
                },
            },
        }],
        client_reference_id=user_id,
        metadata={"user_id": user_id, "pack_id": pack["id"], "credits": str(pack["credits"])},
        success_url=f"{base_url}/?checkout=success",
        cancel_url=f"{base_url}/?checkout=cancelled",
    )
    return {"url": session.url}


@router.post("/api/stripe/webhook")
async def stripe_webhook(request: Request) -> dict:
    """Stripe calls this directly, so it is checked by Stripe's signature instead of sign-in."""
    import stripe

    secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(status_code=503, detail="Stripe webhooks are not configured.")
    try:
        event = stripe.Webhook.construct_event(await request.body(), request.headers.get("stripe-signature", ""), secret)
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise HTTPException(status_code=400, detail="Invalid Stripe signature.") from exc
    # Bank transfers and similar methods complete the session first and pay later, in a second event.
    if event["type"] in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        session = event["data"]["object"].to_dict()
        metadata = session.get("metadata") or {}
        if session.get("payment_status") == "paid" and metadata.get("user_id"):
            add_credits(metadata["user_id"], int(metadata["credits"]), session["id"])
    return {"received": True}
