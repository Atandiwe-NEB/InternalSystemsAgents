"""WebSocket streaming demo — connects to /stream and prints progress events
as they arrive, then prints the final report markdown.

Usage:
    uv run python scripts/ws_demo.py
    uv run python scripts/ws_demo.py "Show contractor cost per project for Q1"

Requires the API to be running:
    make dev
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx


PROMPT = sys.argv[1] if len(sys.argv) > 1 else "Summarize last sprint delivery vs hours logged"
WS_URL = "ws://localhost:8000/stream"
HTTP_URL = "http://localhost:8000"


async def run_stream(prompt: str) -> None:
    # Check server is up first
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{HTTP_URL}/health", timeout=3.0)
            health = r.json()
            print(f"✓ Server online  mock_mode={health['mock_mode']}  model={health['model']}\n")
    except Exception as exc:
        print(f"✗ Cannot reach {HTTP_URL}/health — is the API running?  ({exc})")
        sys.exit(1)

    # websockets is optional — fall back to a plain HTTP /ask if not installed
    try:
        import websockets  # type: ignore
    except ImportError:
        print("websockets not installed — falling back to POST /ask\n")
        await run_http(prompt)
        return

    print(f"Prompt: {prompt!r}\n")
    print("─" * 60)

    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps({"prompt": prompt}))

        async for raw in ws:
            msg = json.loads(raw)
            kind = msg.get("type")

            if kind == "progress":
                print(f"  {msg['message']}")

            elif kind == "report":
                print("\n" + "═" * 60)
                print("REPORT")
                print("═" * 60)
                print(msg["markdown"])
                break

            elif kind == "result":
                print("\n" + "═" * 60)
                print("RESULT")
                print("═" * 60)
                print(msg["text"])
                break

            elif kind == "error":
                print(f"\n✗ Error: {msg['detail']}")
                break


async def run_http(prompt: str) -> None:
    """Fallback: POST /ask and pretty-print the response."""
    print(f"Prompt: {prompt!r}\n")
    print("─" * 60)

    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            f"{HTTP_URL}/ask",
            json={"prompt": prompt},
        )
        r.raise_for_status()
        data = r.json()

    if "markdown" in data:
        print(f"TL;DR: {data['tldr']}\n")
        print("─" * 60)
        print(data["markdown"])
    elif "question" in data:
        print(f"Clarification needed:\n  {data['question']}")
    else:
        print(json.dumps(data, indent=2))


if __name__ == "__main__":
    asyncio.run(run_stream(PROMPT))
