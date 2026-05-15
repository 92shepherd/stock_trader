"""REST API for triggering collectors and managing the scheduler.

This package exposes the `stock_trader` collection pipeline as an HTTP
service. Co-located in the same process: an APScheduler instance that
runs the default daily 03:00 KST backfill so manual triggers and the
scheduled cron share state (per-collector asyncio locks) and never
collide.

Entry point:
    python -m src.api          # uvicorn ASGI server

Public modules:
    app         FastAPI application factory + lifespan
    runners     Wraps each collector with lock + JobRegistry bookkeeping
    locks       Per-collector asyncio + PostgreSQL advisory locks
    jobs        In-memory JobRegistry for async job tracking
    scheduler   APScheduler setup; registers default daily cron
    auth        X-API-Key header dependency
    schemas     Pydantic request/response models
    routers/    HTTP route definitions (health, jobs, schedule, collect)
"""
from __future__ import annotations
