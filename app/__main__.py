"""Точка входа: ``python -m app`` — слушает 0.0.0.0:$PORT."""

from __future__ import annotations

import uvicorn

from app.config import get_settings
from app.main import create_app


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=settings.port,
        log_config=None,
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_graceful_shutdown=settings.shutdown_grace_seconds + 5,
    )


if __name__ == "__main__":
    main()
