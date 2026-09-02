"""模块入口：python -m general_agent"""
from __future__ import annotations

import uvicorn

from general_agent.config import get_settings


def main() -> int:
    settings = get_settings()
    uvicorn.run(
        "general_agent.app:app",
        host=settings.server.host,
        port=settings.server.port,
        log_config=None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())