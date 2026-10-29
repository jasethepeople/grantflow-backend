"""
Stripe integration.

Everything Stripe-specific lives here so the views stay thin and the webhook
handler can be unit-tested by passing in plain dicts (as Stripe's own SDK
gives you after signature verification) without hitting the network.
"""

from __future__ import annotations

import stripe
from django.conf import settings

from accounts.models import Organization


class BillingConfigurationError(Exception):
    """Raised when Stripe isn't configured (no secret key / price IDs)."""


class BillingError(Exception):
    """Raised when a Stripe API call itself fails."""


def _client():
    api_key = getattr(settings, "STRIPE_SECRET_KEY", "")
    if not api_key:
        raise BillingConfigurationError("STRIPE_SECRET_KEY is not set.")
    stripe.api_key = api_key
    return stripe


def _price_id_for_plan(plan: str) -> str:
    price_map = {
        Organization.PLAN_STARTER: getattr(settings, "STRIPE_PRICE_STARTER", ""),
        Organization.PLAN_PRO: getattr(settings, "STRIPE_PRICE_PRO", ""),
    }
    price_id = price_map.get(plan)
    if not price_id:
        raise BillingConfigurationError(f"No Stripe price ID configured for plan '{plan}'.")
    return price_id


def _plan_for_price_id(price_id: str) -> str | None:
    if price_id == getattr(settings, "STRIPE_PRICE_STARTER", ""):
        return Organization.PLAN_STARTER
    if price_id == getattr(settings, "STRIPE_PRICE_PRO", ""):
        return Organization.PLAN_PRO
    return None


def get_or_create_customer(organization: Organization, email: str) -> str:
    """Returns the org's Stripe customer ID, creating one if it doesn't exist yet."""
    if organization.stripe_customer_id:
        return organization.stripe_customer_id

    client = _client()
    try:
        customer = client.Customer.create(
            email=email,
            name=organization.name,
            metadata={"organization_id": str(organization.id)},
        )
    except stripe.error.StripeError as exc:
        raise BillingError(f"Failed to create Stripe customer: {exc}") from exc

    organization.stripe_customer_id = customer["id"]
    organization.save(update_fields=["stripe_customer_id"])
    return customer["id"]


def create_checkout_session(organization: Organization, plan: str, email: str, success_url: str, cancel_url: str) -> str:
    """Creates a Stripe Checkout session for upgrading `organization` to `plan`. Returns the session URL."""
    client = _client()
    price_id = _price_id_for_plan(plan)
    customer_id = get_or_create_customer(organization, email)

    try:
        session = client.checkout.Session.create(
            customer=customer_id,
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={"organization_id": str(organization.id), "plan": plan},
            subscription_data={"metadata": {"organization_id": str(organization.id), "plan": plan}},
        )
    except stripe.error.StripeError as exc:
        raise BillingError(f"Failed to create checkout session: {exc}") from exc

    return session["url"]


def create_billing_portal_session(organization: Organization, return_url: str) -> str:
    """Returns a URL to Stripe's hosted billing portal for managing/cancelling a subscription."""
    if not organization.stripe_customer_id:
        raise BillingError("This organization has no Stripe customer yet — nothing to manage.")

    client = _client()
    try:
        session = client.billing_portal.Session.create(
            customer=organization.stripe_customer_id,
            return_url=return_url,
        )
    except stripe.error.StripeError as exc:
        raise BillingError(f"Failed to create billing portal session: {exc}") from exc

    return session["url"]


def verify_webhook_signature(payload: bytes, sig_header: str) -> dict:
    """Verifies and parses a raw Stripe webhook payload. Raises BillingError on failure."""
    webhook_secret = getattr(settings, "STRIPE_WEBHOOK_SECRET", "")
    if not webhook_secret:
        raise BillingConfigurationError("STRIPE_WEBHOOK_SECRET is not set.")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError) as exc:
        raise BillingError(f"Invalid webhook signature: {exc}") from exc
    return event


def apply_webhook_event(event: dict) -> None:
    """
    Applies the effect of a verified Stripe event to our data. Pure function of
    (event dict) -> DB writes, so it's testable without any real Stripe call.
    """
    event_type = event["type"]
    data = event["data"]["object"]

    if event_type == "checkout.session.completed":
        org_id = data.get("metadata", {}).get("organization_id")
        plan = data.get("metadata", {}).get("plan")
        subscription_id = data.get("subscription")
        if org_id and plan:
            Organization.objects.filter(id=org_id).update(
                plan=plan, stripe_subscription_id=subscription_id,
            )

    elif event_type in ("customer.subscription.updated", "customer.subscription.created"):
        org_id = data.get("metadata", {}).get("organization_id")
        status_ = data.get("status")
        if org_id and status_ in ("active", "trialing"):
            plan = data.get("metadata", {}).get("plan")
            if plan:
                Organization.objects.filter(id=org_id).update(plan=plan, stripe_subscription_id=data.get("id"))
        elif org_id and status_ in ("canceled", "unpaid", "incomplete_expired"):
            Organization.objects.filter(id=org_id).update(plan=Organization.PLAN_FREE)

    elif event_type == "customer.subscription.deleted":
        org_id = data.get("metadata", {}).get("organization_id")
        if org_id:
            Organization.objects.filter(id=org_id).update(
                plan=Organization.PLAN_FREE, stripe_subscription_id=None,
            )
