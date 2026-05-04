from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from shifter import db
from shifter.config import get_settings
from shifter.screenshots import cleanup_loop

log = logging.getLogger("shifter")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Register helpers used in templates so we don't have to thread them through every context.
from shifter.money import format_cents, format_cents_per_hour  # noqa: E402
from shifter.time_utils import format_date, format_dt, format_duration  # noqa: E402

# Stable, distinguishable palette. Picked for legibility on both light & dark
# Pico themes with white foreground text. Extend if you ever need more nannies.
_NANNY_PALETTE = [
    "#2563eb",  # blue
    "#7e22ce",  # purple
    "#16a34a",  # green
    "#ea580c",  # orange
    "#0891b2",  # cyan
    "#ca8a04",  # amber
    "#be123c",  # rose
    "#475569",  # slate
]


def nanny_color(nanny_id: int) -> str:
    return _NANNY_PALETTE[(nanny_id - 1) % len(_NANNY_PALETTE)]


templates.env.globals["format_cents"] = format_cents
templates.env.globals["format_cents_per_hour"] = format_cents_per_hour
templates.env.globals["format_dt"] = format_dt
templates.env.globals["format_date"] = format_date
templates.env.globals["format_duration"] = format_duration
templates.env.globals["nanny_color"] = nanny_color


def get_db(request: Request):
    """Per-request SQLite connection. Cheap to open thanks to persistent WAL."""
    conn = db.connect(request.app.state.settings.database_path)
    try:
        yield conn
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    settings.screenshot_dir.mkdir(parents=True, exist_ok=True)

    if settings.dev_mode:
        log.warning("DEV_MODE is ON — auth bypassed, API key not required. Do not use in production.")

    bootstrap_conn = db.connect(settings.database_path)
    applied = db.apply_migrations(bootstrap_conn)
    if applied:
        log.info("Applied migrations: %s", ", ".join(applied))
    bootstrap_conn.close()

    app.state.settings = settings

    cleanup_conn = db.connect(settings.database_path)
    cleanup_task = asyncio.create_task(cleanup_loop(cleanup_conn, settings))
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        cleanup_conn.close()


app = FastAPI(title="shifter", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/healthz", response_class=PlainTextResponse, include_in_schema=False)
def healthz() -> str:
    return "ok"


# Routers registered lazily so importing main doesn't pull in everything.
from shifter.routes import dashboard, nannies, schedule, shifts, reports, webhooks  # noqa: E402

app.include_router(dashboard.router)
app.include_router(nannies.router)
app.include_router(schedule.router)
app.include_router(shifts.router)
app.include_router(reports.router)
app.include_router(webhooks.router)
