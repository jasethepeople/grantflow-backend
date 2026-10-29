from decimal import Decimal

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Membership, Organization, User
from grants.models import Budget, BudgetLineItem, Grant, GrantSection, SectionVersion


class GrantOrgScopingTests(APITestCase):
    def setUp(self):
        self.user_a = User.objects.create_user(email="a@example.org", password="password-a-123")
        self.user_b = User.objects.create_user(email="b@example.org", password="password-b-123")
        self.org_a = Organization.objects.create(name="Org A")
        self.org_b = Organization.objects.create(name="Org B")
        Membership.objects.create(user=self.user_a, organization=self.org_a, role=Membership.ROLE_OWNER)
        Membership.objects.create(user=self.user_b, organization=self.org_b, role=Membership.ROLE_OWNER)
        self.grant_a = Grant.objects.create(organization=self.org_a, title="Grant A", created_by=self.user_a)
        Budget.objects.create(grant=self.grant_a)

    def test_owner_can_see_own_grant(self):
        self.client.force_authenticate(user=self.user_a)
        response = self.client.get(reverse("grant-detail", args=[self.grant_a.id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_other_org_user_cannot_see_grant(self):
        self.client.force_authenticate(user=self.user_b)
        response = self.client.get(reverse("grant-detail", args=[self.grant_a.id]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_viewer_cannot_edit_grant(self):
        Membership.objects.create(user=self.user_b, organization=self.org_a, role=Membership.ROLE_VIEWER)
        self.client.force_authenticate(user=self.user_b)
        response = self.client.patch(
            reverse("grant-detail", args=[self.grant_a.id]), {"title": "Hacked"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_creating_grant_auto_creates_budget(self):
        self.client.force_authenticate(user=self.user_a)
        # org_a already has one grant from setUp (free plan cap = 1), so use a fresh org.
        org_c = Organization.objects.create(name="Org C")
        Membership.objects.create(user=self.user_a, organization=org_c, role=Membership.ROLE_OWNER)
        response = self.client.post(
            reverse("grant-list"),
            {"organization": str(org_c.id), "title": "New Grant"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        grant = Grant.objects.get(id=response.data["id"])
        self.assertTrue(Budget.objects.filter(grant=grant).exists())


class SectionVersioningTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="writer@example.org", password="password-w-123")
        self.org = Organization.objects.create(name="Writer Org")
        Membership.objects.create(user=self.user, organization=self.org, role=Membership.ROLE_EDITOR)
        self.grant = Grant.objects.create(organization=self.org, title="Versioned Grant", created_by=self.user)
        Budget.objects.create(grant=self.grant)
        self.client.force_authenticate(user=self.user)

    def test_creating_section_seeds_version_one(self):
        response = self.client.post(
            reverse("grant-section-list"),
            {"grant": str(self.grant.id), "section_type": "statement_of_need", "title": "Statement of Need", "order": 1},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        section = GrantSection.objects.get(id=response.data["id"])
        self.assertEqual(section.versions.count(), 1)
        self.assertEqual(section.current_version.version_number, 1)

    def test_posting_new_version_increments_and_preserves_history(self):
        section = GrantSection.objects.create(grant=self.grant, section_type="methodology", title="Methodology", order=1)
        SectionVersion.objects.create(section=section, version_number=1, content="Draft one.", created_by=self.user)

        url = reverse("grant-section-versions", args=[section.id])
        response = self.client.post(url, {"content": "Draft two, revised.", "source": "human"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["version_number"], 2)
        self.assertEqual(section.versions.count(), 2)
        # original version untouched
        v1 = section.versions.get(version_number=1)
        self.assertEqual(v1.content, "Draft one.")

    def test_version_history_ordered_newest_first(self):
        section = GrantSection.objects.create(grant=self.grant, section_type="methodology", title="Methodology", order=1)
        SectionVersion.objects.create(section=section, version_number=1, content="v1", created_by=self.user)
        SectionVersion.objects.create(section=section, version_number=2, content="v2", created_by=self.user)

        url = reverse("grant-section-versions", args=[section.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]["version_number"], 2)


class BudgetCalculationTests(APITestCase):
    """Verifies the indirect-cost math itself, independent of the API layer."""

    def setUp(self):
        self.org = Organization.objects.create(name="Budget Org")
        self.grant = Grant.objects.create(organization=self.org, title="Budget Grant")

    def test_mtdc_excludes_equipment(self):
        budget = Budget.objects.create(
            grant=self.grant, indirect_cost_rate=Decimal("10.00"), indirect_cost_base=Budget.BASE_MTDC,
        )
        BudgetLineItem.objects.create(budget=budget, category=BudgetLineItem.CATEGORY_PERSONNEL, description="Staff", amount=Decimal("50000.00"))
        BudgetLineItem.objects.create(budget=budget, category=BudgetLineItem.CATEGORY_EQUIPMENT, description="Laptops", amount=Decimal("10000.00"))

        # MTDC base = 50000 (equipment excluded); indirect = 10% of 50000 = 5000
        self.assertEqual(budget.indirect_cost_base_amount(), Decimal("50000.00"))
        self.assertEqual(budget.indirect_costs(), Decimal("5000.00"))
        self.assertEqual(budget.total_direct_costs(), Decimal("60000.00"))
        self.assertEqual(budget.total_budget(), Decimal("65000.00"))

    def test_salaries_wages_base_only_counts_personnel(self):
        budget = Budget.objects.create(
            grant=self.grant, indirect_cost_rate=Decimal("20.00"), indirect_cost_base=Budget.BASE_SALARIES_WAGES,
        )
        BudgetLineItem.objects.create(budget=budget, category=BudgetLineItem.CATEGORY_PERSONNEL, description="Staff", amount=Decimal("40000.00"))
        BudgetLineItem.objects.create(budget=budget, category=BudgetLineItem.CATEGORY_TRAVEL, description="Conference", amount=Decimal("2000.00"))

        # base = 40000 (personnel only); indirect = 20% of 40000 = 8000
        self.assertEqual(budget.indirect_cost_base_amount(), Decimal("40000.00"))
        self.assertEqual(budget.indirect_costs(), Decimal("8000.00"))
        self.assertEqual(budget.total_direct_costs(), Decimal("42000.00"))
        self.assertEqual(budget.total_budget(), Decimal("50000.00"))

    def test_total_direct_costs_base_uses_everything(self):
        budget = Budget.objects.create(
            grant=self.grant, indirect_cost_rate=Decimal("15.00"), indirect_cost_base=Budget.BASE_TOTAL_DIRECT,
        )
        BudgetLineItem.objects.create(budget=budget, category=BudgetLineItem.CATEGORY_SUPPLIES, description="Supplies", amount=Decimal("1000.00"))
        BudgetLineItem.objects.create(budget=budget, category=BudgetLineItem.CATEGORY_EQUIPMENT, description="Server", amount=Decimal("4000.00"))

        self.assertEqual(budget.indirect_cost_base_amount(), Decimal("5000.00"))
        self.assertEqual(budget.indirect_costs(), Decimal("750.00"))
        self.assertEqual(budget.total_budget(), Decimal("5750.00"))

    def test_exempt_line_item_excluded_from_mtdc(self):
        budget = Budget.objects.create(
            grant=self.grant, indirect_cost_rate=Decimal("10.00"), indirect_cost_base=Budget.BASE_MTDC,
        )
        BudgetLineItem.objects.create(budget=budget, category=BudgetLineItem.CATEGORY_CONTRACTUAL, description="Small subaward", amount=Decimal("5000.00"))
        BudgetLineItem.objects.create(
            budget=budget, category=BudgetLineItem.CATEGORY_CONTRACTUAL, description="Large subaward",
            amount=Decimal("30000.00"), is_indirect_base_exempt=True,
        )

        self.assertEqual(budget.indirect_cost_base_amount(), Decimal("5000.00"))
        self.assertEqual(budget.indirect_costs(), Decimal("500.00"))
        self.assertEqual(budget.total_direct_costs(), Decimal("35000.00"))

    def test_zero_line_items_produces_zero_budget(self):
        budget = Budget.objects.create(grant=self.grant)
        self.assertEqual(budget.total_budget(), Decimal("0.00"))
