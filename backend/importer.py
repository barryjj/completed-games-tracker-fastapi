"""Spreadsheet import pipeline.

Parses an xlsx file (Google Sheets export), normalises each row, groups rows
that resolve to the same game+platform identity into ImportCandidate records,
and writes them to the DB for user review.

Columns recognised (case-insensitive, order-independent):
  #, Game, Platform, Date, Playthroughs, Notes, Collection

Tabs: one per year; tab name is the fallback year for blank/month-only dates.
"""

import datetime
import re
from io import BytesIO

import openpyxl
from sqlalchemy.orm import Session

from . import models

# Month name → number (full and abbreviated)
_MONTH_MAP: dict[str, int] = {}
for _i, _names in enumerate(
    [
        ("january", "jan"),
        ("february", "feb"),
        ("march", "mar"),
        ("april", "apr"),
        ("may",),
        ("june", "jun"),
        ("july", "jul"),
        ("august", "aug"),
        ("september", "sep", "sept"),
        ("october", "oct"),
        ("november", "nov"),
        ("december", "dec"),
    ],
    start=1,
):
    for _n in _names:
        _MONTH_MAP[_n] = _i


def _parse_date(raw: str | None, tab_year: int | None) -> datetime.date | None:
    """Normalise a raw date string to a date object.

    Accepted formats:
      - Full date: 1/1/2026, 01/01/2026, 2026-01-01
      - Month + year: "January 2019", "Jan 2019", "1/2019"
      - Month name only: "January" (uses tab_year)
      - Blank / None: Jan 1 of tab_year, or None if tab_year also unknown
    """
    if not raw:
        if tab_year:
            return datetime.date(tab_year, 1, 1)
        return None

    s = str(raw).strip()
    if not s:
        if tab_year:
            return datetime.date(tab_year, 1, 1)
        return None

    # openpyxl may hand us a datetime object for formatted cells
    if isinstance(raw, (datetime.date, datetime.datetime)):
        d = raw if isinstance(raw, datetime.date) else raw.date()
        return d

    # ISO date: 2026-01-01
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return datetime.date(int(m[1]), int(m[2]), int(m[3]))

    # Slash full date: 1/1/2026 or 01/01/26
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
    if m:
        y = int(m[3])
        if y < 100:
            y += 2000
        return datetime.date(y, int(m[1]), int(m[2]))

    # Month/year: 1/2019
    m = re.fullmatch(r"(\d{1,2})/(\d{4})", s)
    if m:
        return datetime.date(int(m[2]), int(m[1]), 1)

    # "January 2019" or "Jan 2019"
    m = re.fullmatch(r"([A-Za-z]+)\s+(\d{4})", s)
    if m:
        mon = _MONTH_MAP.get(m[1].lower())
        if mon:
            return datetime.date(int(m[2]), mon, 1)

    # "January" alone — use tab year
    mon = _MONTH_MAP.get(s.lower())
    if mon and tab_year:
        return datetime.date(tab_year, mon, 1)

    # Pure year: "2019"
    m = re.fullmatch(r"(\d{4})", s)
    if m:
        return datetime.date(int(m[1]), 1, 1)

    return None


def _parse_playthroughs(raw: str | None) -> str | None:
    """Normalise playthroughs: strip '+', return as string or None."""
    if raw is None:
        return None
    s = str(raw).strip().rstrip("+").strip()
    if not s or s == "0":
        return None
    try:
        f = float(s)
        return str(int(f)) if f == int(f) else s
    except ValueError:
        return None


def _tab_year(sheet_title: str) -> int | None:
    """Extract a 4-digit year from a tab name like '2019' or 'Games 2019'."""
    m = re.search(r"\b(20\d{2}|19\d{2})\b", sheet_title)
    return int(m[0]) if m else None


def _col_map(header_row: list) -> dict[str, int]:
    """Return {normalised_name: col_index} from a header row."""
    mapping = {}
    for i, cell in enumerate(header_row):
        if cell is None:
            continue
        key = str(cell).strip().lower().lstrip("#").strip()
        if key == "" or key == "#":
            mapping["#"] = i
        else:
            mapping[key] = i
        # Also store raw '#' column
        if str(cell).strip() == "#":
            mapping["#"] = i
    return mapping


def _cell(row: tuple, col_map: dict, *keys: str) -> str | None:
    """Get the first matching key from a row, return stripped string or None."""
    for k in keys:
        idx = col_map.get(k)
        if idx is not None and idx < len(row):
            v = row[idx]
            if v is None:
                continue
            s = str(v).strip() if not isinstance(v, (datetime.date, datetime.datetime)) else v
            if s == "" or s is None:
                continue
            return v if isinstance(v, (datetime.date, datetime.datetime)) else str(v).strip()
    return None


def _normalize_title(title: str) -> str:
    """Strip punctuation and collapse whitespace for fuzzy matching and grouping."""
    t = title.lower()
    t = re.sub(r"[^\w\s]", " ", t)  # punctuation → space
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _group_key(title: str, platform_id: int | None, raw_platform: str) -> str:
    """Stable grouping key: normalised title + resolved platform (or raw if unresolved)."""
    t = _normalize_title(title)
    p = str(platform_id) if platform_id is not None else f"raw:{raw_platform.strip().lower()}"
    return f"{t}|{p}"


class ParseResult:
    def __init__(self):
        self.candidates: list[dict] = []  # [{raw_title, raw_platform, platform_id, rows:[...]}]
        self.skipped_rows: int = 0
        self.total_rows: int = 0


def _row_values(sheet_row) -> tuple:
    """Convert a row of Cell objects to values, preserving percentage display strings."""
    out = []
    for cell in sheet_row:
        v = cell.value
        if isinstance(v, (int, float)) and cell.number_format and "%" in cell.number_format:
            pct = int(round(v * 100))
            out.append(f"{pct}%")
        else:
            out.append(v)
    return tuple(out)


def parse_xlsx(file_bytes: bytes, db: Session, user_id: int) -> ParseResult:
    """Parse an xlsx file and return grouped ImportCandidate data (not yet written to DB)."""
    wb = openpyxl.load_workbook(BytesIO(file_bytes), data_only=True)
    result = ParseResult()

    # groups: group_key → {raw_title, raw_platform, platform_id, rows:[row_dict,...]}
    groups: dict[str, dict] = {}

    for sheet in wb.worksheets:
        tab_year = _tab_year(sheet.title)
        rows = [_row_values(r) for r in sheet.iter_rows()]
        if not rows:
            continue

        # Find header row — first row that has "game" or "title" in any cell
        header_idx = None
        for i, row in enumerate(rows):
            cells = [str(c).strip().lower() for c in row if c is not None]
            if any(c in ("game", "title") for c in cells):
                header_idx = i
                break
        if header_idx is None:
            result.skipped_rows += len(rows)
            continue

        header_list = list(rows[header_idx])
        # Some tabs have sequential row numbers in col A with no header — treat as #
        if header_list and header_list[0] is None and "#" not in _col_map(header_list):
            header_list = list(header_list)
            header_list[0] = "#"
        cols = _col_map(header_list)

        for row in rows[header_idx + 1 :]:
            result.total_rows += 1

            # Skip entirely blank rows
            if all(c is None or str(c).strip() == "" for c in row):
                result.skipped_rows += 1
                continue

            raw_title = _cell(row, cols, "game", "title")
            if not raw_title:
                result.skipped_rows += 1
                continue

            raw_platform = _cell(row, cols, "platform") or ""
            raw_date = _cell(row, cols, "date")
            raw_playthroughs = _cell(row, cols, "playthroughs", "times completed")
            raw_notes = _cell(row, cols, "notes")
            raw_collection = _cell(row, cols, "collection")

            # Row number from # column
            row_num_raw = _cell(row, cols, "#")
            try:
                row_number = int(float(str(row_num_raw))) if row_num_raw else None
            except (ValueError, TypeError):
                row_number = None

            platform_str = re.split(r"[·|/]", raw_platform)[0].strip() if raw_platform else ""
            platform_id = models.resolve_platform_id(db, platform_str) if platform_str else None
            completed_at = _parse_date(raw_date, tab_year)
            playthroughs = _parse_playthroughs(raw_playthroughs)

            key = _group_key(raw_title, platform_id, raw_platform)

            if key not in groups:
                groups[key] = {
                    "raw_title": raw_title,
                    "raw_platform": raw_platform,
                    "platform_id": platform_id,
                    "rows": [],
                }

            groups[key]["rows"].append(
                {
                    "raw_title": raw_title,
                    "raw_platform": raw_platform,
                    "raw_date": str(raw_date) if raw_date else None,
                    "raw_playthroughs": str(raw_playthroughs) if raw_playthroughs else None,
                    "raw_notes": raw_notes,
                    "raw_collection": raw_collection,
                    "source_tab": sheet.title,
                    "row_number": row_number,
                    "completed_at": completed_at,
                    "playthroughs": playthroughs,
                }
            )

    # Dedup rows within each group — same game can appear across multiple tabs
    for group in groups.values():
        seen: set = set()
        unique_rows = []
        for r in group["rows"]:
            key = (r["completed_at"], r["playthroughs"], r["raw_notes"])
            if key not in seen:
                seen.add(key)
                unique_rows.append(r)
        group["rows"] = unique_rows

    result.candidates = list(groups.values())
    return result


def write_candidates(result: ParseResult, db: Session, user_id: int, on_progress=None) -> int:
    """Write parsed groups to ImportCandidate + ImportRow rows in small batches. Returns candidate count."""

    count = 0
    skipped = 0
    for group in result.candidates:
        # Skip groups that already exist (pending or confirmed) from a previous upload
        plat_filter = (
            models.ImportCandidate.platform_id == group["platform_id"]
            if group["platform_id"] is not None
            else models.ImportCandidate.platform_id.is_(None)
        )
        already_exists = (
            db.query(models.ImportCandidate)
            .filter(
                models.ImportCandidate.user_id == user_id,
                models.ImportCandidate.raw_title == group["raw_title"],
                plat_filter,
                models.ImportCandidate.status.in_(["pending", "confirmed"]),
            )
            .first()
        )
        if already_exists:
            skipped += 1
            continue

        # Look for an existing library entry matching title + platform
        existing_entry = None
        if group["platform_id"]:
            needle = _normalize_title(group["raw_title"])
            candidates_for_platform = (
                db.query(models.UserLibraryEntry)
                .join(models.GameRelease, models.UserLibraryEntry.release_id == models.GameRelease.id)
                .join(models.Game, models.GameRelease.game_id == models.Game.id)
                .filter(
                    models.UserLibraryEntry.user_id == user_id,
                    models.GameRelease.platform_id == group["platform_id"],
                )
                .all()
            )
            for entry in candidates_for_platform:
                game_title = entry.release.game.title if entry.release and entry.release.game else ""
                if _normalize_title(game_title) == needle:
                    existing_entry = entry
                    break

        if existing_entry:
            action = "add_to_existing"
        elif group["platform_id"] is None:
            action = "needs_review"
        else:
            action = "create_new"

        candidate = models.ImportCandidate(
            user_id=user_id,
            raw_title=group["raw_title"],
            raw_platform=group["raw_platform"],
            platform_id=group["platform_id"],
            library_entry_id=existing_entry.id if existing_entry else None,
            status="pending",
            proposed_action=action,
        )
        db.add(candidate)
        db.flush()

        for row in group["rows"]:
            db.add(
                models.ImportRow(
                    candidate_id=candidate.id,
                    raw_title=row["raw_title"],
                    raw_platform=row["raw_platform"],
                    raw_date=row["raw_date"],
                    raw_playthroughs=row["raw_playthroughs"],
                    raw_notes=row["raw_notes"],
                    raw_collection=row["raw_collection"],
                    source_tab=row["source_tab"],
                    row_number=row["row_number"],
                    completed_at=row["completed_at"],
                    playthroughs=row["playthroughs"],
                )
            )
        db.commit()
        count += 1
        if on_progress:
            on_progress(count)

    return count
