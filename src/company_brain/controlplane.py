"""Read and write the control-plane database (Neon Postgres) over its SQL-over-HTTPS API.

The web app owns the control plane (workspaces, connections, Telegram links, alert
deliveries); the API only touches the Telegram and alert tables. Standard library only,
always with bound parameters.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any, Protocol

Row = dict[str, Any]


class ControlPlaneError(Exception):
    pass


class ControlPlane(Protocol):
    def query(self, sql: str, params: list[Any] | None = None) -> list[Row]: ...


class NeonHttp:
    def __init__(self, database_url: str, timeout_s: float = 15.0) -> None:
        host = urllib.parse.urlparse(database_url).hostname
        if not host:
            raise ControlPlaneError("DATABASE_URL has no host")
        self._endpoint = f"https://{host}/sql"
        self._url = database_url
        self._timeout = timeout_s

    def query(self, sql: str, params: list[Any] | None = None) -> list[Row]:
        request = urllib.request.Request(  # noqa: S310 - fixed https endpoint derived from config
            self._endpoint,
            data=json.dumps({"query": sql, "params": params or []}).encode(),
            headers={
                "Neon-Connection-String": self._url,
                "Content-Type": "application/json",
                "User-Agent": "node-and-nerve/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                body = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:200]
            raise ControlPlaneError(f"control plane query failed ({exc.code}): {detail}") from None
        except (OSError, ValueError) as exc:
            raise ControlPlaneError(f"control plane unreachable: {type(exc).__name__}") from None
        rows: list[Row] = body.get("rows") or []
        return rows


ControlPlaneFactory = Callable[[str], ControlPlane]
