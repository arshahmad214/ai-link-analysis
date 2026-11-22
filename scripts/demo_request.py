"""Call the running API and save the JSON response under docs/ (evidence for the README).

python scripts/demo_request.py https://example.com
python scripts/demo_request.py https://example.com --base https://your-app.onrender.com --api-key XXXX
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import httpx


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--base", default="http://localhost:4000")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    headers = {"X-API-Key": args.api_key} if args.api_key else {}
    response = httpx.post(
        f"{args.base.rstrip('/')}/link-analysis",
        json={"url": args.url, "refresh": args.refresh},
        headers=headers,
        timeout=180,
    )
    body = response.json()
    print(f"HTTP {response.status_code}")
    print(json.dumps(body, indent=2, ensure_ascii=False))

    slug = re.sub(r"[^a-z0-9]+", "-", args.url.lower().split("://")[-1]).strip("-")[:60]
    out = Path(__file__).resolve().parent.parent / "docs" / f"sample_{slug}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved to {out}")


if __name__ == "__main__":
    main()
