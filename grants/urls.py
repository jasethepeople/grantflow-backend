from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    BudgetLineItemViewSet,
    BudgetViewSet,
    FunderProgramViewSet,
    FunderViewSet,
    GrantSectionViewSet,
    GrantViewSet,
    OutcomeReportViewSet,
)

router = DefaultRouter()
router.register("funders", FunderViewSet, basename="funder")
router.register("funder-programs", FunderProgramViewSet, basename="funder-program")
router.register("grants", GrantViewSet, basename="grant")
router.register("sections", GrantSectionViewSet, basename="grant-section")
router.register("budgets", BudgetViewSet, basename="budget")
router.register("budget-line-items", BudgetLineItemViewSet, basename="budget-line-item")
router.register("outcomes", OutcomeReportViewSet, basename="outcome-report")

urlpatterns = [
    path("", include(router.urls)),
]
