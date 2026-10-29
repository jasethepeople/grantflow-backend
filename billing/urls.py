from django.urls import path

from .views import (
    CreateBillingPortalView,
    CreateCheckoutSessionView,
    PlansView,
    StripeWebhookView,
)

urlpatterns = [
    path("billing/plans/", PlansView.as_view(), name="billing-plans"),
    path("billing/checkout-session/", CreateCheckoutSessionView.as_view(), name="billing-checkout-session"),
    path("billing/portal-session/", CreateBillingPortalView.as_view(), name="billing-portal-session"),
    path("billing/webhook/", StripeWebhookView.as_view(), name="billing-webhook"),
]
