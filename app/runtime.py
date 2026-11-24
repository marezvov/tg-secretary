"""Shared runtime objects.

A single semaphore serialises every heavy operation (LLM/VLM, Playwright,
Whisper).  This keeps RAM/VRAM usage flat on small home servers and prevents
swap storms while a local model is generating.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

START_TIME: float = time.time()
HEAVY_SEMAPHORE = asyncio.Semaphore(1)

# Filled in by app.main / handlers so background jobs can reach them without
# creating circular imports.
bot: Any = None
scheduler: Any = None
database: Any = None
