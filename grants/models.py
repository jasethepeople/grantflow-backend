import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class Funder(models.Model):
    """A grant-making entity: a federal agency, foundation, or corporation."""

    TYPE_FEDERAL = "federal"
    TYPE_FOUNDATION = "foundation"
    TYPE_CORPORATE = "corporate"
    TYPE_STATE_LOCAL = "state_local"
    TYPE_CHOICES = [
        (TYPE_FEDERAL, "Federal Agency"),
        (TYPE_FOUNDATION, "Private Foundation"),
        (TYPE_CORPORATE, "Corporate Giving"),
        (TYPE_STATE_LOCAL, "State / Local Government"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    funder_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    website = models.URLField(blank=True)
    focus_areas = models.CharField(
        max_length=500, blank=True,
        help_text="Comma-separated focus areas, e.g. 'education, youth development'.",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class FunderProgram(models.Model):
    """A specific funding program/opportunity run by a Funder — the 'funder intelligence' unit."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    funder = models.ForeignKey(Funder, on_delete=models.CASCADE, related_name="programs")
    name = models.CharField(max_length=255)
    typical_award_min = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    typical_award_max = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    next_deadline = models.DateField(null=True, blank=True)
    deadline_recurrence = models.CharField(
        max_length=100, blank=True,
        help_text="e.g. 'annual, rolling', 'quarterly'",
    )
    program_officer_name = models.CharField(max_length=255, blank=True)
    program_officer_email = models.EmailField(blank=True)
    program_officer_notes = models.TextField(
        blank=True, help_text="Preferences, past feedback, communication style, etc.",
    )
    past_awards_summary = models.TextField(
        blank=True, help_text="Notes on previously funded projects/organizations.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["next_deadline", "name"]

    def __str__(self):
        return f"{self.funder.name} — {self.name}"


class Grant(models.Model):
    """A single grant application/proposal being worked on by an Organization."""

    STATUS_DRAFTING = "drafting"
    STATUS_INTERNAL_REVIEW = "internal_review"
    STATUS_SUBMITTED = "submitted"
    STATUS_AWARDED = "awarded"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_DRAFTING, "Drafting"),
        (STATUS_INTERNAL_REVIEW, "Internal Review"),
        (STATUS_SUBMITTED, "Submitted"),
        (STATUS_AWARDED, "Awarded"),
        (STATUS_REJECTED, "Rejected"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE, related_name="grants",
    )
    funder_program = models.ForeignKey(
        FunderProgram, on_delete=models.SET_NULL, null=True, blank=True, related_name="grants",
    )
    title = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFTING)
    amount_requested = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    deadline = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="grants_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.title} ({self.organization.name})"


class GrantSection(models.Model):
    """A named section of a proposal (e.g. Statement of Need, Methodology)."""

    SECTION_TYPES = [
        ("statement_of_need", "Statement of Need"),
        ("goals_objectives", "Goals & Objectives"),
        ("methodology", "Methodology / Program Design"),
        ("evaluation_plan", "Evaluation Plan"),
        ("organizational_capacity", "Organizational Capacity"),
        ("sustainability", "Sustainability Plan"),
        ("budget_narrative", "Budget Narrative"),
        ("executive_summary", "Executive Summary"),
        ("other", "Other"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    grant = models.ForeignKey(Grant, on_delete=models.CASCADE, related_name="sections")
    section_type = models.CharField(max_length=40, choices=SECTION_TYPES)
    title = models.CharField(max_length=255)
    order = models.PositiveIntegerField(default=0)
    word_limit = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["grant", "order"]

    def __str__(self):
        return f"{self.grant.title} — {self.title}"

    @property
    def current_version(self):
        return self.versions.order_by("-version_number").first()


class SectionVersion(models.Model):
    """
    An immutable snapshot of a GrantSection's content. Editing a section creates
    a new version rather than mutating history — this is the version control.
    """

    SOURCE_HUMAN = "human"
    SOURCE_AI = "ai_generated"
    SOURCE_CHOICES = [(SOURCE_HUMAN, "Human edit"), (SOURCE_AI, "AI generated")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    section = models.ForeignKey(GrantSection, on_delete=models.CASCADE, related_name="versions")
    version_number = models.PositiveIntegerField()
    content = models.TextField(blank=True)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_HUMAN)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="section_versions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["section", "version_number"], name="unique_section_version_number",
            )
        ]

    def __str__(self):
        return f"{self.section.title} v{self.version_number}"

    @property
    def word_count(self):
        return len(self.content.split())


class Budget(models.Model):
    """One budget per Grant. Holds the indirect cost policy; line items hold the numbers."""

    BASE_TOTAL_DIRECT = "total_direct"
    BASE_MTDC = "mtdc"  # Modified Total Direct Costs (excludes equipment, subawards >$25k, etc.)
    BASE_SALARIES_WAGES = "salaries_wages"
    BASE_CHOICES = [
        (BASE_TOTAL_DIRECT, "Total Direct Costs"),
        (BASE_MTDC, "Modified Total Direct Costs (MTDC)"),
        (BASE_SALARIES_WAGES, "Salaries & Wages Only"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    grant = models.OneToOneField(Grant, on_delete=models.CASCADE, related_name="budget")
    indirect_cost_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("100.00"))],
        help_text="Indirect cost rate as a percentage, e.g. 15.00 for 15%.",
    )
    indirect_cost_base = models.CharField(max_length=20, choices=BASE_CHOICES, default=BASE_MTDC)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Budget for {self.grant.title}"

    def direct_line_items(self):
        return self.line_items.filter(is_indirect_base_exempt=False)

    def total_direct_costs(self) -> Decimal:
        total = self.line_items.aggregate(total=models.Sum("amount"))["total"]
        return total or Decimal("0.00")

    def indirect_cost_base_amount(self) -> Decimal:
        """The dollar amount the indirect rate is applied to, per the chosen base."""
        if self.indirect_cost_base == self.BASE_SALARIES_WAGES:
            total = self.line_items.filter(category=BudgetLineItem.CATEGORY_PERSONNEL).aggregate(
                total=models.Sum("amount")
            )["total"]
            return total or Decimal("0.00")
        if self.indirect_cost_base == self.BASE_MTDC:
            # MTDC excludes equipment, capital expenditures, and items flagged exempt.
            total = self.line_items.exclude(
                category=BudgetLineItem.CATEGORY_EQUIPMENT
            ).exclude(is_indirect_base_exempt=True).aggregate(total=models.Sum("amount"))["total"]
            return total or Decimal("0.00")
        return self.total_direct_costs()

    def indirect_costs(self) -> Decimal:
        base = self.indirect_cost_base_amount()
        rate = self.indirect_cost_rate / Decimal("100.00")
        return (base * rate).quantize(Decimal("0.01"))

    def total_budget(self) -> Decimal:
        return (self.total_direct_costs() + self.indirect_costs()).quantize(Decimal("0.01"))

    def summary(self) -> dict:
        return {
            "total_direct_costs": self.total_direct_costs(),
            "indirect_cost_base_amount": self.indirect_cost_base_amount(),
            "indirect_cost_rate": self.indirect_cost_rate,
            "indirect_costs": self.indirect_costs(),
            "total_budget": self.total_budget(),
        }


class BudgetLineItem(models.Model):
    CATEGORY_PERSONNEL = "personnel"
    CATEGORY_FRINGE = "fringe_benefits"
    CATEGORY_TRAVEL = "travel"
    CATEGORY_EQUIPMENT = "equipment"
    CATEGORY_SUPPLIES = "supplies"
    CATEGORY_CONTRACTUAL = "contractual"
    CATEGORY_OTHER = "other"
    CATEGORY_CHOICES = [
        (CATEGORY_PERSONNEL, "Personnel"),
        (CATEGORY_FRINGE, "Fringe Benefits"),
        (CATEGORY_TRAVEL, "Travel"),
        (CATEGORY_EQUIPMENT, "Equipment"),
        (CATEGORY_SUPPLIES, "Supplies"),
        (CATEGORY_CONTRACTUAL, "Contractual"),
        (CATEGORY_OTHER, "Other Direct Costs"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    budget = models.ForeignKey(Budget, on_delete=models.CASCADE, related_name="line_items")
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    description = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.00"))])
    is_indirect_base_exempt = models.BooleanField(
        default=False,
        help_text="Excluded from the indirect cost base calculation regardless of category (e.g. large subawards).",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["category", "description"]

    def __str__(self):
        return f"{self.description}: ${self.amount}"


class OutcomeReport(models.Model):
    """A tracked outcome, auto-populated into impact reports once a grant is awarded."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    grant = models.ForeignKey(Grant, on_delete=models.CASCADE, related_name="outcomes")
    metric_name = models.CharField(max_length=255, help_text="e.g. 'Youth served', 'Meals distributed'")
    target_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    actual_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    reporting_period_start = models.DateField(null=True, blank=True)
    reporting_period_end = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-reporting_period_end"]

    def __str__(self):
        return f"{self.metric_name} — {self.grant.title}"

    @property
    def percent_of_target(self):
        if not self.target_value:
            return None
        if self.actual_value is None:
            return None
        return round((self.actual_value / self.target_value) * 100, 1)
