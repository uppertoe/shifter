"""CLI for importing a TimeTagger TSV export.

Usage:
    python -m scripts.import_timetagger /path/to/export.tsv [--dry-run] [--map TAG=NANNY_ID]

Interactive by default: shows the unique tags found and asks which nanny each
maps to. --map flags skip prompts (repeatable, e.g. --map anita=1 --map joy=2).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from shifter import db, importer, repos
from shifter.config import get_settings


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Import a TimeTagger TSV export into shifter.")
    p.add_argument("file", type=Path, help="Path to the TimeTagger .tsv (or .csv with tabs).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would happen without writing anything.")
    p.add_argument("--map", action="append", default=[], dest="maps",
                   help="Skip the prompt: TAG=NANNY_ID (repeatable). Tag is case-insensitive, no '#'.")
    p.add_argument("--paid-on", default=date.today().isoformat(),
                   help="Paid date stamped on imported shifts (default: today). Pass '' to import as unpaid.")
    p.add_argument("--user", default="import",
                   help="created_by/updated_by audit value (default: 'import').")
    return p.parse_args(argv)


def parse_maps(maps: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for m in maps:
        if "=" not in m:
            sys.exit(f"--map expects TAG=NANNY_ID, got {m!r}")
        tag, sid = m.split("=", 1)
        try:
            out[tag.strip().lower().lstrip("#")] = int(sid)
        except ValueError:
            sys.exit(f"--map nanny id must be an integer, got {sid!r}")
    return out


def prompt_for_mapping(tags: dict[str, int], nannies: list) -> dict[str, int]:
    """Walk the user through unique tags, ask which nanny each maps to."""
    print("\nFound these tags in the file (count in parens):\n")
    for tag, n in sorted(tags.items(), key=lambda kv: -kv[1]):
        print(f"  #{tag}  ({n})")
    print()
    print("Available nannies:")
    for n in nannies:
        print(f"  [{n['id']}] {n['name']}")
    print("  [s] skip this tag (don't import)")
    print()

    mapping: dict[str, int] = {}
    valid_ids = {n["id"] for n in nannies}
    for tag in sorted(tags, key=lambda t: -tags[t]):
        while True:
            ans = input(f"  '#{tag}' → nanny id, or [s]kip: ").strip().lower()
            if ans == "s" or ans == "":
                break
            try:
                nid = int(ans)
            except ValueError:
                print("    please enter a number, or 's' to skip")
                continue
            if nid not in valid_ids:
                print(f"    no nanny with id {nid}")
                continue
            mapping[tag] = nid
            break
    return mapping


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.file.exists():
        sys.exit(f"file not found: {args.file}")

    settings = get_settings()
    tz = settings.zoneinfo

    with db.connect(settings.database_path) as conn:
        nannies = repos.list_nannies(conn, include_inactive=True)
        if not nannies:
            sys.exit("No nannies in the database. Add at least one nanny first via the UI.")

        # Build the tag→nanny map.
        if args.maps:
            mapping = parse_maps(args.maps)
            valid_ids = {n["id"] for n in nannies}
            unknown = [k for k, v in mapping.items() if v not in valid_ids]
            if unknown:
                sys.exit(f"--map references unknown nanny id(s): {unknown}")
        else:
            tags = importer.unique_tags(args.file, tz)
            if not tags:
                sys.exit("No tags found in the file. Nothing to import.")
            mapping = prompt_for_mapping(tags, nannies)
            if not mapping:
                print("\nNo tags mapped. Nothing to do.")
                return 0

        plan = importer.plan_import(conn, args.file, mapping, tz=tz)

        # Show plan.
        print("\n=== Import plan ===")
        print(f"File: {plan.file}")
        print(f"Total rows: {plan.rows_total}")
        for nid, count in sorted(plan.by_nanny_id.items()):
            name = next((n["name"] for n in nannies if n["id"] == nid), f"#{nid}")
            print(f"  {name}: {count} shifts")
        if plan.expense_candidates_total:
            print(f"Expense candidates extracted from descriptions: "
                  f"{plan.expense_candidates_total} (will be marked 'pending review')")
        if plan.not_worked_count:
            print(f"Sick / not-worked rows: {plan.not_worked_count} "
                  f"(imported as paid full shifts; the note is preserved)")
        print(f"Skipped (no mapped tag): {plan.rows_skipped_no_mapping}")
        if plan.skipped_unique_tags:
            top = ", ".join(f"#{t}({c})" for t, c in plan.skipped_unique_tags.most_common(8))
            print(f"  most common unmapped tags: {top}")
        print(f"Skipped (empty/malformed): {plan.rows_skipped_empty}")
        print(f"Skipped (already imported by key): {plan.rows_skipped_already_imported}")

        if args.dry_run:
            print("\n--dry-run: no changes made.")
            return 0

        if not plan.by_nanny_id:
            print("\nNothing to import.")
            return 0

        ans = input("\nProceed with import? [y/N]: ").strip().lower()
        if ans != "y":
            print("Aborted.")
            return 1

        paid_on = date.fromisoformat(args.paid_on) if args.paid_on else None
        result = importer.run_import(
            conn, args.file, mapping, tz=tz, paid_on=paid_on, created_by=args.user,
        )
        print("\n=== Done ===")
        print(f"Shifts inserted: {result.shifts_inserted}")
        print(f"Expense candidates inserted (pending review): {result.expenses_inserted}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
