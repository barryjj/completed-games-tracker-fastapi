"""Remove every PSN-sourced row so a sync can be re-run from scratch.

Dry-run by default. Pass --execute to actually delete.

WHY THIS IS A SCRIPT AND NOT A BUTTON: it is destructive, rare, and only used
while developing the PSN pipeline. A button invites an accidental click.

SAFETY: refuses to run if any Completion is attached to a PSN entry. Completions
are hand-curated history — the library can be re-synced from Sony in minutes,
completions cannot be recovered at all. If this check ever trips, stop and
reattach those completions elsewhere first.

    .venv/bin/python scripts/purge_psn.py              # dry run
    .venv/bin/python scripts/purge_psn.py --execute    # after reading the dry run
"""

import argparse
import datetime
import os
import shutil
import sqlite3
import sys

DB = os.path.join(os.path.dirname(__file__), "..", "backend", "app.db")


def counts(cur):
    q = {
        "library entries (psn_import)": "SELECT COUNT(*) FROM user_library WHERE import_source='psn_import'",
        "releases (source=psn)": "SELECT COUNT(*) FROM game_releases WHERE source='psn'",
        "review candidates": "SELECT COUNT(*) FROM psn_review_candidates",
        "user_artwork on psn entries": """
            SELECT COUNT(*) FROM user_artwork ua
            WHERE ua.entry_id IN (SELECT id FROM user_library WHERE import_source='psn_import')""",
        "games left orphaned": """
            SELECT COUNT(*) FROM games g
            WHERE EXISTS (SELECT 1 FROM game_releases r WHERE r.game_id=g.id AND r.source='psn')
              AND NOT EXISTS (SELECT 1 FROM game_releases r WHERE r.game_id=g.id AND r.source<>'psn')""",
    }
    return {label: cur.execute(sql).fetchone()[0] for label, sql in q.items()}


def completions_at_risk(cur):
    return cur.execute(
        """SELECT COUNT(*) FROM completions c
           JOIN user_library e ON e.id = c.library_entry_id
           WHERE e.import_source='psn_import'"""
    ).fetchone()[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="actually delete (default is a dry run)")
    args = ap.parse_args()

    db = sqlite3.connect(DB)
    cur = db.cursor()

    print("=== what a purge would remove ===")
    for label, n in counts(cur).items():
        print(f"  {n:>6}  {label}")

    at_risk = completions_at_risk(cur)
    print(f"\n  {at_risk:>6}  completions attached to PSN entries")
    if at_risk:
        print("\nREFUSING: completions are attached to PSN entries.")
        print("The library re-syncs from Sony in minutes; completions cannot be recovered.")
        print("Reattach them to non-PSN entries first, then re-run.")
        return 1

    if not args.execute:
        print("\nDry run only. Re-run with --execute to delete.")
        return 0

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{DB}.psn-purge-{stamp}.bak"
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.commit()
    shutil.copy2(DB, backup)
    print(f"\nbackup written: {os.path.abspath(backup)}")

    # Order matters: children before parents, orphans last.
    cur.execute("""DELETE FROM user_artwork WHERE entry_id IN
                   (SELECT id FROM user_library WHERE import_source='psn_import')""")
    art = cur.rowcount
    cur.execute("DELETE FROM user_library WHERE import_source='psn_import'")
    entries = cur.rowcount
    cur.execute("""DELETE FROM game_artwork WHERE release_id IN
                   (SELECT id FROM game_releases WHERE source='psn')""")
    gart = cur.rowcount
    cur.execute("DELETE FROM game_releases WHERE source='psn'")
    releases = cur.rowcount
    cur.execute("DELETE FROM psn_review_candidates")
    cands = cur.rowcount
    # Games with no releases left at all — a PSN-only game nothing else refers to.
    cur.execute("""DELETE FROM games WHERE NOT EXISTS
                   (SELECT 1 FROM game_releases r WHERE r.game_id = games.id)""")
    games = cur.rowcount
    db.commit()

    print(f"  deleted {entries} entries, {releases} releases, {cands} candidates,")
    print(f"          {art} user_artwork, {gart} game_artwork, {games} orphaned games")
    print("\nRun a PSN sync to repopulate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
