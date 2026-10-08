"""Signed links to a job's Apply kit, for alerts (ntfy, email, digest).

Configured by ``KIT_BASE_URL`` (the API's URL) plus the kit secret (``KIT_SECRET``,
or the SSM parameter named by ``KIT_SECRET_PARAMETER``). Without both, there are no
kit links and every alert looks exactly as it did before the kit existed.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import quote

from jobmonitor.apply import tokens


@dataclass(frozen=True, slots=True)
class KitLinker:
    base_url: str
    secret: str

    def __call__(self, job_id: str) -> str:
        base = self.base_url.rstrip("/")
        return f"{base}/kit/{quote(job_id, safe='')}?t={tokens.sign(job_id, self.secret)}"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> KitLinker | None:
        e = os.environ if env is None else env
        base = e.get("KIT_BASE_URL")
        if not base:
            return None
        from jobmonitor.apply.kit_api import secret_from_env

        secret = secret_from_env(e)
        return cls(base, secret) if secret else None


__all__: Sequence[str] = ("KitLinker",)
