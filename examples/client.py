"""Minimal HTTP client for Company Brain using only the Python standard library.

Any language works the same way: POST JSON, read JSON. Run the API first:
    uv run uvicorn --factory company_brain.api:app
Then:
    python examples/client.py
"""

import json
import os
import urllib.request

BASE = os.environ.get("COMPANY_BRAIN_URL", "http://localhost:8000")
API_KEY = os.environ.get("COMPANY_BRAIN_API_KEY", "")


def investigate(company: str, question: str, as_of: str | None = None) -> dict:
    body = {"question": question, **({"as_of": as_of} if as_of else {})}
    request = urllib.request.Request(
        f"{BASE}/v1/companies/{company}/investigations",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": API_KEY},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


if __name__ == "__main__":
    result = investigate(
        "northstar", "Why is Project Atlas behind schedule and who owns the blockers?", "2026-10-16"
    )
    # An agent would hand these structured facts to its own reasoning step.
    for finding in result["findings"]:
        flag = "supported" if finding["supported"] else "UNSUPPORTED"
        print(f"[{flag}] {finding['statement']}  sources={finding['source_ids']}")
    print("missing:", result["missing_information"])
