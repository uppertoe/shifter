from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from shifter import ha, repos, screenshots
from shifter.auth import current_user, require_api_key
from shifter.config import Settings, get_settings
from shifter.main import get_db, templates


router = APIRouter(prefix="/api")


class EventIn(BaseModel):
    occurred_at: datetime          # ISO8601 with offset preferred
    source: str | None = None
    event_type: str | None = None  # 'arrival' | 'departure' | None


@router.post("/events", dependencies=[Depends(require_api_key)])
def post_event(
    payload: EventIn,
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if payload.event_type not in (None, "arrival", "departure"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "event_type must be 'arrival', 'departure', or absent")

    occurred = payload.occurred_at
    # Default to configured TZ if HA didn't include one (tolerate but not encouraged).
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=settings.zoneinfo)

    result = ha.process_event(
        conn,
        occurred_at=occurred,
        source=payload.source,
        event_type_hint=payload.event_type,
        settings=settings,
    )
    return JSONResponse({
        "event_id": result.event_id,
        "resolution": result.resolution,
        "nanny_id": result.nanny_id,
        "shift_id": result.shift_id,
        "note": result.note,
    })


@router.post("/events/{event_id}/screenshot", dependencies=[Depends(require_api_key)])
async def upload_screenshot(
    event_id: int,
    file: Annotated[UploadFile, File(...)],
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    event = conn.execute(
        "SELECT * FROM ha_events WHERE id = ?", (event_id,)
    ).fetchone()
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such event")

    if file.content_type not in screenshots.ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"content type {file.content_type!r} not supported",
        )
    body = await file.read()
    if not body:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty body")

    occurred = datetime.fromisoformat(event["occurred_at"])
    rel_path, size = screenshots.store_screenshot(
        settings=settings,
        ha_event_id=event_id,
        content=body,
        content_type=file.content_type,
        taken_at=occurred,
    )
    cur = conn.execute(
        "INSERT INTO screenshots (ha_event_id, filename, content_type, size_bytes)"
        " VALUES (?, ?, ?, ?)",
        (event_id, rel_path, file.content_type, size),
    )
    return JSONResponse({"screenshot_id": cur.lastrowid, "filename": rel_path, "size_bytes": size})


# --- manual attribution UI (auth: regular user) -----------------------------

@router.get("/events/unresolved", response_class=HTMLResponse)
def list_unresolved(
    request: Request,
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    rows = conn.execute(
        "SELECT * FROM ha_events WHERE resolution = 'unresolved' ORDER BY occurred_at DESC"
    ).fetchall()
    nannies = repos.list_nannies(conn, include_inactive=False)
    return templates.TemplateResponse(
        request,
        "ha/unresolved.html",
        {"user": user, "events": rows, "nannies": nannies, "tz": settings.zoneinfo},
    )


@router.post("/events/{event_id}/attribute")
def attribute(
    event_id: int,
    nanny_id: int = Form(...),
    direction: str = Form(...),
    conn=Depends(get_db),
    user: str = Depends(current_user),
):
    try:
        ha.attribute_unresolved(conn, event_id, nanny_id=nanny_id,
                                  direction=direction, user=user)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return RedirectResponse("/api/events/unresolved", status_code=status.HTTP_303_SEE_OTHER)
