"""Entry point: `python -m collab_hub` (container CMD) or the `collab-hub` script."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")  # noqa: S104 - container listens on all interfaces
    uvicorn.run(
        "collab_hub.app:create_app",
        factory=True,
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",  # ACA ingress terminates TLS in front of the app
        log_level="info",
        access_log=False,  # tool calls are logged by the app, without content
    )


if __name__ == "__main__":
    main()
