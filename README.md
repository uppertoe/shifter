# shifter

Self-hosted nanny shift tracking for the household. Built to replace TimeTagger when two nannies, hourly rates, occasional expenses, and Home Assistant integration combine into something a generic time-tracker can't model cleanly.

Stack: FastAPI + Pico CSS + HTMX + SQLite. Single Docker container, ~one secret to set, sits behind your existing reverse proxy + Authelia.

## Features

- **Nannies & pay rates** — multiple nannies, each with effective-dated hourly rates so historical shifts always price correctly.
- **Shifts** — manual entry, datetime-local pickers, optional per-shift rate override or flat rate, free-text notes.
- **Expenses** — add expense lines per shift (e.g. lunch, outings). Tallied separately on the unpaid view.
- **Paid / unpaid tracking** — per-nanny "owed" view with shifts subtotal + expenses subtotal + grand total. Single click marks everything paid on a chosen date.
- **Reports** — per-nanny totals by AU financial year (Jul–Jun), calendar year, last 30 days, or custom range. CSV export for shifts and expenses.
- **Schedule** — recurring weekly patterns ("Anita / Wed / 07:00–18:00") + per-day overrides on a calendar UI. Used to attribute HA events.
- **Home Assistant integration** — HA sends raw, *unattributed* presence events. Shifter resolves the nanny via the schedule and current open-shift state. Optional screenshot upload (Frigate, etc.).
- **TimeTagger import** — one-shot CLI that maps your existing tags to nannies, extracts expense candidates from descriptions, idempotent re-runs.

## Run (production)

```sh
cp .env.example .env       # set API_KEY (≥8 chars), ALLOWED_USERS
docker compose pull        # pulls the multi-arch image from GHCR
docker compose up -d
```

The app listens on `127.0.0.1:8000`. Front it with your existing reverse proxy and Authelia; shifter trusts `Remote-User` from the proxy.

The container runs as `uid=1000`. If your host's first user is also uid 1000 (typical on Linux), the bind-mounted `./data` directory will work out of the box. Otherwise: `chown -R 1000:1000 ./data` once.

CI publishes images for **linux/amd64 and linux/arm64** to `ghcr.io/<your-fork>/shifter`. To build locally instead of pulling, swap the `image:` line in `docker-compose.yml` for `build: .`

## Run (development)

You don't need Docker for local hacking. Dev mode bypasses both Authelia and the API key.

```sh
brew install uv               # or: curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv .venv
uv pip install -e ".[dev]"    # or use the included pip-based .venv approach
./scripts/dev.sh              # binds to 127.0.0.1:8765
```

Open `http://127.0.0.1:8765` — no header injection needed. The DB lives at `data/shifter-dev.db`; delete it any time to start fresh (migrations re-run automatically).

## Config

| Env var | Default | Notes |
|---|---|---|
| `TZ` | `Australia/Melbourne` | Display & computation timezone. |
| `DATABASE_PATH` | `/data/shifter.db` | SQLite file. WAL enabled. |
| `SCREENSHOT_DIR` | `/data/screenshots` | Screenshot storage root. |
| `SCREENSHOT_RETENTION_DAYS` | `90` | Older screenshots pruned daily; kept indefinitely on unpaid shifts. |
| `API_KEY` | _required (≥8)_ | Used by HA on the `X-API-Key` header for `/api/*` routes. Optional in dev mode. |
| `ALLOWED_USERS` | _(empty = allow all)_ | Comma-separated `Remote-User` allowlist. |
| `FY_START_MONTH` | `7` | Financial year start month (AU = July, NZ = April, etc.). |
| `HA_DEBOUNCE_MINUTES` | `15` | Same-source events within this window are recorded as `ignored`. |
| `SHIFT_STALE_HOURS` | `16` | Open shifts older than this stop accepting auto-attributed events (the "left clocked-in overnight" case). The shift stays open in the DB and is flagged on the dashboard for manual close. |
| `FRIGATE_BASE_URL` | _(empty)_ | If set (e.g. `https://frigate.example.com`), the dashboard adds a "Frigate" link next to each pending HA shift, scoped to that shift's day. |
| `DEV_MODE` | `false` | When true, auth is bypassed. **Never enable in prod.** |
| `DEV_USER` | `dev` | Username assumed when dev mode receives no `Remote-User`. |

## Home Assistant integration

Shifter accepts raw presence events on `POST /api/events` (auth: `X-API-Key`) and figures out which nanny they belong to using your schedule + open-shift state. HA doesn't need to identify the person. Optional `POST /api/events/{event_id}/screenshot` attaches an image (e.g. from Frigate).

Full request/response schemas, error codes, resolution policy, debounce semantics, and worked HA snippets (including a one-shot `shell_command` that posts the event and uploads a snapshot only when shifter actually opened or closed a shift) live in **[docs/home-assistant.md](docs/home-assistant.md)**.

## TimeTagger import

```sh
docker compose run --rm shifter python -m scripts.import_timetagger /data/import/export.tsv --dry-run
```

The script:
- Reads tab-separated `key / start / stop / tags / description`.
- Prompts you for tag→nanny mapping (or pass `--map anita=1 --map joy=2` to skip).
- Filters out rows whose tags don't map (e.g. `#oncall` for your own time).
- Imports `imported`-source shifts as paid (default = today; override with `--paid-on YYYY-MM-DD` or `''` for unpaid).
- Extracts expense candidates from descriptions ("$53 for aquarium", "lunch $14") and stores them with `pending_review = 1` so you can vet each before they count.
- Idempotent: re-running with the same file is a no-op.

## Development

```sh
uv sync                  # install deps from uv.lock
uv run pytest -q         # 72 tests, ~0.3s
```

Tests cover pay computation, auth dependencies, schedule materializer, HA event resolution, and the TimeTagger importer (using a representative fixture in `tests/fixtures/`).

### CI / publishing

`.github/workflows/ci.yml` runs tests on every push & PR, then on `main` and `v*.*.*` tags builds a **multi-arch** image and pushes it to **GitHub Container Registry** (`ghcr.io/<owner>/shifter`).

Tags emitted by CI:
- `latest` (on main)
- `v1.2.3`, `1.2`, `1`  (on git tags `v1.2.3`)
- `<short-sha>`  (every push)
- `main`  (latest of branch)

To cut a release: `git tag v0.1.0 && git push --tags`. CI takes care of the rest.

## Data model

Stored in SQLite. Money as integer cents. Datetimes as ISO 8601 with offset.

```
nannies(id, name, active)
pay_rates(id, nanny_id, rate_cents, effective_from)
shifts(id, nanny_id, start_time, end_time, rate_override_cents,
       flat_rate_cents, notes, source, confirmed, paid_on,
       paid_amount_cents, paid_note, timetagger_key, ...audit)
expenses(id, shift_id, amount_cents, description, paid_on, pending_review)
schedule_patterns(id, nanny_id, day_of_week, start_time, end_time,
                  active_from, active_until)
expected_shifts(id, nanny_id, date, start_time, end_time, pattern_id,
                source, cancelled)
ha_events(id, occurred_at, source, event_type_hint, nanny_id, shift_id,
          expected_shift_id, resolution, resolution_note)
screenshots(id, ha_event_id, filename, content_type, size_bytes)
```
