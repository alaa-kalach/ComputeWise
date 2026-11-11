"""The ONE shared authorization layer: every query is limited to the caller's subtree.

    Department admin -> sees everything under the departments they administer (observe, bill)
    Professor        -> sees and MANAGES his own groups, their members, his credentials
    User             -> sees himself and the groups he belongs to

Roles can be combined (a department admin who is also a professor). Visibility is the
union of what each role sees. Managing a group (approving access, issuing, limiting or
revoking keys, stopping or starting its pods, setting budgets, suspending members) needs the
professor role AND ownership of the group: a department admin without the professor role
never manages or issues. Approved students launch (and stop) their OWN pods: that endpoint
checks the student's approved RunPod access, not require_manage_group.

Use it in every endpoint instead of writing your own permission checks:

    @router.get("/groups")
    def list_groups(scope: ScopeDep):
        return scope.session.exec(scope.groups()).all()

    @router.get("/groups/{group_id}")
    def get_group(group_id: uuid.UUID, scope: ScopeDep):
        return scope.require_group(group_id)          # 404 if outside the caller's scope

    @router.post("/groups/{group_id}/keys")
    def issue_key(group_id: uuid.UUID, scope: ScopeDep):
        group = scope.require_manage_group(group_id)  # only the group's own professor

Rules of thumb:
- Reading something outside your scope returns 404, not 403, so ids of other
  professors' objects don't leak.
- Visible but not yours to manage (e.g. the department looking at a group) returns 403.
- The *_ids() methods return subqueries, so filters run in the database:
      select(X).where(X.group_id.in_(scope.group_ids()))
"""

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy import Select, false, or_, union
from sqlmodel import Session, select

from app.core.auth import CurrentUser, CurrentUserDep
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


def _union_ids(*queries: Select | None) -> Select:
    """One subquery of ids from several role-specific subqueries."""
    parts = [q for q in queries if q is not None]
    if len(parts) == 1:
        return parts[0]
    return select(union(*parts).subquery().c[0])


class Scope:
    def __init__(self, me: CurrentUser, session: Session) -> None:
        self.me = me
        self.session = session

    # ------------------------------------------------------------ per-role building blocks
    def _admin_professor_ids(self) -> Select | None:
        if not self.me.is_admin:
            return None
        return select(Professor.id).where(Professor.department_id.in_(self.me.admin_department_ids))

    def _own_professor_ids(self) -> Select | None:
        if not self.me.is_professor:
            return None
        return select(Professor.id).where(Professor.id == self.me.professor_id)

    def _member_group_ids(self) -> Select:
        return select(GroupMember.group_id).where(GroupMember.user_id == self.me.id)

    def _staff_group_ids(self) -> Select | None:
        """Groups seen as department admin or as their professor (not as a member)."""
        profs = [q for q in (self._admin_professor_ids(), self._own_professor_ids()) if q is not None]
        if not profs:
            return None
        return select(Group.id).where(Group.professor_id.in_(_union_ids(*profs)))

    # ------------------------------------------------------------ visible ids (subqueries)
    def professor_ids(self) -> Select:
        member_profs = (
            select(Group.professor_id)
            .join(GroupMember, GroupMember.group_id == Group.id)
            .where(GroupMember.user_id == self.me.id)
        )
        return _union_ids(self._admin_professor_ids(), self._own_professor_ids(), member_profs)

    def group_ids(self) -> Select:
        return _union_ids(self._staff_group_ids(), self._member_group_ids())

    def user_ids(self) -> Select:
        parts: list[Select | None] = [select(User.id).where(User.id == self.me.id)]
        staff_groups = self._staff_group_ids()
        if staff_groups is not None:
            parts.append(select(GroupMember.user_id).where(GroupMember.group_id.in_(staff_groups)))
            staff_profs = _union_ids(*[q for q in (self._admin_professor_ids(), self._own_professor_ids()) if q is not None])
            parts.append(select(Professor.user_id).where(Professor.id.in_(staff_profs)))
        if self.me.is_admin:
            parts.append(
                select(DepartmentAdmin.user_id).where(DepartmentAdmin.department_id.in_(self.me.admin_department_ids))
            )
        return _union_ids(*parts)

    # ------------------------------------------------------------ scoped queries
    def groups(self) -> Select:
        return select(Group).where(Group.id.in_(self.group_ids())).order_by(Group.name)

    def users(self) -> Select:
        return select(User).where(User.id.in_(self.user_ids())).order_by(User.name)

    def professors(self) -> Select:
        return select(Professor).where(Professor.id.in_(self.professor_ids()))

    def group_members(self) -> Select:
        """Staff see all members of their groups; as a member you see only your own rows."""
        own = GroupMember.user_id == self.me.id
        staff = self._staff_group_ids()
        return select(GroupMember).where(or_(own, GroupMember.group_id.in_(staff)) if staff is not None else own)

    def issued_resources(self) -> Select:
        return select(IssuedResource).where(self._staff_or_own(IssuedResource))

    def access_requests(self) -> Select:
        return select(AccessRequest).where(self._staff_or_own(AccessRequest))

    def _staff_or_own(self, model: type[IssuedResource] | type[AccessRequest]):
        own = model.user_id == self.me.id
        staff = self._staff_group_ids()
        return or_(own, model.group_id.in_(staff)) if staff is not None else own

    def budget_policies(self) -> Select:
        return select(BudgetPolicy).where(self._budget_filter(BudgetPolicy))

    def ledger_entries(self) -> Select:
        return select(LedgerEntry).where(self._budget_filter(LedgerEntry))

    def _budget_filter(self, model: type[BudgetPolicy] | type[LedgerEntry]):
        """As a member: your team's group-level rows and your OWN user rows (not teammates'
        individual rows). As staff: professor-level rows of visible professors, plus every
        row of their groups. Team balances that need teammates' spend are computed by the
        budget module after scope.require_group()."""
        member_groups = self._member_group_ids()
        conditions = [
            (model.scope_type == ScopeType.group) & model.group_id.in_(member_groups),
            (model.scope_type == ScopeType.user) & (model.scope_id == self.me.id) & model.group_id.in_(member_groups),
        ]
        staff_groups = self._staff_group_ids()
        if staff_groups is not None:
            staff_profs = _union_ids(*[q for q in (self._admin_professor_ids(), self._own_professor_ids()) if q is not None])
            conditions.append((model.scope_type == ScopeType.professor) & model.scope_id.in_(staff_profs))
            conditions.append(model.group_id.in_(staff_groups))
        return or_(*conditions)

    def credentials(self) -> Select:
        """Credential METADATA only. Never return secret_encrypted from an API."""
        conditions = []
        if self.me.is_admin:
            admin_profs = self._admin_professor_ids()
            conditions.append(
                (ProviderCredential.owner_type == OwnerType.department)
                & ProviderCredential.owner_id.in_(self.me.admin_department_ids)
            )
            conditions.append(
                (ProviderCredential.owner_type == OwnerType.professor) & ProviderCredential.owner_id.in_(admin_profs)
            )
        if self.me.is_professor:
            granted = select(CredentialGrant.credential_id).where(
                CredentialGrant.professor_id == self.me.professor_id, CredentialGrant.status == GrantStatus.active
            )
            conditions.append(
                (ProviderCredential.owner_type == OwnerType.professor)
                & (ProviderCredential.owner_id == self.me.professor_id)
            )
            conditions.append(ProviderCredential.id.in_(granted))
        if not conditions:
            return select(ProviderCredential).where(false())
        return select(ProviderCredential).where(or_(*conditions))

    # ------------------------------------------------------------ single-object checks
    def can_see_group(self, group_id: uuid.UUID) -> bool:
        return (
            self.session.exec(select(Group.id).where(Group.id == group_id, Group.id.in_(self.group_ids()))).first()
            is not None
        )

    def require_group(self, group_id: uuid.UUID) -> Group:
        group = self.session.exec(self.groups().where(Group.id == group_id)).first()
        if group is None:
            raise _not_found("Group")
        return group

    def require_manage_group(self, group_id: uuid.UUID) -> Group:
        """Only the professor who owns the group may manage it: approve or reject access,
        issue, limit or revoke keys, stop or start any pod in the group, set its budgets,
        suspend members. (A student launching his own pod is NOT checked here.) A department admin can do this only if he also holds the
        professor role AND owns this group."""
        group = self.require_group(group_id)
        if not self.me.is_professor or group.professor_id != self.me.professor_id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Only the group's professor can approve access, issue keys, control pods, or change this group",
            )
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
