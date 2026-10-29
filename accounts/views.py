from rest_framework import generics, permissions, status, viewsets
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from .models import Membership, Organization
from .permissions import IsOrgMember
from .serializers import (
    MembershipSerializer,
    OrganizationSerializer,
    RegisterSerializer,
    UserSerializer,
)


def tokens_for_user(user):
    refresh = RefreshToken.for_user(user)
    return {"refresh": str(refresh), "access": str(refresh.access_token)}


class RegisterView(generics.CreateAPIView):
    """POST /api/auth/register/ -> creates User + Organization + Membership, returns JWT pair."""

    permission_classes = [permissions.AllowAny]
    serializer_class = RegisterSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "register"

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        membership = user.memberships.select_related("organization").first()
        return Response(
            {
                "user": UserSerializer(user).data,
                "organization": OrganizationSerializer(membership.organization).data,
                "tokens": tokens_for_user(user),
            },
            status=status.HTTP_201_CREATED,
        )


class LoginView(TokenObtainPairView):
    """POST /api/auth/login/ -> {access, refresh}. Rate-limited to slow credential stuffing."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"


class MeView(APIView):
    """GET /api/auth/me/ -> the authenticated user plus their org memberships."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        memberships = request.user.memberships.select_related("organization")
        return Response(
            {
                "user": UserSerializer(request.user).data,
                "memberships": MembershipSerializer(memberships, many=True).data,
            }
        )


class OrganizationViewSet(viewsets.ModelViewSet):
    """
    CRUD for Organizations, scoped to organizations the requester belongs to.
    Only owners/admins may update or delete (enforced by IsOrgMember).
    """

    serializer_class = OrganizationSerializer
    permission_classes = [permissions.IsAuthenticated, IsOrgMember]

    def get_queryset(self):
        return Organization.objects.filter(memberships__user=self.request.user).distinct()

    def perform_create(self, serializer):
        org = serializer.save()
        Membership.objects.create(
            user=self.request.user, organization=org, role=Membership.ROLE_OWNER
        )


class MembershipViewSet(viewsets.ReadOnlyModelViewSet):
    """Lists memberships for organizations the requester belongs to."""

    serializer_class = MembershipSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Membership.objects.filter(
            organization__memberships__user=self.request.user
        ).select_related("user", "organization").distinct()
