"""Minimal API-key authorization with per-key company scopes.

``COMPANY_BRAIN_API_KEYS="key-a=northstar|acme,key-b=*,key-c=ws-*"`` grants key-a access
to two companies, key-b to all, and key-c to every company whose id starts with ``ws-``
(user workspaces, never the demo companies). When unset, authentication is disabled; this is meant
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
            for c in scope:
                if c != "*" and "*" in c[:-1]:
                    raise ValueError("'*' may only end a company prefix, e.g. 'ws-*'")
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
        if scope is None:
            return False
        return any(
            s == "*" or s == company_id or (s.endswith("*") and company_id.startswith(s[:-1]))
            for s in scope
        )
