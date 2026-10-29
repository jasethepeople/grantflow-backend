from django.db import transaction
from rest_framework import serializers

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


class FunderProgramSerializer(serializers.ModelSerializer):
    class Meta:
        model = FunderProgram
        fields = [
            "id", "funder", "name", "typical_award_min", "typical_award_max",
            "next_deadline", "deadline_recurrence", "program_officer_name",
            "program_officer_email", "program_officer_notes", "past_awards_summary",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class FunderSerializer(serializers.ModelSerializer):
    programs = FunderProgramSerializer(many=True, read_only=True)

    class Meta:
        model = Funder
        fields = ["id", "name", "funder_type", "website", "focus_areas", "notes", "programs", "created_at"]
        read_only_fields = ["id", "created_at"]


class SectionVersionSerializer(serializers.ModelSerializer):
    created_by_email = serializers.EmailField(source="created_by.email", read_only=True)
    word_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = SectionVersion
        fields = [
            "id", "section", "version_number", "content", "source",
            "created_by", "created_by_email", "word_count", "created_at",
        ]
        read_only_fields = ["id", "version_number", "created_by", "created_by_email", "word_count", "created_at"]


class SectionVersionCreateSerializer(serializers.Serializer):
    """Input for creating a new version of a section (i.e. saving an edit)."""

    content = serializers.CharField(allow_blank=True)
    source = serializers.ChoiceField(
        choices=SectionVersion.SOURCE_CHOICES, default=SectionVersion.SOURCE_HUMAN, required=False,
    )


class GrantSectionSerializer(serializers.ModelSerializer):
    current_version = SectionVersionSerializer(read_only=True)
    current_word_count = serializers.SerializerMethodField()

    class Meta:
        model = GrantSection
        fields = [
            "id", "grant", "section_type", "title", "order", "word_limit",
            "current_version", "current_word_count", "created_at",
        ]
        read_only_fields = ["id", "current_version", "current_word_count", "created_at"]

    def get_current_word_count(self, obj):
        version = obj.current_version
        return version.word_count if version else 0

    @transaction.atomic
    def create(self, validated_data):
        section = super().create(validated_data)
        # Every section starts with an empty v1 so `current_version` is never null.
        SectionVersion.objects.create(
            section=section,
            version_number=1,
            content="",
            source=SectionVersion.SOURCE_HUMAN,
            created_by=self.context["request"].user,
        )
        return section


class BudgetLineItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = BudgetLineItem
        fields = ["id", "budget", "category", "description", "amount", "is_indirect_base_exempt", "created_at"]
        read_only_fields = ["id", "created_at"]


class BudgetSerializer(serializers.ModelSerializer):
    line_items = BudgetLineItemSerializer(many=True, read_only=True)
    summary = serializers.SerializerMethodField()

    class Meta:
        model = Budget
        fields = [
            "id", "grant", "indirect_cost_rate", "indirect_cost_base", "notes",
            "line_items", "summary", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "line_items", "summary", "created_at", "updated_at"]

    def get_summary(self, obj):
        return obj.summary()


class OutcomeReportSerializer(serializers.ModelSerializer):
    percent_of_target = serializers.FloatField(read_only=True)

    class Meta:
        model = OutcomeReport
        fields = [
            "id", "grant", "metric_name", "target_value", "actual_value",
            "reporting_period_start", "reporting_period_end", "notes",
            "percent_of_target", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "percent_of_target", "created_at", "updated_at"]


class GrantListSerializer(serializers.ModelSerializer):
    funder_name = serializers.CharField(source="funder_program.funder.name", read_only=True, default=None)

    class Meta:
        model = Grant
        fields = [
            "id", "organization", "funder_program", "funder_name", "title",
            "status", "amount_requested", "deadline", "created_by", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_by", "created_at", "updated_at"]


class GrantDetailSerializer(GrantListSerializer):
    sections = GrantSectionSerializer(many=True, read_only=True)
    outcomes = OutcomeReportSerializer(many=True, read_only=True)
    budget = BudgetSerializer(read_only=True)

    class Meta(GrantListSerializer.Meta):
        fields = GrantListSerializer.Meta.fields + ["sections", "outcomes", "budget"]
