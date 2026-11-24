"""PostHog's users, organizations and memberships, as the reconciliation needs them."""

from __future__ import annotations

import structlog
from django.db import IntegrityError, transaction
from posthog.models import Organization, OrganizationMembership, User

from . import core

logger = structlog.get_logger(__name__)

_LEVELS = {
    core.MEMBER: OrganizationMembership.Level.MEMBER,
    core.ADMIN: OrganizationMembership.Level.ADMIN,
}
_NAMES = {
    OrganizationMembership.Level.MEMBER: core.MEMBER,
    OrganizationMembership.Level.ADMIN: core.ADMIN,
    OrganizationMembership.Level.OWNER: core.OWNER,
}


def reconcile(identity: core.Identity, config: core.Config) -> User | None:
    """Bring the account in line with the identity. None when it has no access to any organization."""
    try:
        return _reconcile(identity, config)
    except IntegrityError:
        # Two first requests of the same user ran side by side; the other one created the rows.
        return _reconcile(identity, config)


@transaction.atomic
def _reconcile(identity: core.Identity, config: core.Config) -> User | None:
    organizations = {str(o.id): o for o in Organization.objects.order_by("created_at")}
    desired, unknown = core.resolve(
        core.desired_levels(identity.groups, config),
        [(org_id, org.slug) for org_id, org in organizations.items()],
    )
    if unknown:
        logger.warning("auth_proxy_unknown_organizations", references=unknown)

    user = User.objects.select_for_update().filter(email__iexact=identity.email).first()
    memberships = {str(m.organization_id): m for m in OrganizationMembership.objects.filter(user=user)} if user else {}
    plan = core.plan({org_id: _NAMES[m.level] for org_id, m in memberships.items()}, desired)

    if user is None:
        if not plan.has_access:
            logger.info("auth_proxy_denied", reason="no_mapped_group")
            return None
        user = User.objects.create_user(
            email=identity.email,
            password=None,
            first_name=identity.name or identity.email.split("@", 1)[0],
            is_email_verified=True,
        )
        logger.info("auth_proxy_user_created", user_id=user.pk)

    for org_id in plan.remove:
        user.leave(organization=organizations[org_id])
    for org_id, level in plan.change.items():
        membership = memberships[org_id]
        membership.level = _LEVELS[level]
        membership.save(update_fields=["level", "updated_at"])
    for org_id, level in plan.add.items():
        user.join(organization=organizations[org_id], level=_LEVELS[level])
    if plan.add or plan.change or plan.remove:
        logger.info(
            "auth_proxy_memberships_changed",
            user_id=user.pk,
            added=sorted(plan.add),
            changed=sorted(plan.change),
            removed=list(plan.remove),
        )

    if user.is_active != plan.has_access:
        user.is_active = plan.has_access
        user.save(update_fields=["is_active"])
        logger.info("auth_proxy_user_active_changed", user_id=user.pk, is_active=plan.has_access)
    return user if plan.has_access else None
