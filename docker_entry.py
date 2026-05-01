"""Arranque no Docker/Render com logging Rich + uvicorn."""

from __future__ import annotations

import os

from maria_logging import UVICORN_RICH_LOG_CONFIG, quiet_noisy_loggers

import uvicorn

if __name__ == "__main__":
    quiet_noisy_loggers()
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(
        "maria_os:app",
        host="0.0.0.0",
        port=port,
        log_config=UVICORN_RICH_LOG_CONFIG,
        access_log=True,
    )
