"""Match-review routes: reconciling a synced release against a manually-added
library entry (merge, keep-separate, bulk merge, clear dismissed).

Moved verbatim out of `pages.py` — no behaviour changes. The review logic itself
lives in `match_review.py`; this module is only the HTTP surface for it.
"""

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session, joinedload, selectinload

from . import match_review, models
from .models import get_db
from .pages_common import (
    _base_ctx,
    _build_detail_pane_visuals,
    _extract_igdb_meta,
    _extract_steam_meta,
    get_web_user,
    templates,
)

router = APIRouter()


@router.get("/library/match-review")
def match_review_page(
    request: Request,
    show_skipped: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    candidates = match_review.get_candidates(db, current_user, include_skipped=show_skipped)
    # Attach the synced release object to each candidate for the template
    # (look up by platform_source + external_id)
    enriched = []
    for c in candidates:
        synced_release = db.query(models.GameRelease).filter_by(source=c.platform_source, external_id=c.external_id).first()
        synced_entry = None
        if synced_release:
            synced_entry = db.query(models.UserLibraryEntry).filter_by(user_id=current_user.id, release_id=synced_release.id).first()
        enriched.append(
            {
                "candidate": c,
                "manual_entry": c.manual_entry,
                "synced_release": synced_release,
                "synced_entry": synced_entry,
                "label": match_review.confidence_label(c.match_score),
                "color": match_review.confidence_css(c.match_score),
            }
        )

    # Group by manual_entry_id so multi-candidate entries can be rendered
    # as a single "pick one" card rather than separate cards.
    groups: list[dict] = []
    _seen: dict[int, dict] = {}
    for row in enriched:
        mid = row["candidate"].manual_entry_id
        if mid not in _seen:
            g = {
                "manual_entry": row["manual_entry"],
                "candidates": [],
                "multi": False,
            }
            _seen[mid] = g
            groups.append(g)
        _seen[mid]["candidates"].append(row)
    for g in groups:
        g["multi"] = len(g["candidates"]) > 1
    groups.sort(
        key=lambda g: (
            not g["multi"],
            g["manual_entry"].title.lower(),
        )
    )

    pending = match_review.pending_count(db, current_user)
    return templates.TemplateResponse(
        request=request,
        name="match_review.html",
        context={
            "current_user": current_user,
            "enriched": enriched,
            "groups": groups,
            "pending": pending,
            "show_skipped": show_skipped,
            **_base_ctx(db, current_user),
        },
    )


@router.post("/library/match-review/{candidate_id}/merge")
def match_review_merge(
    candidate_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    candidate = (
        db.query(models.SyncMatchCandidate)
        .join(models.UserLibraryEntry, models.SyncMatchCandidate.manual_entry_id == models.UserLibraryEntry.id)
        .filter(models.SyncMatchCandidate.id == candidate_id, models.UserLibraryEntry.user_id == current_user.id)
        .first()
    )
    if not candidate:
        return Response(status_code=404)
    ok = match_review.merge_candidate(db, candidate, current_user)
    kind = "success" if ok else "danger"
    body = "Entries merged." if ok else "Merge failed — synced entry not found."
    return templates.TemplateResponse(
        request=request,
        name="partials/_toast.html",
        context={"kind": kind, "body": body},
        headers={"HX-Reswap": "none"},
    )


@router.post("/library/match-review/{candidate_id}/skip")
def match_review_skip(
    candidate_id: int,
    request: Request,
    note: str = Form(""),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    candidate = (
        db.query(models.SyncMatchCandidate)
        .join(models.UserLibraryEntry, models.SyncMatchCandidate.manual_entry_id == models.UserLibraryEntry.id)
        .filter(models.SyncMatchCandidate.id == candidate_id, models.UserLibraryEntry.user_id == current_user.id)
        .first()
    )
    if not candidate:
        return Response(status_code=404)
    match_review.dismiss_candidate(db, candidate, note=note or None)
    return templates.TemplateResponse(
        request=request,
        name="partials/_toast.html",
        context={"kind": "success", "body": "Kept separate."},
        headers={"HX-Reswap": "none"},
    )


@router.get("/library/match-review/{candidate_id}/preview")
def match_review_preview(
    candidate_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Render a preview pane showing what the synced entry looks like, with
    Confirm / Dismiss / Close actions pinned in the footer."""
    candidate = (
        db.query(models.SyncMatchCandidate)
        .join(models.UserLibraryEntry, models.SyncMatchCandidate.manual_entry_id == models.UserLibraryEntry.id)
        .filter(models.SyncMatchCandidate.id == candidate_id, models.UserLibraryEntry.user_id == current_user.id)
        .first()
    )
    if not candidate:
        return Response(status_code=404)

    # Load the synced (surviving) entry
    synced_entry = (
        db.query(models.UserLibraryEntry)
        .options(
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game),
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.artwork),
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.platform_obj),
            joinedload(models.UserLibraryEntry.completions),
            selectinload(models.UserLibraryEntry.user_artwork),
        )
        .join(models.GameRelease)
        .filter(
            models.GameRelease.external_id == candidate.external_id,
            models.GameRelease.source == candidate.platform_source,
            models.UserLibraryEntry.user_id == current_user.id,
        )
        .first()
    )

    # Load the manual entry (the one that will be removed)
    manual_entry = (
        db.query(models.UserLibraryEntry)
        .options(
            joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game),
            joinedload(models.UserLibraryEntry.completions),
            selectinload(models.UserLibraryEntry.user_artwork),
        )
        .filter_by(id=candidate.manual_entry_id)
        .first()
    )

    visuals = _build_detail_pane_visuals(db, synced_entry, synced_entry.release.game, synced_entry.release) if synced_entry else {}
    appdetails = (synced_entry.release.raw_data or {}).get("appdetails") or {} if synced_entry else {}
    # Completions come from the manual entry — they migrate to the synced entry on confirm
    completions = sorted(manual_entry.completions, key=lambda c: c.completed_at, reverse=True) if manual_entry else []

    return templates.TemplateResponse(
        request=request,
        name="partials/match_review_preview.html",
        context={
            "candidate": candidate,
            "entry": synced_entry,
            "game": synced_entry.release.game if synced_entry else None,
            "release": synced_entry.release if synced_entry else None,
            "appdetails": appdetails,
            "steam_meta": _extract_steam_meta(appdetails),
            "igdb_meta": _extract_igdb_meta(synced_entry.release) if synced_entry else {},
            "completions": completions,
            "manual_entry": manual_entry,
            "current_user": current_user,
            "needs_refresh": False,
            "fresh_open": False,
            **visuals,
        },
    )


@router.post("/library/match-review/merge-bulk")
def match_review_merge_bulk(
    request: Request,
    candidate_ids: str = Form(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    ids = [int(x) for x in candidate_ids.split(",") if x.strip().isdigit()]
    merged = 0
    failed = 0
    for cid in ids:
        candidate = (
            db.query(models.SyncMatchCandidate)
            .join(models.UserLibraryEntry, models.SyncMatchCandidate.manual_entry_id == models.UserLibraryEntry.id)
            .filter(models.SyncMatchCandidate.id == cid, models.UserLibraryEntry.user_id == current_user.id)
            .first()
        )
        if candidate and match_review.merge_candidate(db, candidate, current_user):
            merged += 1
        else:
            failed += 1
    parts = [f"{merged} merged"]
    if failed:
        parts.append(f"{failed} failed")
    msg = "Bulk merge complete — " + ", ".join(parts) + "."
    kind = "danger" if failed and not merged else "success"
    return templates.TemplateResponse(
        request=request,
        name="partials/_toast.html",
        context={"kind": kind, "body": msg},
        headers={"HX-Refresh": "true"},
    )


@router.post("/library/match-review/clear-dismissed")
def match_review_clear_dismissed(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Delete all dismissed candidates for this user so they can be re-detected on next scan."""
    deleted = (
        db.query(models.SyncMatchCandidate)
        .join(models.UserLibraryEntry, models.SyncMatchCandidate.manual_entry_id == models.UserLibraryEntry.id)
        .filter(
            models.UserLibraryEntry.user_id == current_user.id,
            models.SyncMatchCandidate.status == "dismissed",
        )
        .delete(synchronize_session=False)
    )
    db.commit()
    body = f"{deleted} dismissed match{'es' if deleted != 1 else ''} cleared — they'll resurface on next scan."
    return templates.TemplateResponse(
        request=request,
        name="partials/_toast.html",
        context={"kind": "success", "body": body},
        headers={"HX-Reswap": "none"},
    )
