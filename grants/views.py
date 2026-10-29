from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

from accounts.permissions import IsGrantOrgMember
from accounts.models import Membership, Organization

from billing.plans import get_limits
from realtime import steps_store

from . import ai_service
from .models import (
    Budget,
    BudgetLineItem,
    Funder,
    FunderProgram,
    Grant,
    GrantSection,
    OutcomeReport,
    SectionVersion,
)
from .serializers import (
    BudgetLineItemSerializer,
    BudgetSerializer,
    FunderProgramSerializer,
    FunderSerializer,
    GrantDetailSerializer,
    GrantListSerializer,
    GrantSectionSerializer,
    OutcomeReportSerializer,
    SectionVersionCreateSerializer,
    SectionVersionSerializer,
)


class AIGenerateThrottle(UserRateThrottle):
    scope = "ai_generate"


class FunderViewSet(viewsets.ModelViewSet):
    """
    The funder intelligence database. Shared reference data — readable by any
    authenticated user, writable by any authenticated user (crowdsourced/curated
    by staff running grant campaigns). Tighten to IsAdminUser if you want this
    locked down to internal curators only.
    """

    serializer_class = FunderSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = Funder.objects.all().prefetch_related("programs")
        funder_type = self.request.query_params.get("funder_type")
        if funder_type:
            qs = qs.filter(funder_type=funder_type)
        search = self.request.query_params.get("search")
        if search:
            qs = qs.filter(name__icontains=search)
        return qs


class FunderProgramViewSet(viewsets.ModelViewSet):
    serializer_class = FunderProgramSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = FunderProgram.objects.select_related("funder").all()
        funder_id = self.request.query_params.get("funder")
        if funder_id:
            qs = qs.filter(funder_id=funder_id)
        return qs


class GrantViewSet(viewsets.ModelViewSet):
    """CRUD for Grants, scoped strictly to the requester's organizations."""

    permission_classes = [permissions.IsAuthenticated, IsGrantOrgMember]

    def get_queryset(self):
        org_ids = Membership.objects.filter(user=self.request.user).values_list("organization_id", flat=True)
        return (
            Grant.objects.filter(organization_id__in=org_ids)
            .select_related("organization", "funder_program__funder")
            .prefetch_related("sections__versions", "outcomes", "budget__line_items")
        )

    def get_serializer_class(self):
        if self.action in ("retrieve", "create", "update", "partial_update"):
            return GrantDetailSerializer
        return GrantListSerializer

    def create(self, request, *args, **kwargs):
        organization_id = request.data.get("organization")
        organization = Organization.objects.filter(id=organization_id).first() if organization_id else None
        if organization is not None:
            limits = get_limits(organization.plan)
            max_grants = limits["max_grants"]
            if max_grants is not None and Grant.objects.filter(organization=organization).count() >= max_grants:
                return Response(
                    {
                        "detail": (
                            f"Your organization's '{organization.plan}' plan allows up to "
                            f"{max_grants} grant(s). Upgrade your plan to create more."
                        )
                    },
                    status=status.HTTP_402_PAYMENT_REQUIRED,
                )
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        organization = serializer.validated_data["organization"]
        membership = Membership.objects.filter(user=self.request.user, organization=organization).first()
        if membership is None or not membership.can_edit:
            raise permissions.PermissionDenied("You are not a member of this organization.")
        grant = serializer.save(created_by=self.request.user)
        Budget.objects.create(grant=grant)


class GrantSectionViewSet(viewsets.ModelViewSet):
    """CRUD for a Grant's sections, plus a `versions` action for version control."""

    serializer_class = GrantSectionSerializer
    permission_classes = [permissions.IsAuthenticated, IsGrantOrgMember]

    def get_queryset(self):
        org_ids = Membership.objects.filter(user=self.request.user).values_list("organization_id", flat=True)
        qs = GrantSection.objects.filter(grant__organization_id__in=org_ids).select_related("grant")
        grant_id = self.request.query_params.get("grant")
        if grant_id:
            qs = qs.filter(grant_id=grant_id)
        return qs

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["request"] = self.request
        return context

    @action(detail=True, methods=["get", "post"], url_path="versions")
    def versions(self, request, pk=None):
        """
        GET  -> full version history for this section, newest first.
        POST -> save a new version (an edit). Body: {"content": "...", "source": "human"}.
        """
        section = self.get_object()

        if request.method == "GET":
            qs = section.versions.all()
            return Response(SectionVersionSerializer(qs, many=True).data)

        serializer = SectionVersionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            next_number = (
                section.versions.aggregate(m=Max("version_number"))["m"] or 0
            ) + 1
            version = SectionVersion.objects.create(
                section=section,
                version_number=next_number,
                content=serializer.validated_data["content"],
                source=serializer.validated_data.get("source", SectionVersion.SOURCE_HUMAN),
                created_by=request.user,
            )
        steps_store.reset_sync(section.id)
        return Response(SectionVersionSerializer(version).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="generate", throttle_classes=[AIGenerateThrottle])
    def generate(self, request, pk=None):
        """
        POST /api/sections/{id}/generate/
        Body (optional): {"instructions": "emphasize our after-school program"}

        Calls the AI drafting engine using the grant's funder intelligence and
        the organization's mission as context, and saves the result as a new,
        AI-sourced SectionVersion (the existing version history is untouched).
        """
        section = self.get_object()
        instructions = request.data.get("instructions", "")

        organization = section.grant.organization
        limits = get_limits(organization.plan)
        monthly_limit = limits["ai_generations_per_month"]
        if monthly_limit is not None:
            month_start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            used_this_month = SectionVersion.objects.filter(
                section__grant__organization=organization,
                source=SectionVersion.SOURCE_AI,
                created_at__gte=month_start,
            ).count()
            if used_this_month >= monthly_limit:
                return Response(
                    {
                        "detail": (
                            f"Your organization's '{organization.plan}' plan allows "
                            f"{monthly_limit} AI generations per month, and you've used "
                            f"{used_this_month}. Upgrade your plan for more."
                        )
                    },
                    status=status.HTTP_402_PAYMENT_REQUIRED,
                )

        try:
            content = ai_service.generate_draft(section, extra_instructions=instructions)
        except ai_service.AIConfigurationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except ai_service.AIGenerationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        with transaction.atomic():
            next_number = (
                section.versions.aggregate(m=Max("version_number"))["m"] or 0
            ) + 1
            version = SectionVersion.objects.create(
                section=section,
                version_number=next_number,
                content=content,
                source=SectionVersion.SOURCE_AI,
                created_by=request.user,
            )
        steps_store.reset_sync(section.id)
        return Response(SectionVersionSerializer(version).data, status=status.HTTP_201_CREATED)


class BudgetViewSet(viewsets.ModelViewSet):
    """One Budget per Grant. Also exposes a `summary` action for the computed totals."""

    serializer_class = BudgetSerializer
    permission_classes = [permissions.IsAuthenticated, IsGrantOrgMember]

    def get_queryset(self):
        org_ids = Membership.objects.filter(user=self.request.user).values_list("organization_id", flat=True)
        qs = Budget.objects.filter(grant__organization_id__in=org_ids).select_related("grant").prefetch_related(
            "line_items"
        )
        grant_id = self.request.query_params.get("grant")
        if grant_id:
            qs = qs.filter(grant_id=grant_id)
        return qs

    @action(detail=True, methods=["get"])
    def summary(self, request, pk=None):
        budget = self.get_object()
        return Response(budget.summary())


class BudgetLineItemViewSet(viewsets.ModelViewSet):
    serializer_class = BudgetLineItemSerializer
    permission_classes = [permissions.IsAuthenticated, IsGrantOrgMember]

    def get_queryset(self):
        org_ids = Membership.objects.filter(user=self.request.user).values_list("organization_id", flat=True)
        qs = BudgetLineItem.objects.filter(budget__grant__organization_id__in=org_ids).select_related(
            "budget__grant"
        )
        budget_id = self.request.query_params.get("budget")
        if budget_id:
            qs = qs.filter(budget_id=budget_id)
        return qs


class OutcomeReportViewSet(viewsets.ModelViewSet):
    serializer_class = OutcomeReportSerializer
    permission_classes = [permissions.IsAuthenticated, IsGrantOrgMember]

    def get_queryset(self):
        org_ids = Membership.objects.filter(user=self.request.user).values_list("organization_id", flat=True)
        qs = OutcomeReport.objects.filter(grant__organization_id__in=org_ids).select_related("grant")
        grant_id = self.request.query_params.get("grant")
        if grant_id:
            qs = qs.filter(grant_id=grant_id)
        return qs
