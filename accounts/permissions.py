from rest_framework import permissions

from .models import Membership


class IsOrgMember(permissions.BasePermission):
    """
    Allows read access to any member of the object's organization.
    Allows write access only to owners/admins of that organization.
    """

    def has_object_permission(self, request, view, obj):
        membership = Membership.objects.filter(user=request.user, organization=obj).first()
        if membership is None:
            return False
        if request.method in permissions.SAFE_METHODS:
            return True
        return membership.can_manage


def resolve_organization(obj):
    """
    Walks common relation chains to find the Organization that owns obj.
    Handles Grant (.organization), GrantSection/OutcomeReport/Budget (.grant.organization),
    and BudgetLineItem (.budget.grant.organization).
    """
    if hasattr(obj, "organization"):
        return obj.organization
    if hasattr(obj, "grant"):
        return obj.grant.organization
    if hasattr(obj, "budget"):
        return obj.budget.grant.organization
    raise AttributeError(f"Cannot resolve organization for {obj!r}")


class IsGrantOrgMember(permissions.BasePermission):
    """
    Allows read access to any member of the owning Grant's organization.
    Allows write access to editors, admins, and owners (not viewers).
    """

    def has_object_permission(self, request, view, obj):
        organization = resolve_organization(obj)
        membership = Membership.objects.filter(user=request.user, organization=organization).first()
        if membership is None:
            return False
        if request.method in permissions.SAFE_METHODS:
            return True
        return membership.can_edit
