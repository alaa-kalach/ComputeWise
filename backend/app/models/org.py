"""Organisation and roles: Department -> Professors -> Groups -> Users.

Roles are membership tables, not a column on users. A user is a department admin if
they appear in department_admins, a professor if they appear in professors, and a
regular user otherwise. Groups are how a professor organises users, not a fourth role.
"""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlmodel import Field, SQLModel

from app.models.base import MemberStatus, created_at_field, enum_type, id_field


class Department(SQLModel, table=True):
    __tablename__ = "departments"

    id: uuid.UUID = id_field()
    name: str = Field(max_length=200, unique=True)
    created_at: datetime = created_at_field()


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: uuid.UUID = id_field()
    name: str = Field(max_length=200)
    email: str = Field(max_length=320, unique=True)
    # Supabase auth.users.id. Nullable so seeded users exist before they first log in;
    # A2 links it on first login by email.
    auth_id: uuid.UUID | None = Field(default=None, unique=True)
    created_at: datetime = created_at_field()


class DepartmentAdmin(SQLModel, table=True):
    __tablename__ = "department_admins"

    department_id: uuid.UUID = Field(foreign_key="departments.id", primary_key=True, ondelete="CASCADE")
    user_id: uuid.UUID = Field(foreign_key="users.id", primary_key=True, ondelete="CASCADE")
    created_at: datetime = created_at_field()


class Professor(SQLModel, table=True):
    __tablename__ = "professors"

    id: uuid.UUID = id_field()
    department_id: uuid.UUID = Field(foreign_key="departments.id", index=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", unique=True)
    created_at: datetime = created_at_field()


class Group(SQLModel, table=True):
    __tablename__ = "groups"
    __table_args__ = (sa.UniqueConstraint("professor_id", "name"),)

    id: uuid.UUID = id_field()
    professor_id: uuid.UUID = Field(foreign_key="professors.id", index=True)
    name: str = Field(max_length=200)
    created_at: datetime = created_at_field()


class GroupMember(SQLModel, table=True):
    __tablename__ = "group_members"

    group_id: uuid.UUID = Field(foreign_key="groups.id", primary_key=True, ondelete="CASCADE")
    user_id: uuid.UUID = Field(foreign_key="users.id", primary_key=True, ondelete="CASCADE", index=True)
    # Added to the design: a professor suspends a user in HIS groups only, because the
    # same user may also belong to another professor's groups (A5).
    status: MemberStatus = Field(
        default=MemberStatus.active,
        sa_type=enum_type(MemberStatus),
        sa_column_kwargs={"server_default": MemberStatus.active.value},
    )
    created_at: datetime = created_at_field()
