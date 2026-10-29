from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Membership, Organization, User
from grants import ai_service
from grants.models import Budget, Funder, FunderProgram, Grant, GrantSection, SectionVersion


class PromptBuildingTests(TestCase):
    """Verifies the prompt actually incorporates funder intelligence and org context."""

    def setUp(self):
        self.org = Organization.objects.create(
            name="River City Youth Services",
            mission_statement="We provide after-school mentoring to at-risk youth.",
        )
        self.funder = Funder.objects.create(
            name="Acme Foundation", funder_type=Funder.TYPE_FOUNDATION, focus_areas="youth, education",
        )
        self.program = FunderProgram.objects.create(
            funder=self.funder,
            name="Youth Opportunity Fund",
            program_officer_notes="Prefers proposals with clear evaluation metrics.",
        )
        self.grant = Grant.objects.create(
            organization=self.org, funder_program=self.program, title="Mentoring Expansion Grant",
        )
        self.section = GrantSection.objects.create(
            grant=self.grant, section_type="statement_of_need", title="Statement of Need", order=1, word_limit=500,
        )

    def test_prompt_includes_org_mission_and_funder_context(self):
        system_prompt, user_prompt = ai_service._build_prompt(self.section)
        self.assertIn("after-school mentoring", user_prompt)
        self.assertIn("Acme Foundation", user_prompt)
        self.assertIn("clear evaluation metrics", user_prompt)
        self.assertIn("500 words", user_prompt)

    def test_prompt_includes_other_existing_sections_for_consistency(self):
        other = GrantSection.objects.create(
            grant=self.grant, section_type="goals_objectives", title="Goals & Objectives", order=2,
        )
        SectionVersion.objects.create(
            section=other, version_number=1, content="Reduce chronic absenteeism by 20%.",
        )
        _, user_prompt = ai_service._build_prompt(self.section)
        self.assertIn("Reduce chronic absenteeism by 20%.", user_prompt)

    def test_system_prompt_forbids_fabrication(self):
        system_prompt, _ = ai_service._build_prompt(self.section)
        self.assertIn("Never fabricate", system_prompt)


class GenerateDraftServiceTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Org")
        self.grant = Grant.objects.create(organization=self.org, title="Grant")
        self.section = GrantSection.objects.create(
            grant=self.grant, section_type="methodology", title="Methodology", order=1,
        )

    @override_settings(ANTHROPIC_API_KEY="")
    def test_raises_configuration_error_without_api_key(self):
        with self.assertRaises(ai_service.AIConfigurationError):
            ai_service.generate_draft(self.section)

    @override_settings(ANTHROPIC_API_KEY="test-key-123")
    def test_returns_generated_text_on_success(self):
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = "This is the generated methodology section."
        mock_response = MagicMock()
        mock_response.content = [mock_block]

        with patch.object(ai_service.anthropic, "Anthropic") as MockClient:
            MockClient.return_value.messages.create.return_value = mock_response
            result = ai_service.generate_draft(self.section)

        self.assertEqual(result, "This is the generated methodology section.")

    @override_settings(ANTHROPIC_API_KEY="test-key-123")
    def test_wraps_api_errors(self):
        with patch.object(ai_service.anthropic, "Anthropic") as MockClient:
            MockClient.return_value.messages.create.side_effect = ai_service.anthropic.APIError(
                "boom", request=MagicMock(), body=None
            )
            with self.assertRaises(ai_service.AIGenerationError):
                ai_service.generate_draft(self.section)


class GenerateEndpointTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="writer@example.org", password="password-w-123")
        self.org = Organization.objects.create(name="Endpoint Org")
        Membership.objects.create(user=self.user, organization=self.org, role=Membership.ROLE_EDITOR)
        self.grant = Grant.objects.create(organization=self.org, title="Endpoint Grant")
        Budget.objects.create(grant=self.grant)
        self.section = GrantSection.objects.create(
            grant=self.grant, section_type="methodology", title="Methodology", order=1,
        )
        SectionVersion.objects.create(section=self.section, version_number=1, content="")
        self.client.force_authenticate(user=self.user)

    def test_generate_without_api_key_returns_503(self):
        url = reverse("grant-section-generate", args=[self.section.id])
        with override_settings(ANTHROPIC_API_KEY=""):
            response = self.client.post(url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_generate_success_creates_ai_sourced_version(self):
        url = reverse("grant-section-generate", args=[self.section.id])
        with patch.object(ai_service, "generate_draft", return_value="Generated draft text.") as mock_gen:
            response = self.client.post(url, {"instructions": "focus on outcomes"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["source"], "ai_generated")
        self.assertEqual(response.data["version_number"], 2)
        mock_gen.assert_called_once()
        self.assertEqual(mock_gen.call_args.kwargs.get("extra_instructions"), "focus on outcomes")

        self.section.refresh_from_db()
        self.assertEqual(self.section.versions.count(), 2)
        self.assertEqual(self.section.current_version.content, "Generated draft text.")

    def test_viewer_cannot_trigger_generation(self):
        viewer = User.objects.create_user(email="viewer@example.org", password="password-v-123")
        Membership.objects.create(user=viewer, organization=self.org, role=Membership.ROLE_VIEWER)
        self.client.force_authenticate(user=viewer)

        url = reverse("grant-section-generate", args=[self.section.id])
        with patch.object(ai_service, "generate_draft", return_value="Should not be called"):
            response = self.client.post(url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
