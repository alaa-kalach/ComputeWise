"""All tables from the design document's data model.

Import every model here: Alembic only sees tables that have been imported.
This folder is shared: change it only through a PR the other two approve.
"""

from app.models.access import AccessRequest, IssuedResource, UsageSnapshot
from app.models.audit import AuditLog
from app.models.base import (
    CredentialStatus,
    GrantStatus,
    LedgerType,
    MemberStatus,
    OwnerType,
    Provider,
    RequestStatus,
    ResourceStatus,
    ScopeType,
)
from app.models.budget import BudgetPolicy, LedgerEntry
from app.models.credentials import CredentialGrant, ProfessorProviderBinding, ProviderCredential
from app.models.org import Department, DepartmentAdmin, Group, GroupMember, Professor, User

# Tables a trigger keeps append-only (see the initial migration).
APPEND_ONLY_TABLES = ("ledger_entries", "audit_log")

__all__ = [
    "APPEND_ONLY_TABLES",
    "AccessRequest",
    "AuditLog",
    "BudgetPolicy",
    "CredentialGrant",
    "CredentialStatus",
    "Department",
    "DepartmentAdmin",
    "GrantStatus",
    "Group",
    "GroupMember",
    "IssuedResource",
    "LedgerEntry",
    "LedgerType",
    "MemberStatus",
    "OwnerType",
    "Professor",
    "ProfessorProviderBinding",
    "Provider",
    "ProviderCredential",
    "RequestStatus",
    "ResourceStatus",
    "ScopeType",
    "UsageSnapshot",
    "User",
]
