"""Minimal API-key authorization with per-key company scopes.

``COMPANY_BRAIN_API_KEYS="key-a=northstar|acme,key-b=*"`` grants key-a access to two
companies and key-b to all. When unset, authentication is disabled; this is meant
only for local development. This is not a replacement for real identity, rotation
or audit; see SECURITY.md.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass


@dataclass(frozen=True)
class ApiKeys:
    grants: tuple[tuple[str, frozenset[str]], ...]

    @property
    def enabled(self) -> bool:
        return bool(self.grants)

    @classmethod
    def parse(cls, raw: str) -> ApiKeys:
        grants: list[tuple[str, frozenset[str]]] = []
        for entry in filter(None, (part.strip() for part in raw.split(","))):
            key, sep, companies = entry.partition("=")
            if not sep or not key.strip() or not companies.strip():
                raise ValueError(
                    "COMPANY_BRAIN_API_KEYS entries must look like 'key=company|company'"
                )
            if len(key.strip()) < 16:
                raise ValueError("API keys must be at least 16 characters")
            scope = frozenset(c.strip() for c in companies.split("|") if c.strip())
            grants.append((key.strip(), scope))
        return cls(tuple(grants))

    def companies_for(self, presented: str | None) -> frozenset[str] | None:
        """Return the companies a key may access, or None if the key is invalid."""
        if presented is None:
            return None
        match: frozenset[str] | None = None
        for key, scope in self.grants:
            # compare every key to keep timing independent of which one matches
            if hmac.compare_digest(key.encode(), presented.encode()):
                match = scope
        return match

    def allows(self, presented: str | None, company_id: str) -> bool:
        scope = self.companies_for(presented)
        return scope is not None and ("*" in scope or company_id in scope)
