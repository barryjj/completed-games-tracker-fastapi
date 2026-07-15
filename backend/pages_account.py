"""Account & settings routes: the settings page and its platform-management
section, the account redirect, platform edit/alias CRUD, and the credential
changes (display name, username, password, delete account).

Moved verbatim out of `pages.py` — no behaviour changes.
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session, joinedload

from . import models, users
from .models import get_db
from .pages_common import _base_ctx, _get_all_platforms, get_web_user, templates

router = APIRouter()


def _annotate_platforms_in_library(
    db: Session,
    user: "models.User",
    platforms: list["models.Platform"],
) -> tuple[list["models.Platform"], bool]:
    """Set .in_library on each Platform and return (platforms, has_any).

    .in_library is True when the user has at least one GameRelease linked to
    that platform via platform_id.  has_any is True when any platform matched.
    """
    used_ids: set[int] = {
        row[0]
        for row in db.query(models.GameRelease.platform_id)
        .join(models.UserLibraryEntry)
        .filter(
            models.UserLibraryEntry.user_id == user.id,
            models.GameRelease.platform_id.isnot(None),
        )
        .distinct()
        .all()
    }
    for p in platforms:
        p.in_library = p.id in used_ids
    return platforms, bool(used_ids)


@router.get("/settings")
def settings_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Settings area with grouped left nav: Account (profile/security/appearance)
    + Configuration (platforms, integrations). Section switching is client-side
    via ?section=, same pattern the old account tabs used."""
    platforms = _get_all_platforms(db)
    platforms, has_library_platforms = _annotate_platforms_in_library(db, current_user, platforms)
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "current_user": current_user,
            "platforms": platforms,
            "ctp_accents": models.CTP_ACCENTS,
            "has_library_platforms": has_library_platforms,
            **_base_ctx(db, current_user),
        },
    )


# Section names of the settings page — also the complete set of legal
# ?section= values for the /account -> /settings redirect below.
_SETTINGS_SECTIONS = {"profile", "security", "appearance", "platforms", "integrations"}


@router.get("/account")
def account_page(request: Request, tab: str = ""):
    """Old account page — content moved to /settings. Tab names map 1:1 to
    settings sections, so deep links keep working. The tab value is checked
    against the known section names so request data never flows into the
    redirect target (CodeQL: untrusted URL redirection)."""
    target = f"/settings?section={tab}" if tab in _SETTINGS_SECTIONS else "/settings"
    return RedirectResponse(target, status_code=302)


@router.get("/account/platforms/{platform_id}/cancel")
def cancel_platform_edit(
    request: Request,
    platform_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Return the compact row, discarding any unsaved edits."""
    platform = (
        db.query(models.Platform)
        .options(joinedload(models.Platform.aliases), joinedload(models.Platform.family))
        .filter(models.Platform.id == platform_id)
        .first()
    )
    if not platform:
        return Response(status_code=404)
    in_library = (
        db.query(models.GameRelease)
        .join(models.UserLibraryEntry)
        .filter(
            models.UserLibraryEntry.user_id == current_user.id,
            models.GameRelease.platform_id == platform.id,
        )
        .limit(1)
        .first()
        is not None
    )
    platform.in_library = in_library
    return templates.TemplateResponse(
        request=request,
        name="partials/platform_row.html",
        context={"platform": platform, "ctp_accents": models.CTP_ACCENTS},
    )


@router.get("/account/platforms/{platform_id}/edit")
def edit_platform_row(
    request: Request,
    platform_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Return the expanded editing row for a platform."""
    platform = (
        db.query(models.Platform)
        .options(joinedload(models.Platform.aliases), joinedload(models.Platform.family))
        .filter(models.Platform.id == platform_id)
        .first()
    )
    if not platform:
        return Response(status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="partials/platform_row_edit.html",
        context={"platform": platform, "ctp_accents": models.CTP_ACCENTS},
    )


@router.post("/account/platforms/{platform_id}")
def update_platform(
    request: Request,
    platform_id: int,
    display_name: str = Form(""),
    color: str = Form(""),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Update a platform's display_name and/or color. Returns the updated row partial."""
    platform = (
        db.query(models.Platform)
        .options(joinedload(models.Platform.aliases), joinedload(models.Platform.family))
        .filter(models.Platform.id == platform_id)
        .first()
    )
    if not platform:
        return Response(status_code=404)
    display_name = display_name.strip()
    color = color.strip()
    platform.display_name = display_name if display_name else None
    if color in models.CTP_ACCENTS:
        platform.color = color
    elif color == "":
        platform.color = None
    db.commit()
    db.refresh(platform)
    in_library = (
        db.query(models.GameRelease)
        .join(models.UserLibraryEntry)
        .filter(
            models.UserLibraryEntry.user_id == current_user.id,
            models.GameRelease.platform_id == platform.id,
        )
        .limit(1)
        .first()
        is not None
    )
    platform.in_library = in_library
    return templates.TemplateResponse(
        request=request,
        name="partials/platform_row.html",
        context={
            "platform": platform,
            "ctp_accents": models.CTP_ACCENTS,
        },
    )


@router.post("/account/platforms/{platform_id}/aliases")
def add_platform_alias(
    request: Request,
    platform_id: int,
    alias: str = Form(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Add an alias to a platform. Returns the updated aliases partial."""
    platform = db.query(models.Platform).options(joinedload(models.Platform.aliases)).filter(models.Platform.id == platform_id).first()
    if not platform:
        return Response(status_code=404)
    alias = alias.strip()
    if alias:
        db.add(models.PlatformAlias(platform_id=platform_id, alias=alias))
        db.commit()
        db.expire(platform)
        db.refresh(platform)
    return templates.TemplateResponse(
        request=request,
        name="partials/platform_aliases.html",
        context={"platform": platform},
    )


@router.delete("/account/platforms/aliases/{alias_id}")
def delete_platform_alias(
    request: Request,
    alias_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    """Delete a platform alias. Returns the updated aliases partial."""
    alias = db.query(models.PlatformAlias).filter(models.PlatformAlias.id == alias_id).first()
    if not alias:
        return Response(status_code=404)
    platform_id = alias.platform_id
    db.delete(alias)
    db.commit()
    platform = db.query(models.Platform).options(joinedload(models.Platform.aliases)).filter(models.Platform.id == platform_id).first()
    return templates.TemplateResponse(
        request=request,
        name="partials/platform_aliases.html",
        context={"platform": platform},
    )


@router.post("/account/display-name")
def update_display_name(
    request: Request,
    name: str = Form(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    users.update_display_name(db, current_user, name)
    return templates.TemplateResponse(
        request=request,
        name="partials/account_flash.html",
        context={"message": "Display name updated."},
    )


@router.post("/account/username")
def update_username(
    request: Request,
    new_username: str = Form(...),
    current_password: str = Form(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    result = users.update_username(db, current_user, new_username, current_password)
    if result == "incorrect_password":
        return templates.TemplateResponse(
            request=request,
            name="partials/account_flash.html",
            context={"error": "Current password is incorrect."},
            status_code=422,
        )
    if result == "username_taken":
        return templates.TemplateResponse(
            request=request,
            name="partials/account_flash.html",
            context={"error": "That username is already taken."},
            status_code=422,
        )
    return templates.TemplateResponse(
        request=request,
        name="partials/account_flash.html",
        context={"message": "Username updated."},
    )


@router.post("/account/password")
def update_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    new_password_confirm: str = Form(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    if new_password != new_password_confirm:
        return templates.TemplateResponse(
            request=request,
            name="partials/account_flash.html",
            context={"error": "New passwords do not match."},
            status_code=422,
        )
    result = users.update_password(db, current_user, current_password, new_password)
    if result == "incorrect_password":
        return templates.TemplateResponse(
            request=request,
            name="partials/account_flash.html",
            context={"error": "Current password is incorrect."},
            status_code=422,
        )
    return templates.TemplateResponse(
        request=request,
        name="partials/account_flash.html",
        context={"message": "Password updated."},
    )


@router.post("/account/delete")
def delete_account(
    request: Request,
    confirm_username: str = Form(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    if confirm_username.strip() != current_user.username:
        return templates.TemplateResponse(
            request=request,
            name="partials/account_flash.html",
            context={"error": "Username did not match. Account not deleted.", "target": "flash-delete"},
            status_code=422,
        )
    db.delete(current_user)
    db.commit()
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("session")
    return response
