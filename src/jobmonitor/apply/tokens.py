"""Signed Apply-kit links.

A kit page shows the user's phone number, email and address, so a link to it must
not be guessable. Each link carries an HMAC of the job id under a secret only the
deployment knows (``KIT_SECRET``, kept in SSM Parameter Store). A link for one job
does not open another, and a wrong token gets the same 404 as a missing job.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Sequence

#: Bytes of the HMAC kept in the link: 128 bits, 22 URL-safe characters.
TOKEN_BYTES = 16


def sign(job_id: str, secret: str) -> str:
    if not secret:
        raise ValueError("KIT_SECRET is empty")
    digest = hmac.new(secret.encode(), job_id.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:TOKEN_BYTES]).decode().rstrip("=")


def verify(job_id: str, token: str | None, secret: str | None) -> bool:
    """Constant-time check; any missing piece is simply ``False``."""
    if not token or not secret:
        return False
    return hmac.compare_digest(sign(job_id, secret), token)


__all__: Sequence[str] = ("TOKEN_BYTES", "sign", "verify")
