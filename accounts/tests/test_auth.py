from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Membership, Organization, User


class RegistrationTests(APITestCase):
    def test_register_creates_user_org_and_owner_membership(self):
        url = reverse("auth-register")
        payload = {
            "email": "founder@example.org",
            "password": "correct-horse-battery-9",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "organization_name": "Example Nonprofit",
        }
        response = self.client.post(url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIn("access", response.data["tokens"])
        self.assertIn("refresh", response.data["tokens"])

        user = User.objects.get(email="founder@example.org")
        org = Organization.objects.get(name="Example Nonprofit")
        membership = Membership.objects.get(user=user, organization=org)
        self.assertEqual(membership.role, Membership.ROLE_OWNER)

    def test_register_rejects_duplicate_email(self):
        User.objects.create_user(email="dupe@example.org", password="whatever-1234")
        url = reverse("auth-register")
        payload = {
            "email": "dupe@example.org",
            "password": "another-password-5678",
            "organization_name": "Another Org",
        }
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_register_rejects_weak_password(self):
        url = reverse("auth-register")
        payload = {
            "email": "weak@example.org",
            "password": "12345",
            "organization_name": "Weak Password Org",
        }
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("password", response.data)


class LoginTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="login@example.org", password="a-strong-password-1")

    def test_login_returns_jwt_pair(self):
        url = reverse("auth-login")
        response = self.client.post(url, {"email": "login@example.org", "password": "a-strong-password-1"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)

    def test_login_rejects_bad_password(self):
        url = reverse("auth-login")
        response = self.client.post(url, {"email": "login@example.org", "password": "wrong"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_me_requires_authentication(self):
        url = reverse("auth-me")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_me_returns_memberships(self):
        org = Organization.objects.create(name="Login Org")
        Membership.objects.create(user=self.user, organization=org, role=Membership.ROLE_OWNER)
        self.client.force_authenticate(user=self.user)
        response = self.client.get(reverse("auth-me"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["memberships"]), 1)


class OrganizationScopingTests(APITestCase):
    """An org's data must never be visible to a user outside that org."""

    def setUp(self):
        self.user_a = User.objects.create_user(email="a@example.org", password="password-a-123")
        self.user_b = User.objects.create_user(email="b@example.org", password="password-b-123")
        self.org_a = Organization.objects.create(name="Org A")
        self.org_b = Organization.objects.create(name="Org B")
        Membership.objects.create(user=self.user_a, organization=self.org_a, role=Membership.ROLE_OWNER)
        Membership.objects.create(user=self.user_b, organization=self.org_b, role=Membership.ROLE_OWNER)

    def test_user_only_sees_own_organizations(self):
        self.client.force_authenticate(user=self.user_a)
        response = self.client.get(reverse("organization-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [org["name"] for org in response.data["results"]]
        self.assertIn("Org A", names)
        self.assertNotIn("Org B", names)

    def test_user_cannot_fetch_another_orgs_detail(self):
        self.client.force_authenticate(user=self.user_a)
        response = self.client.get(reverse("organization-detail", args=[self.org_b.id]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
