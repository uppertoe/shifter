# Home Assistant integration

Shifter accepts raw presence events from Home Assistant and figures out which
nanny they belong to, using your schedule + current open-shift state. HA does
not need to identify the person — any door sensor, Frigate person-detect, or
mmWave trigger can be wired straight in.

- [Endpoints](#endpoints)
- [Auth](#auth)
- [POST /api/events](#post-apievents)
- [POST /api/events/{event_id}/screenshot](#post-apieventsevent_idscreenshot)
- [GET /api/shift/current](#get-apishiftcurrent)
- [Resolution policy](#resolution-policy)
- [Debounce](#debounce)
- [Manual attribution UI](#manual-attribution-ui)
- [Worked HA configuration](#worked-ha-configuration)
- [Quick test from the CLI](#quick-test-from-the-cli)

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/api/events` | `X-API-Key` | Submit a raw presence event. |
| `POST` | `/api/events/{event_id}/screenshot` | `X-API-Key` | Attach an image to a previously-submitted event. |
| `GET`  | `/api/shift/current` | `X-API-Key` | Read-only snapshot for HA polling: open shift + unresolved-event count + last event. |
| `GET`  | `/api/events/unresolved` | session (Authelia) | HTML list of events the resolver couldn't attribute. |
| `POST` | `/api/events/{event_id}/attribute` | session (Authelia) | Manually attribute an unresolved event. |

The bottom two are session-authenticated UI for *humans*. HA only needs the top
two — do not call them with the API key.

## Auth

Both API endpoints require the shared secret in the `X-API-Key` header:

```
X-API-Key: <value of API_KEY in shifter's .env>
```

Failures:

| Code | When |
|---|---|
| `401 Unauthorized` | header missing or wrong value |

The check is a constant-time comparison. In `DEV_MODE=true` the check is
bypassed entirely — never run dev mode in production.

## `POST /api/events`

### Request body (JSON)

| Field | Type | Required | Notes |
|---|---|---|---|
| `occurred_at` | ISO 8601 datetime | yes | Should include a timezone offset. If naive, shifter assumes the configured `TZ` (default `Australia/Melbourne`). |
| `source` | string | no | Free-form identifier of the trigger (e.g. `"frigate-front-door"`). Used as the debounce key — two events from the same source within `HA_DEBOUNCE_MINUTES` collapse to one. Omit at your own risk. |
| `event_type` | `"arrival"` \| `"departure"` \| omitted | no | Strong hint when HA can tell direction (e.g. door + presence direction). Without it, shifter infers from open-shift state. Any other value → `400`. |

### Response (200, JSON)

```json
{
  "event_id": 42,
  "resolution": "arrival",
  "nanny_id": 1,
  "shift_id": 17,
  "note": null
}
```

| Field | Notes |
|---|---|
| `event_id` | Always present. Use this to attach a screenshot afterwards. |
| `resolution` | One of `arrival`, `departure`, `unresolved`, `ignored`. |
| `nanny_id` / `shift_id` | Populated for `arrival` and `departure`; `null` otherwise. |
| `note` | Human-readable explanation when `resolution` is `unresolved` or `ignored`. |

### Errors

| Code | When |
|---|---|
| `400` | `event_type` is not one of `arrival`, `departure`, or absent. |
| `401` | API key missing/wrong. |
| `422` | JSON missing `occurred_at` or it doesn't parse as ISO 8601. |

### What "resolution" means for the caller

- `arrival` / `departure` — shifter opened or closed a shift. You can stop here.
- `unresolved` — the event was recorded but no shift was touched. It will appear
  in the unresolved queue for human attribution. Worth attaching a screenshot.
- `ignored` — recorded but actively suppressed (debounced or background noise).
  Don't bother with a screenshot.

## `POST /api/events/{event_id}/screenshot`

Multipart upload, attached to a previously created event. Optional — only call
if you have an image worth keeping.

### Request

- Content-Type: `multipart/form-data`
- Field name: `file`
- Allowed image types: `image/jpeg`, `image/png`, `image/webp`
- No size limit enforced by shifter (your reverse proxy probably has one).

### Response (200, JSON)

```json
{
  "screenshot_id": 11,
  "filename": "2026/05/event-42-153012.jpg",
  "size_bytes": 84512
}
```

### Errors

| Code | When |
|---|---|
| `400` | empty body |
| `401` | API key missing/wrong |
| `404` | no event with that `event_id` |
| `415` | content type not in the allowed set |

Screenshots are pruned by `SCREENSHOT_RETENTION_DAYS` (default 90), but those
attached to events on **unpaid** shifts are kept indefinitely.

## `GET /api/shift/current`

A polling endpoint for HA dashboards. Cheap (one indexed query each) and safe
to hit every 30–60 seconds.

### Response (200, JSON)

```json
{
  "shift": {
    "shift_id": 17,
    "nanny_name": "Jane",
    "started_at": "2026-05-05T09:15:00+10:00",
    "duration_minutes": 142
  },
  "unresolved_count": 2,
  "last_event": {
    "event_id": 42,
    "resolution": "arrival",
    "occurred_at": "2026-05-05T09:15:03+10:00"
  }
}
```

| Field | Notes |
|---|---|
| `shift` | The currently open shift. `null` when no shift is open. If multiple shifts are open (rare — one per nanny on the same day), the most recently started one wins. |
| `shift.duration_minutes` | Floor-minutes since `started_at`, computed in the configured `TZ`. |
| `unresolved_count` | Number of `ha_events` rows still in the unresolved queue. Drives a "needs human" badge in HA. |
| `last_event` | The most recent `ha_events` row regardless of resolution, or `null` if none have ever been recorded. |

### Errors

| Code | When |
|---|---|
| `401` | API key missing/wrong |

### HA snippets

`configuration.yaml`:

```yaml
rest:
  - resource: "{{ shifter_base_url }}/api/shift/current"
    headers:
      X-API-Key: !secret shifter_api_key
    scan_interval: 60
    sensor:
      - name: "Nanny on shift"
        unique_id: shifter_current_nanny
        value_template: "{{ value_json.shift.nanny_name if value_json.shift else 'none' }}"
        json_attributes_path: "$.shift"
        json_attributes:
          - shift_id
          - started_at
          - duration_minutes
      - name: "Nanny shift duration (min)"
        unique_id: shifter_current_duration
        unit_of_measurement: "min"
        value_template: "{{ value_json.shift.duration_minutes if value_json.shift else 0 }}"
      - name: "Shifter unresolved events"
        unique_id: shifter_unresolved_count
        value_template: "{{ value_json.unresolved_count }}"
```

Dashboard card (Lovelace) using the sensors above:

```yaml
type: entities
title: Nanny shift
entities:
  - entity: sensor.nanny_on_shift
    name: On shift
  - entity: sensor.nanny_shift_duration_min
    name: Duration
  - type: conditional
    conditions:
      - entity: sensor.shifter_unresolved_events
        state_not: "0"
    row:
      entity: sensor.shifter_unresolved_events
      name: ⚠ Unresolved events
```

## Resolution policy

What shifter does after debounce:

| State | Result |
|---|---|
| No nanny scheduled today **and** no fresh open shift | `ignored` — background noise |
| One expected nanny hasn't arrived yet | `arrival` — opens an unconfirmed shift (preferred even if a stale shift is still open) |
| Exactly one fresh open shift, no expected arrival pending | `departure` — closes that shift |
| Anything ambiguous (multiple expected, multiple fresh open, etc.) | `unresolved` — surfaced in the dashboard for one-click manual attribution |

### Stale open shifts

An open shift that was never clocked-out is "stale" and ignored by the
resolver if either:

- another shift has been started after it (the next bucket has begun), or
- it's been open for longer than `SHIFT_STALE_HOURS` (default 16h).

Stale shifts stay open in the database and are flagged on the dashboard for
manual close/edit. This stops a stray morning event (e.g. a parent leaving
for work) from accidentally closing yesterday's never-clocked-out shift.

The 16h default fits a long overnight shift (e.g. 7pm Mon → 9am Tue) without
prematurely staling it. Bump `SHIFT_STALE_HOURS` if you have a nanny who
genuinely works longer than that.

If you pass `event_type=arrival` or `departure`, shifter still resolves the
*nanny* via the schedule, but it will only attempt the matching transition. If
that transition is ambiguous (e.g. `arrival` but two nannies are due and neither
has clocked in), the event becomes `unresolved`.

## Debounce

- Set by `HA_DEBOUNCE_MINUTES` (default `15`).
- Keyed by `source`. Events without a `source` are *not* debounced — that's
  another reason to always send one.
- A debounced event is still recorded in the DB with `resolution=ignored` and
  a `note` explaining why; it just won't open or close a shift. This means
  you can audit suppression after the fact.
- Only previous events with `resolution != 'ignored'` count as debounce
  anchors; chains of ignored events don't accumulate a longer suppression
  window.

## Manual attribution UI

Unresolved events live at `/api/events/unresolved` (session auth). The page
lists each event with its source, time, and any attached screenshot, plus a
form to pick a nanny and direction. Posting that form calls
`POST /api/events/{event_id}/attribute` and creates or closes the shift as
appropriate. HA never needs to touch this.

## Worked HA configuration

`secrets.yaml`:

```yaml
shifter_api_key: "<long-random-string-matching-shifter's-API_KEY>"
shifter_base_url: "http://shifter:8000"
```

`configuration.yaml` — minimal event submission:

```yaml
rest_command:
  shifter_event:
    url: "{{ shifter_base_url }}/api/events"
    method: POST
    content_type: "application/json"
    headers:
      X-API-Key: !secret shifter_api_key
    payload: >
      {"occurred_at": "{{ now().isoformat() }}",
       "source": "{{ source | default('unknown') }}"
       {% if event_type is defined %}, "event_type": "{{ event_type }}"{% endif %}
      }
```

Trigger from any automation:

```yaml
automation:
  - alias: "Front door movement"
    trigger:
      - platform: state
        entity_id: binary_sensor.frigate_front_door_person
        to: "on"
    action:
      - service: rest_command.shifter_event
        data:
          source: "frigate-front-door"
```

### Capturing the response, then uploading a screenshot

`rest_command` discards the response body, so to act on `event_id` use the
`rest` integration's `command` form (or a `shell_command` calling `curl`).
Example using a single shell command that posts the event, parses the
`event_id`, and uploads a Frigate snapshot only when the resolution is
*not* `ignored`:

`shell_command`:

```yaml
shell_command:
  shifter_event_with_snapshot: >-
    bash -c '
      RESP=$(curl -sf -X POST "{{ shifter_base_url }}/api/events"
        -H "X-API-Key: {{ shifter_api_key }}"
        -H "Content-Type: application/json"
        -d "{\"occurred_at\":\"$(date -Iseconds)\",\"source\":\"{{ source }}\"}");
      EID=$(echo "$RESP" | jq -r .event_id);
      RES=$(echo "$RESP" | jq -r .resolution);
      if [ "$RES" != "ignored" ]; then
        curl -sf -X POST "{{ shifter_base_url }}/api/events/$EID/screenshot"
          -H "X-API-Key: {{ shifter_api_key }}"
          -F "file=@/config/www/frigate/{{ camera }}-latest.jpg;type=image/jpeg";
      fi'
```

(Render the templated values with `data:` keys when calling the service.)

## Quick test from the CLI

Sanity-check the endpoint without HA in the loop:

```sh
curl -sf -X POST http://localhost:8000/api/events \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"occurred_at":"2026-05-04T07:32:00+10:00","source":"cli-test"}' | jq
```

Expected response (depending on schedule/state):

```json
{
  "event_id": 1,
  "resolution": "arrival",
  "nanny_id": 1,
  "shift_id": 1,
  "note": null
}
```

Then attach an image:

```sh
curl -sf -X POST http://localhost:8000/api/events/1/screenshot \
  -H "X-API-Key: $API_KEY" \
  -F "file=@/tmp/snapshot.jpg;type=image/jpeg" | jq
```
