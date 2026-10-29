import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import stripe
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Membership, Organization, User
from billing import stripe_service
from billing.models import WebhookEvent
from billing.plans import PLAN_LIMITS, get_limits
from grants.models import Budget, Grant, GrantSection, SectionVersion


class PlanLimitsTests(TestCase):
    def test_free_plan_limits(self):
        limits = get_limits(Organization.PLAN_FREE)
        self.assertEqual(limits["max_grants"], 1)
        self.assertEqual(limits["ai_generations_per_month"], 5)

    def test_unknown_plan_falls_back_to_free(self):
        limits = get_limits("nonexistent")
        self.assertEqual(limits, PLAN_LIMITS[Organization.PLAN_FREE])

    def test_pro_plan_has_unlimited_grants(self):
        limits = get_limits(Organization.PLAN_PRO)
        self.assertIsNone(limits["max_grants"])


class CheckoutSessionViewTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="owner@example.org", password="password-o-123")
        self.viewer = User.objects.create_user(email="viewer@example.org", password="password-v-123")
        self.org = Organization.objects.create(name="Checkout Org")
        Membership.objects.create(user=self.owner, organization=self.org, role=Membership.ROLE_OWNER)
        Membership.objects.create(user=self.viewer, organization=self.org, role=Membership.ROLE_VIEWER)

    def _payload(self, plan="starter"):
        return {
            "organization_id": str(self.org.id),
            "plan": plan,
            "success_url": "https://app.example.org/billing/success",
            "cancel_url": "https://app.example.org/billing/cancel",
        }

    def test_viewer_cannot_initiate_checkout(self):
        self.client.force_authenticate(user=self.viewer)
        response = self.client.post(reverse("billing-checkout-session"), self._payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_invalid_plan_rejected(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.post(reverse("billing-checkout-session"), self._payload(plan="ultra"), format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_missing_stripe_key_returns_503(self):
        self.client.force_authenticate(user=self.owner)
        with override_settings(STRIPE_SECRET_KEY=""):
            response = self.client.post(reverse("billing-checkout-session"), self._payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    @override_settings(STRIPE_SECRET_KEY="sk_test_123", STRIPE_PRICE_STARTER="price_starter_123")
    def test_owner_gets_checkout_url_on_success(self):
        self.client.force_authenticate(user=self.owner)
        with patch.object(stripe_service, "_client") as mock_client:
            mock_client.return_value.Customer.create.return_value = {"id": "cus_123"}
            mock_client.return_value.checkout.Session.create.return_value = {"url": "https://checkout.stripe.com/xyz"}
            response = self.client.post(reverse("billing-checkout-session"), self._payload(), format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["checkout_url"], "https://checkout.stripe.com/xyz")
        self.org.refresh_from_db()
        self.assertEqual(self.org.stripe_customer_id, "cus_123")


class BillingPortalViewTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="owner2@example.org", password="password-o-123")
        self.org = Organization.objects.create(name="Portal Org", stripe_customer_id="cus_existing")
        Membership.objects.create(user=self.owner, organization=self.org, role=Membership.ROLE_OWNER)
        self.client.force_authenticate(user=self.owner)

    @override_settings(STRIPE_SECRET_KEY="sk_test_123")
    def test_returns_portal_url(self):
        with patch.object(stripe_service, "_client") as mock_client:
            mock_client.return_value.billing_portal.Session.create.return_value = {"url": "https://billing.stripe.com/p/abc"}
            response = self.client.post(
                reverse("billing-portal-session"),
                {"organization_id": str(self.org.id), "return_url": "https://app.example.org/account"},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["portal_url"], "https://billing.stripe.com/p/abc")

    def test_org_without_stripe_customer_returns_502(self):
        org2 = Organization.objects.create(name="No Customer Org")
        Membership.objects.create(user=self.owner, organization=org2, role=Membership.ROLE_OWNER)
        with override_settings(STRIPE_SECRET_KEY="sk_test_123"):
            response = self.client.post(
                reverse("billing-portal-session"),
                {"organization_id": str(org2.id), "return_url": "https://app.example.org/account"},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)


class WebhookEventApplicationTests(TestCase):
    """Tests apply_webhook_event as a pure function — no network, no signature verification."""

    def setUp(self):
        self.org = Organization.objects.create(name="Webhook Org", plan=Organization.PLAN_FREE)

    def test_checkout_completed_upgrades_plan(self):
        event = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "metadata": {"organization_id": str(self.org.id), "plan": "starter"},
                    "subscription": "sub_123",
                }
            },
        }
        stripe_service.apply_webhook_event(event)
        self.org.refresh_from_db()
        self.assertEqual(self.org.plan, "starter")
        self.assertEqual(self.org.stripe_subscription_id, "sub_123")

    def test_subscription_deleted_downgrades_to_free(self):
        self.org.plan = Organization.PLAN_PRO
        self.org.stripe_subscription_id = "sub_456"
        self.org.save()

        event = {
            "type": "customer.subscription.deleted",
            "data": {"object": {"metadata": {"organization_id": str(self.org.id)}}},
        }
        stripe_service.apply_webhook_event(event)
        self.org.refresh_from_db()
        self.assertEqual(self.org.plan, Organization.PLAN_FREE)
        self.assertIsNone(self.org.stripe_subscription_id)

    def test_subscription_canceled_status_downgrades_to_free(self):
        self.org.plan = Organization.PLAN_PRO
        self.org.save()
        event = {
            "type": "customer.subscription.updated",
            "data": {"object": {"metadata": {"organization_id": str(self.org.id)}, "status": "canceled", "id": "sub_789"}},
        }
        stripe_service.apply_webhook_event(event)
        self.org.refresh_from_db()
        self.assertEqual(self.org.plan, Organization.PLAN_FREE)

    def test_unrelated_event_type_is_noop(self):
        event = {"type": "invoice.paid", "data": {"object": {}}}
        stripe_service.apply_webhook_event(event)  # should not raise
        self.org.refresh_from_db()
        self.assertEqual(self.org.plan, Organization.PLAN_FREE)


class WebhookEndpointTests(APITestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Endpoint Webhook Org")

    def test_missing_webhook_secret_returns_503(self):
        with override_settings(STRIPE_WEBHOOK_SECRET=""):
            response = self.client.post(
                reverse("billing-webhook"), data=b"{}", content_type="application/json",
                HTTP_STRIPE_SIGNATURE="t=1,v1=fake",
            )
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    @override_settings(STRIPE_WEBHOOK_SECRET="whsec_test")
    def test_invalid_signature_returns_400(self):
        response = self.client.post(
            reverse("billing-webhook"), data=b'{"type": "checkout.session.completed"}',
            content_type="application/json", HTTP_STRIPE_SIGNATURE="t=1,v1=invalid",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_settings(STRIPE_WEBHOOK_SECRET="whsec_test")
    def test_valid_event_applies_and_records_idempotency(self):
        fake_event = {
            "id": "evt_123",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "metadata": {"organization_id": str(self.org.id), "plan": "pro"},
                    "subscription": "sub_999",
                }
            },
        }
        with patch.object(stripe.Webhook, "construct_event", return_value=fake_event):
            response = self.client.post(
                reverse("billing-webhook"), data=b"{}", content_type="application/json",
                HTTP_STRIPE_SIGNATURE="t=1,v1=fake",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.org.refresh_from_db()
        self.assertEqual(self.org.plan, "pro")
        self.assertTrue(WebhookEvent.objects.filter(stripe_event_id="evt_123").exists())

    @override_settings(STRIPE_WEBHOOK_SECRET="whsec_test")
    def test_duplicate_event_is_noop_second_time(self):
        WebhookEvent.objects.create(stripe_event_id="evt_dupe", event_type="checkout.session.completed", payload={})
        fake_event = {
            "id": "evt_dupe",
            "type": "checkout.session.completed",
            "data": {"object": {"metadata": {"organization_id": str(self.org.id), "plan": "pro"}, "subscription": "x"}},
        }
        with patch.object(stripe.Webhook, "construct_event", return_value=fake_event):
            response = self.client.post(
                reverse("billing-webhook"), data=b"{}", content_type="application/json",
                HTTP_STRIPE_SIGNATURE="t=1,v1=fake",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.org.refresh_from_db()
        self.assertEqual(self.org.plan, Organization.PLAN_FREE)  # unchanged — event was already processed


class PlanGatingTests(APITestCase):
    """Verifies free-plan grant and AI-generation caps are actually enforced by the API."""

    def setUp(self):
        self.user = User.objects.create_user(email="capped@example.org", password="password-c-123")
        self.org = Organization.objects.create(name="Capped Org", plan=Organization.PLAN_FREE)
        Membership.objects.create(user=self.user, organization=self.org, role=Membership.ROLE_OWNER)
        self.client.force_authenticate(user=self.user)

    def test_free_plan_blocks_second_grant(self):
        Grant.objects.create(organization=self.org, title="First Grant")
        response = self.client.post(
            reverse("grant-list"), {"organization": str(self.org.id), "title": "Second Grant"}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_402_PAYMENT_REQUIRED)

    def test_free_plan_allows_first_grant(self):
        response = self.client.post(
            reverse("grant-list"), {"organization": str(self.org.id), "title": "Only Grant"}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_free_plan_blocks_ai_generation_over_limit(self):
        grant = Grant.objects.create(organization=self.org, title="AI Grant")
        Budget.objects.create(grant=grant)
        section = GrantSection.objects.create(grant=grant, section_type="methodology", title="Methodology", order=1)
        # Free plan allows 5/month — simulate 5 already used this month.
        for i in range(5):
            SectionVersion.objects.create(
                section=section, version_number=i + 1, content=f"v{i}", source=SectionVersion.SOURCE_AI,
            )
        response = self.client.post(reverse("grant-section-generate", args=[section.id]), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_402_PAYMENT_REQUIRED)
