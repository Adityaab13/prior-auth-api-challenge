"""Print signed JWT tokens for all seeded synthetic users."""

from __future__ import annotations

from app.auth import create_access_token
from app.domain.types import UserRole

SEEDED_IDENTITIES = [
    ("user-prov-maine", UserRole.PROVIDER_OFFICE, "prac-maine"),
    ("user-prov-hawaii", UserRole.PROVIDER_OFFICE, "prac-hawaii"),
    ("user-mem-alice", UserRole.MEMBER, None),
    ("user-mem-bob", UserRole.MEMBER, None),
    ("user-rev-nurse", UserRole.REVIEWER, None),
    ("user-rev-dr-smith", UserRole.REVIEWER, None),
    ("user-admin-1", UserRole.ADMIN, None),
]


def main() -> None:
    for user_id, role, practice_id in SEEDED_IDENTITIES:
        token = create_access_token(user_id=user_id, role=role, practice_id=practice_id)
        print(f"{user_id} ({role.value}):\n  {token}\n")


if __name__ == "__main__":
    main()
