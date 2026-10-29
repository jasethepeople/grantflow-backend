import uuid

from django.db import models


class WebhookEvent(models.Model):
    """
    Records every processed Stripe webhook event ID so a retried/duplicate
    delivery (Stripe does this routinely) is a no-op instead of double-applying
    a plan change or double-crediting anything.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stripe_event_id = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=100)
    payload = models.JSONField()
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return f"{self.event_type} ({self.stripe_event_id})"
