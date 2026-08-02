"""Match-review routes: reconciling a synced release against a manually-added
library entry (merge, keep-separate, bulk merge, clear dismissed).

Moved verbatim out of `pages.py` — no behaviour changes. The review logic itself
lives in `match_review.py`; this module is only the HTTP surface for it.
"""

import json as _json

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


# PSN review lives under /tools, not /library: it is an operations queue like
# match review's sibling cards on the Tools hub, and the nav highlights by path
# prefix — under /library it lit up "Library" while the user was on a Tools page.
#
# The two PSN queues are tabs of one page: both are "PSN couldn't settle this,
# you decide", both are rows of the same psn_review_candidates table, and having
# one on Tools with the other buried in the config page was the scattered flow
# (#157).

# Per-queue sort options. Trophy progress is the discriminator for cross-play
# rows (two Crimsonland sets differ only by it); playtime is the one for
# played-only, where "did I actually play this" is the whole question.
_PSN_SORTS = {
    "cross_play": [("name", "Title"), ("progress", "Trophy progress"), ("platforms", "Platform count")],
    "played_only": [("name", "Title"), ("playtime", "Playtime"), ("recent", "Last played")],
}


def _sort_review_rows(rows: list[dict], sort: str, kind: str) -> list[dict]:
    """Sort a review queue. Unknown keys fall back to title, so a stale URL
    can't produce an arbitrarily ordered page."""
    if sort == "progress":
        return sorted(rows, key=lambda r: (-(r.get("trophy_progress") or 0), (r["name"] or "").casefold()))
    if sort == "platforms":
        return sorted(rows, key=lambda r: (-len(r.get("options") or []), (r["name"] or "").casefold()))
    if sort == "playtime":
        return sorted(rows, key=lambda r: (-(r.get("minutes") or 0), (r["name"] or "").casefold()))
    if sort == "recent":
        return sorted(rows, key=lambda r: (r.get("last_played") or "", (r["name"] or "").casefold()), reverse=True)
    return sorted(rows, key=lambda r: ((r["name"] or "").casefold(), r.get("set_index", 0)))


_PSN_REVIEW_KINDS = ("cross_play", "played_only")


@router.get("/tools/psn-review")
def psn_review_page(
    request: Request,
    kind: str = Query("cross_play"),
    view: str = Query("list"),
    platform: str = Query(""),
    q: str = Query(""),
    sort: str = Query("name"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """PSN decisions the sync can't make for you, as their own review queue.

    Same shape as import review — one server-rendered view at a time (list or
    card stack), per-row actions that take effect on click. The view is
    server-side rather than a CSS toggle so the two layouts never both exist in
    the DOM fighting over the same element ids (#163).
    """
    from . import psn

    kind = kind if kind in _PSN_REVIEW_KINDS else "cross_play"
    view = view if view in ("list", "card") else "list"

    cross_rows = psn.import_review_rows(db, current_user.id)
    played_rows = [r for r in psn.played_only_rows(db, current_user.id) if not r["decision"]]
    counts = {"cross_play": len(cross_rows), "played_only": len(played_rows)}

    rows = cross_rows if kind == "cross_play" else played_rows
    review_platforms = sorted({o["platform"] for r in cross_rows for o in r["options"]})
    if kind == "cross_play" and platform:
        rows = [r for r in rows if any(o["platform"] == platform for o in r["options"])]
    if q:
        needle = q.casefold()
        rows = [r for r in rows if needle in (r["name"] or "").casefold()]
    rows = _sort_review_rows(rows, sort, kind)

    ctx = {
        "current_user": current_user,
        **_base_ctx(db, current_user),
        "rows": rows,
        "kind": kind,
        "counts": counts,
        "view": view,
        "platform": platform,
        "q": q,
        "sort": sort,
        "sorts": _PSN_SORTS[kind],
        "has_snapshot": psn.has_synced(db, current_user.id),
        "review_platforms": review_platforms,
    }
    if request.headers.get("HX-Request"):
        # oob: the sort select is outside the swap target and its options are
        # per-queue, so it has to ride along or a tab switch leaves the other
        # queue's options in place.
        response = templates.TemplateResponse(request=request, name="partials/_psn_review_content.html", context={**ctx, "oob": True})
    else:
        response = templates.TemplateResponse(request=request, name="psn_review.html", context=ctx)
    # no-store: rows are derived live from the candidates and the cross-buy
    # reference on every render, so a reload IS the refresh — but the desktop
    # shell's WKWebView heuristically caches GETs that carry no cache headers,
    # which would pin a stale queue in a context with no user-facing reload.
    response.headers["Cache-Control"] = "no-store"
    return response


def _maybe_enrich(db: Session, user: models.User) -> None:
    """Once the review queue is empty, enrich what it created.

    Entries confirmed here arrive after the sync's own enrichment pass has run,
    so without this they sit with no store metadata and no artwork until the
    next sync — "sync again" purely as a chore. Fired on the queue emptying
    rather than per confirm, so clicking through 54 rows spawns one pass, not
    54. Both jobs self-gate, so an empty pass is nearly free.
    """
    from . import integrations, psn

    if psn.review_pending_count(db, user.id) == 0:
        integrations.kick_psn_enrichment(user)


@router.post("/tools/psn-review/bulk-confirm")
async def psn_review_bulk_confirm(
    request: Request,
    selections: str = Form(""),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Confirm a batch of cross-play rows, each with its own ticked platforms.

    Not just a list of ids like import review's bulk confirm: the whole value
    here is that most rows arrive pre-ticked correctly from the cross-buy
    reference, so the selection has to travel per row. Payload is JSON,
    {external_id: [platform, ...]}.

    Rows already decided, or platforms a trophy set doesn't cover, are dropped
    by confirm_entry_decision rather than trusted — a stale page can post
    anything, and an entry this can't un-create is the expensive mistake.
    """
    from . import integrations, psn

    try:
        parsed = _json.loads(selections) if selections.strip() else {}
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict) or not parsed:
        return Response("No rows selected.", status_code=422)

    confirmed = created = 0
    for key, platforms in parsed.items():
        if not isinstance(platforms, list):
            continue
        try:
            result = psn.confirm_entry_decision(db, current_user, str(key), [str(p) for p in platforms])
        except ValueError:
            continue  # already decided, or no longer in the queue
        confirmed += 1
        created += result["created"]

    if confirmed and psn.review_pending_count(db, current_user.id) == 0:
        integrations.kick_psn_enrichment(current_user)

    entries = f"{created} library entr{'ies' if created != 1 else 'y'}"
    body = f"Confirmed {confirmed} game{'s' if confirmed != 1 else ''} — added {entries}."
    return templates.TemplateResponse(
        request=request,
        name="partials/_toast.html",
        context={
            "kind": "success" if confirmed else "error",
            "body": body if confirmed else "Nothing was confirmed — those rows are no longer pending.",
        },
    )


@router.post("/tools/psn-review/bulk-dismiss")
async def psn_review_bulk_dismiss(
    request: Request,
    keys: str = Form(""),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Dismiss a batch of rows — "own it on nothing", creating nothing."""
    from . import integrations, psn

    key_list = [k for k in keys.split(",") if k.strip()]
    if not key_list:
        return Response("No rows selected.", status_code=422)
    dismissed = 0
    for key in key_list:
        try:
            psn.dismiss_entry_decision(db, current_user, key.strip())
        except ValueError:
            continue
        dismissed += 1
    if dismissed and psn.review_pending_count(db, current_user.id) == 0:
        integrations.kick_psn_enrichment(current_user)
    return templates.TemplateResponse(
        request=request,
        name="partials/_toast.html",
        context={"kind": "success" if dismissed else "error", "body": f"Dismissed {dismissed} game{'s' if dismissed != 1 else ''}."},
    )


def _review_chrome_ctx(db: Session, user: models.User, kind: str) -> dict:
    """Counts + kind for the out-of-band chrome on a row-action response.

    Without it the tab badges and the pending count only refresh on a full
    swap, so deciding rows left both showing the number you started with.
    """
    from . import psn

    return {
        "oob": True,
        "kind": kind,
        "counts": {
            "cross_play": len(psn.import_review_rows(db, user.id)),
            "played_only": len([r for r in psn.played_only_rows(db, user.id) if not r["decision"]]),
        },
    }


def _played_only_done(request: Request, db: Session, user: models.User, key: str, name: str, verb: str):
    """Row replacement after a played-only action — same contract as the
    cross-play confirm: the click IS the action, the response retires the row."""
    return templates.TemplateResponse(
        request=request,
        name="partials/_psn_review_done.html",
        context={"key": key, "name": name, "detail": verb, **_review_chrome_ctx(db, user, "played_only")},
        headers={"HX-Retarget": f"#psn-row-{key}", "HX-Reswap": "outerHTML"},
    )


@router.post("/tools/psn-review/{key}/played-only/import")
async def psn_played_only_import(
    key: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Import one played-only row as its own library entry."""
    from . import psn

    try:
        name = psn.import_played_only(db, current_user, key)
    except ValueError:
        return Response("That row is no longer in the PSN review queue.", status_code=404)
    _maybe_enrich(db, current_user)
    return _played_only_done(request, db, current_user, key, name, "added to your library")


@router.post("/tools/psn-review/{key}/played-only/skip")
async def psn_played_only_skip(
    key: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Skip one played-only row so it stops asking."""
    from . import psn

    rows = {r["external_id"]: r["name"] for r in psn.played_only_rows(db, current_user.id)}
    try:
        psn.skip_played_only(db, current_user, key)
    except ValueError:
        return Response("That row is no longer in the PSN review queue.", status_code=404)
    _maybe_enrich(db, current_user)
    return _played_only_done(request, db, current_user, key, rows.get(key, key), "skipped")


@router.post("/tools/psn-review/{key}/played-only/attach")
async def psn_played_only_attach(
    key: str,
    request: Request,
    entry_id: int = Form(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Attach a played-only row's play stats to an entry you already own.

    The DMC5-SE-on-disc case: the activity row and the owned game wear
    different Sony names, and the playtime exists nowhere else."""
    from . import psn

    try:
        name = psn.attach_played_only(db, current_user, key, entry_id)
    except ValueError:
        return Response("That row is no longer in the PSN review queue.", status_code=404)
    _maybe_enrich(db, current_user)
    return _played_only_done(request, db, current_user, key, name, "play stats attached")


@router.post("/tools/psn-review/{key}/confirm")
async def psn_review_confirm(
    key: str,
    request: Request,
    platforms: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Confirm one row: create the entries for the ticked platforms and retire
    the row. Mirrors import review's per-candidate confirm — the click IS the
    action, and the response is the row's replacement."""
    from . import psn

    try:
        result = psn.confirm_entry_decision(db, current_user, key, [p.upper() for p in platforms])
    except ValueError:
        # Fixed text, not the exception's: a 404 here only ever means the row
        # isn't in the queue (stale page, already actioned), and echoing an
        # internal message back to the client is what CodeQL flags.
        return Response("That row is no longer in the PSN review queue.", status_code=404)
    _maybe_enrich(db, current_user)
    return templates.TemplateResponse(
        request=request,
        name="partials/_psn_review_done.html",
        context={"key": key, "name": result["name"], "created": result["created"], **_review_chrome_ctx(db, current_user, "cross_play")},
        headers={"HX-Retarget": f"#psn-row-{key}", "HX-Reswap": "outerHTML"},
    )


@router.post("/tools/psn-review/{key}/dismiss")
async def psn_review_dismiss(
    key: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Dismiss one row — records "own it on nothing" so it stops asking."""
    from . import psn

    try:
        result = psn.dismiss_entry_decision(db, current_user, key)
    except ValueError:
        return Response("That row is no longer in the PSN review queue.", status_code=404)
    # A dismiss creates nothing itself, but it can be the click that empties the
    # queue — and earlier confirms in the same session did create entries.
    _maybe_enrich(db, current_user)
    return templates.TemplateResponse(
        request=request,
        name="partials/_psn_review_done.html",
        context={"key": key, "name": result["name"], "created": 0, **_review_chrome_ctx(db, current_user, "cross_play")},
        headers={"HX-Retarget": f"#psn-row-{key}", "HX-Reswap": "outerHTML"},
    )


@router.get("/tools/match-review")
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


@router.post("/tools/match-review/{candidate_id}/merge")
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


@router.post("/tools/match-review/{candidate_id}/skip")
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


@router.get("/tools/match-review/{candidate_id}/preview")
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


@router.post("/tools/match-review/merge-bulk")
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


@router.post("/tools/match-review/clear-dismissed")
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
