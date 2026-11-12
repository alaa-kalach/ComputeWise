"""Send a Supabase invite email to someone a professor just added (A5).

Public sign-up is off (D10), so a new student has no Supabase account yet. The invite
creates one and emails a link to set a password; on first login, app.core.auth links it to
the users row by email.

Needs SUPABASE_SERVICE_ROLE_KEY (server-side only, never in the frontend). Without it, or
if Supabase can't be reached, nothing is sent and the caller reports invited=false; the
person can then be invited from the Supabase dashboard (Authentication > Users > Invite).
"""

import logging

import httpx

from app.core.config import get_settings

log = logging.getLogger(__name__)


def send_invite(email: str) -> bool:
    """True if Supabase accepted the invite. Never raises."""
    settings = get_settings()
    key = settings.supabase_service_role_key
    if not key or not settings.supabase_url:
        return False
    try:
        resp = httpx.post(
            f"{settings.supabase_url}/auth/v1/invite",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            json={"email": email},
            timeout=15,
        )
    except httpx.HTTPError as exc:
        log.warning("Supabase invite failed for a new member: %s", type(exc).__name__)
        return False
    if resp.status_code >= 400:
        # 422 "already registered" also lands here: the account exists, so login works anyway.
        log.warning("Supabase invite returned %s", resp.status_code)
        return False
    return True
