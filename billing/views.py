from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Membership, Organization

from . import stripe_service
from .models import WebhookEvent
from .plans import PLAN_LIMITS


class CreateCheckoutSessionView(APIView):
    """
    POST /api/billing/checkout-session/
    Body: {"organization_id": "...", "plan": "starter", "success_url": "...", "cancel_url": "..."}
    Returns {"checkout_url": "..."} to redirect the user to Stripe Checkout.
    Only owners/admins of the organization may initiate a plan change.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        organization_id = request.data.get("organization_id")
        plan = request.data.get("plan")
        success_url = request.data.get("success_url")
        cancel_url = request.data.get("cancel_url")

        if not all([organization_id, plan, success_url, cancel_url]):
            return Response(
                {"detail": "organization_id, plan, success_url, and cancel_url are all required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if plan not in (Organization.PLAN_STARTER, Organization.PLAN_PRO):
            return Response({"detail": f"Invalid plan '{plan}'."}, status=status.HTTP_400_BAD_REQUEST)

        organization = Organization.objects.filter(id=organization_id).first()
        if organization is None:
            return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

        membership = Membership.objects.filter(user=request.user, organization=organization).first()
        if membership is None or not membership.can_manage:
            return Response(
                {"detail": "Only organization owners/admins can change billing plans."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            checkout_url = stripe_service.create_checkout_session(
                organization=organization,
                plan=plan,
                email=request.user.email,
                success_url=success_url,
                cancel_url=cancel_url,
            )
        except stripe_service.BillingConfigurationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except stripe_service.BillingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        return Response({"checkout_url": checkout_url}, status=status.HTTP_200_OK)


class CreateBillingPortalView(APIView):
    """POST /api/billing/portal-session/ -> {"portal_url": "..."} for managing/cancelling a subscription."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        organization_id = request.data.get("organization_id")
        return_url = request.data.get("return_url")
        if not organization_id or not return_url:
            return Response(
                {"detail": "organization_id and return_url are required."}, status=status.HTTP_400_BAD_REQUEST,
            )

        organization = Organization.objects.filter(id=organization_id).first()
        if organization is None:
            return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

        membership = Membership.objects.filter(user=request.user, organization=organization).first()
        if membership is None or not membership.can_manage:
            return Response(
                {"detail": "Only organization owners/admins can manage billing."}, status=status.HTTP_403_FORBIDDEN,
            )

        try:
            portal_url = stripe_service.create_billing_portal_session(organization, return_url)
        except stripe_service.BillingConfigurationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except stripe_service.BillingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        return Response({"portal_url": portal_url}, status=status.HTTP_200_OK)


class PlansView(APIView):
    """GET /api/billing/plans/ -> the plan tiers and their limits, for pricing-page rendering."""

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        return Response(PLAN_LIMITS)


@method_decorator(csrf_exempt, name="dispatch")
class StripeWebhookView(APIView):
    """
    POST /api/billing/webhook/ — Stripe calls this directly, no user auth (verified by signature instead).
    Idempotent: duplicate deliveries of the same event ID are no-ops.
    """

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")
        try:
            event = stripe_service.verify_webhook_signature(request.body, sig_header)
        except stripe_service.BillingConfigurationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except stripe_service.BillingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        event_id = event["id"]
        if WebhookEvent.objects.filter(stripe_event_id=event_id).exists():
            return Response({"detail": "Event already processed."}, status=status.HTTP_200_OK)

        stripe_service.apply_webhook_event(event)
        WebhookEvent.objects.create(
            stripe_event_id=event_id, event_type=event["type"], payload=dict(event),
        )
        return Response({"detail": "OK"}, status=status.HTTP_200_OK)
