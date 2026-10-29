"""
Plan tiers and their feature limits. This is the single source of truth for
what free/starter/pro accounts are allowed to do — enforced in
grants/views.py (grant count) and grants/views.py's `generate` action
(AI generations per month).

`None` in a limit means unlimited.
"""

from accounts.models import Organization

PLAN_LIMITS = {
    Organization.PLAN_FREE: {
        "max_grants": 1,
        "ai_generations_per_month": 5,
        "max_seats": 2,
    },
    Organization.PLAN_STARTER: {
        "max_grants": 5,
        "ai_generations_per_month": 50,
        "max_seats": 5,
    },
    Organization.PLAN_PRO: {
        "max_grants": None,
        "ai_generations_per_month": 500,
        "max_seats": 20,
    },
}


def get_limits(plan: str) -> dict:
    return PLAN_LIMITS.get(plan, PLAN_LIMITS[Organization.PLAN_FREE])


class PlanLimitExceeded(Exception):
    """Raised when an organization has hit a plan limit. Caught in the view layer as HTTP 402."""
