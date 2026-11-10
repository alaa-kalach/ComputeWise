"""The ONE shared authorization layer: every query is limited to the caller's subtree.

    Department admin -> everything under the departments they administer
    Professor        -> his own groups, their members, his credentials and budgets
    User             -> himself, and the groups he belongs to

Use it in every endpoint instead of writing your own permission checks:

    @router.get("/groups")
    def list_groups(scope: ScopeDep):
        return scope.session.exec(scope.groups()).all()

    @router.get("/groups/{group_id}")
    def get_group(group_id: uuid.UUID, scope: ScopeDep):
        return scope.require_group(group_id)          # 404 if outside the caller's scope

    @router.post("/groups/{group_id}/budget")
    def set_budget(group_id: uuid.UUID, scope: ScopeDep):
        group = scope.require_manage_group(group_id)  # only the owning professor

Rules of thumb:
- Reading something outside your scope returns 404, not 403, so ids of other
  professors' objects don't leak.
- The department observes and bills; it does not manage groups (403 on manage).
- The *_ids() methods return subqueries, so filters run in the database:
      select(X).where(X.group_id.in_(scope.group_ids()))
"""

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy import Select, false, or_, union
from sqlmodel import Session, select

from app.core.auth import CurrentUser, CurrentUserDep, Role
from app.core.db import get_session
from app.models import (
    AccessRequest,
    BudgetPolicy,
    CredentialGrant,
    DepartmentAdmin,
    GrantStatus,
    Group,
    GroupMember,
    IssuedResource,
    LedgerEntry,
    OwnerType,
    Professor,
    ProviderCredential,
    ScopeType,
    User,
)


def _not_found(what: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found")


class Scope:
    def __init__(self, me: CurrentUser, session: Session) -> None:
        self.me = me
        self.session = session

    @property
    def role(self) -> Role:
        return self.me.role

    # ------------------------------------------------------------ visible ids (subqueries)
    def professor_ids(self) -> Select:
        if self.role is Role.department_admin:
            return select(Professor.id).where(Professor.department_id.in_(self.me.department_ids))
        if self.role is Role.professor:
            return select(Professor.id).where(Professor.id == self.me.professor_id)
        # user: the professors of the groups he belongs to
        return (
            select(Group.professor_id)
            .join(GroupMember, GroupMember.group_id == Group.id)
            .where(GroupMember.user_id == self.me.id)
        )

    def group_ids(self) -> Select:
        if self.role is Role.user:
            return select(GroupMember.group_id).where(GroupMember.user_id == self.me.id)
        return select(Group.id).where(Group.professor_id.in_(self.professor_ids()))

    def user_ids(self) -> Select:
        if self.role is Role.user:
            return select(User.id).where(User.id == self.me.id)
        members = select(GroupMember.user_id).where(GroupMember.group_id.in_(self.group_ids()))
        professors = select(Professor.user_id).where(Professor.id.in_(self.professor_ids()))
        if self.role is Role.department_admin:
            admins = select(DepartmentAdmin.user_id).where(
                DepartmentAdmin.department_id.in_(self.me.department_ids)
            )
            return select(union(members, professors, admins).subquery().c[0])
        return select(union(members, professors).subquery().c[0])

    # ------------------------------------------------------------ scoped queries
    def groups(self) -> Select:
        return select(Group).where(Group.id.in_(self.group_ids())).order_by(Group.name)

    def users(self) -> Select:
        return select(User).where(User.id.in_(self.user_ids())).order_by(User.name)

    def professors(self) -> Select:
        return select(Professor).where(Professor.id.in_(self.professor_ids()))

    def group_members(self) -> Select:
        q = select(GroupMember).where(GroupMember.group_id.in_(self.group_ids()))
        if self.role is Role.user:
            q = q.where(GroupMember.user_id == self.me.id)  # not his teammates' rows
        return q

    def issued_resources(self) -> Select:
        if self.role is Role.user:
            return select(IssuedResource).where(IssuedResource.user_id == self.me.id)
        return select(IssuedResource).where(IssuedResource.group_id.in_(self.group_ids()))

    def access_requests(self) -> Select:
        if self.role is Role.user:
            return select(AccessRequest).where(AccessRequest.user_id == self.me.id)
        return select(AccessRequest).where(AccessRequest.group_id.in_(self.group_ids()))

    def budget_policies(self) -> Select:
        return select(BudgetPolicy).where(self._budget_filter(BudgetPolicy))

    def ledger_entries(self) -> Select:
        return select(LedgerEntry).where(self._budget_filter(LedgerEntry))

    def _budget_filter(self, model: type[BudgetPolicy] | type[LedgerEntry]):
        """Admins and professors: professor-level rows of visible professors, plus every
        row of their groups. Users: their group's team rows and their OWN user rows
        (not teammates' individual rows). Team balances that need teammates' spend are
        computed by the budget module after scope.require_group()."""
        if self.role is Role.user:
            return or_(
                (model.scope_type == ScopeType.group) & model.group_id.in_(self.group_ids()),
                (model.scope_type == ScopeType.user)
                & (model.scope_id == self.me.id)
                & model.group_id.in_(self.group_ids()),
            )
        return or_(
            (model.scope_type == ScopeType.professor) & model.scope_id.in_(self.professor_ids()),
            model.group_id.in_(self.group_ids()),
        )

    def credentials(self) -> Select:
        """Credential METADATA only. Never return secret_encrypted from an API."""
        if self.role is Role.user:
            return select(ProviderCredential).where(false())
        if self.role is Role.department_admin:
            return select(ProviderCredential).where(
                or_(
                    (ProviderCredential.owner_type == OwnerType.department)
                    & ProviderCredential.owner_id.in_(self.me.department_ids),
                    (ProviderCredential.owner_type == OwnerType.professor)
                    & ProviderCredential.owner_id.in_(self.professor_ids()),
                )
            )
        granted = select(CredentialGrant.credential_id).where(
            CredentialGrant.professor_id == self.me.professor_id, CredentialGrant.status == GrantStatus.active
        )
        return select(ProviderCredential).where(
            or_(
                (ProviderCredential.owner_type == OwnerType.professor)
                & (ProviderCredential.owner_id == self.me.professor_id),
                ProviderCredential.id.in_(granted),
            )
        )

    # ------------------------------------------------------------ single-object checks
    def can_see_group(self, group_id: uuid.UUID) -> bool:
        return self.session.exec(select(Group.id).where(Group.id == group_id, Group.id.in_(self.group_ids()))).first() is not None

    def require_group(self, group_id: uuid.UUID) -> Group:
        group = self.session.exec(self.groups().where(Group.id == group_id)).first()
        if group is None:
            raise _not_found("Group")
        return group

    def require_manage_group(self, group_id: uuid.UUID) -> Group:
        """Only the professor who owns the group may change it."""
        group = self.require_group(group_id)
        if self.role is not Role.professor or group.professor_id != self.me.professor_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the group's professor can manage it")
        return group

    def require_user(self, user_id: uuid.UUID) -> User:
        user = self.session.exec(self.users().where(User.id == user_id)).first()
        if user is None:
            raise _not_found("User")
        return user

    def require_professor(self, professor_id: uuid.UUID) -> Professor:
        prof = self.session.exec(self.professors().where(Professor.id == professor_id)).first()
        if prof is None:
            raise _not_found("Professor")
        return prof

    def require_issued_resource(self, resource_id: uuid.UUID) -> IssuedResource:
        res = self.session.exec(self.issued_resources().where(IssuedResource.id == resource_id)).first()
        if res is None:
            raise _not_found("Resource")
        return res

    def require_access_request(self, request_id: uuid.UUID) -> AccessRequest:
        req = self.session.exec(self.access_requests().where(AccessRequest.id == request_id)).first()
        if req is None:
            raise _not_found("Access request")
        return req

    def require_credential(self, credential_id: uuid.UUID) -> ProviderCredential:
        cred = self.session.exec(self.credentials().where(ProviderCredential.id == credential_id)).first()
        if cred is None:
            raise _not_found("Credential")
        return cred


def get_scope(me: CurrentUserDep, session: Annotated[Session, Depends(get_session)]) -> Scope:
    return Scope(me, session)


ScopeDep = Annotated[Scope, Depends(get_scope)]
