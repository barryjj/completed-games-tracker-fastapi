"""Re-attach SGDB artwork to PSN entries after a purge + re-sync.

A purge deletes user_artwork because those rows point at entry ids that will not
exist afterwards. The ART itself is still fine — it was fetched by title and the
URLs do not change — so re-fetching it from SGDB is thousands of API calls spent
recovering something a backup already holds.

The join key is GameRelease.external_id: Sony's own titleId /
npCommunicationId, which is stable across a re-crawl. That is what lets a row
from the old database find its new entry.

    .venv/bin/python scripts/restore_psn_artwork.py <backup.bak>              # dry run
    .venv/bin/python scripts/restore_psn_artwork.py <backup.bak> --execute

Run it AFTER the re-sync, so there are entries to attach to. Idempotent: an
entry that already has art of that type is left alone, so a partially re-fetched
library is safe to run this against.
"""

import argparse
import collections
import os
import sqlite3
import sys

DB = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend", "app.db"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("backup", help="the .bak written by purge_psn.py")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    backup = os.path.abspath(args.backup)
    if not os.path.exists(backup):
        print(f"no such backup: {backup}")
        return 1

    old = sqlite3.connect(backup)
    saved = old.execute(
        """SELECT r.external_id, ua.artwork_type, ua.source, ua.url, ua.file_path,
                  ua.mime_type, ua.width, ua.height
           FROM user_artwork ua
           JOIN user_library e ON e.id = ua.entry_id
           JOIN game_releases r ON r.id = e.release_id
           WHERE e.import_source='psn_import' AND ua.url IS NOT NULL"""
    ).fetchall()
    print(f"artwork rows in backup: {len(saved)}")
    print("by type:", dict(collections.Counter(r[1] for r in saved)))

    new = sqlite3.connect(DB)
    # external_id is NOT unique on its own — one trophy set can become several
    # releases (cross-buy). Every matching entry gets the art.
    entries = collections.defaultdict(list)
    for ext, entry_id, user_id, game_id in new.execute(
        """SELECT r.external_id, e.id, e.user_id, r.game_id
           FROM user_library e JOIN game_releases r ON r.id = e.release_id
           WHERE r.source='psn'"""
    ):
        entries[ext].append((entry_id, user_id, game_id))

    have = {
        (entry_id, art_type)
        for entry_id, art_type in new.execute("SELECT entry_id, artwork_type FROM user_artwork WHERE entry_id IS NOT NULL")
    }

    todo, no_entry, already = [], 0, 0
    for ext, art_type, source, url, file_path, mime, w, h in saved:
        targets = entries.get(ext)
        if not targets:
            no_entry += 1
            continue
        for entry_id, user_id, game_id in targets:
            if (entry_id, art_type) in have:
                already += 1
                continue
            todo.append((user_id, entry_id, game_id, art_type, source, url, file_path, mime, w, h))

    print(f"\n  {len(todo):>6}  would be re-attached")
    print(f"  {already:>6}  already present (left alone)")
    print(f"  {no_entry:>6}  have no matching entry yet — re-sync first if this is high")

    if not args.execute:
        print("\nDry run only. Re-run with --execute.")
        return 0

    new.executemany(
        """INSERT INTO user_artwork
           (user_id, entry_id, game_id, artwork_type, source, url, file_path, mime_type, width, height)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        todo,
    )
    new.commit()
    print(f"\nre-attached {len(todo)} artwork rows — that many SGDB lookups not spent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
