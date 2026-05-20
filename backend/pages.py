import datetime
import os

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload, contains_eager

from . import models, users
from .models import get_db

router = APIRouter()

TEMPLATES_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "frontend", "templates"))
templates = Jinja2Templates(directory=TEMPLATES_DIR)

PLATFORMS = ["Steam", "PS5", "PS4", "PS3", "Switch", "Xbox", "iOS", "Android", "Other"]

COLLECTION_KEYWORDS = [
    "collection", "anthology", "trilogy", "compilation",
    "complete edition", "complete pack", "bundle", "chronicles",
    "archives", "legacy", "origins",
]

def infer_is_collection(title: str) -> bool:
    """Auto-detect collections by title keyword — for import-time use only."""
    t = title.lower()
    return any(kw in t for kw in COLLECTION_KEYWORDS)


def get_web_user(request: Request, db: Session = Depends(get_db)) -> models.User:
    """Dependency for page routes — raises RequiresLoginException instead of 401."""
    from .main import RequiresLoginException
    token = request.cookies.get("session")
    user = users.get_user_by_token(db, token) if token else None
    if not user:
        raise RequiresLoginException()
    return user


# --- Auth ---

@router.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html")


@router.get("/signup")
def signup_page(request: Request):
    return templates.TemplateResponse(request=request, name="signup.html")


@router.post("/signup")
def signup_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    if password != password_confirm:
        return templates.TemplateResponse(
            request=request,
            name="signup.html",
            context={"error": "Passwords do not match", "username": username},
            status_code=422,
        )
    if not users.username_available(db, username.strip()):
        return templates.TemplateResponse(
            request=request,
            name="signup.html",
            context={"error": "That username is already taken", "username": username},
            status_code=422,
        )
    u = users.signup_user(db, username.strip(), password)
    response = RedirectResponse("/library", status_code=302)
    response.set_cookie("session", u.api_token, httponly=True, samesite="lax")
    return response


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = users.authenticate(db, username, password)
    if not user:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Invalid username or password"},
            status_code=401,
        )
    response = RedirectResponse("/library", status_code=302)
    response.set_cookie("session", user.api_token, httponly=True, samesite="lax")
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("session")
    return response


# --- Account ---

@router.get("/account")
def account_page(
    request: Request,
    current_user: models.User = Depends(get_web_user),
):
    return templates.TemplateResponse(
        request=request,
        name="account.html",
        context={"current_user": current_user},
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


PAGE_SIZE = 250

# --- Library ---

@router.get("/library")
def library_page(
    request: Request,
    page: int = Query(1, ge=1),
    q: str = Query(""),
    platform: str = Query(""),
    show_dlc: str = Query(""),
    show_in_collection: str = Query(""),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    base_q = (
        db.query(models.UserLibraryEntry)
        .options(
            joinedload(models.UserLibraryEntry.release)
            .joinedload(models.GameRelease.game)
            .joinedload(models.Game.parent)
        )
        .filter(models.UserLibraryEntry.user_id == current_user.id)
        .join(models.GameRelease)
        .join(models.Game)
        .order_by(models.Game.title)
    )
    q = q.strip()
    if q:
        base_q = base_q.filter(
            or_(
                models.Game.title.ilike(f"%{q}%"),
                models.Game.display_name.ilike(f"%{q}%"),
            )
        )
    if platform:
        base_q = base_q.filter(models.GameRelease.platform == platform)

    # Default: hide children (DLC and games-within-collections).
    # Each checkbox independently re-admits its category.
    show_dlc_bool = show_dlc == "1"
    show_in_collection_bool = show_in_collection == "1"
    if not show_dlc_bool and not show_in_collection_bool:
        base_q = base_q.filter(models.Game.parent_id == None)
    elif show_dlc_bool and not show_in_collection_bool:
        base_q = base_q.filter(
            or_(models.Game.parent_id == None, models.Game.is_dlc == True)
        )
    elif not show_dlc_bool and show_in_collection_bool:
        base_q = base_q.filter(
            or_(models.Game.parent_id == None, models.Game.is_dlc == False)
        )
    # both checked → no additional filter, show everything
    total = base_q.count()
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, total_pages)
    entries = base_q.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()

    # All non-DLC entries — used for "base game" parent dropdowns (add + edit)
    base_game_options = (
        db.query(models.UserLibraryEntry)
        .options(joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game))
        .join(models.GameRelease)
        .join(models.Game)
        .filter(
            models.UserLibraryEntry.user_id == current_user.id,
            models.Game.is_dlc == False,
        )
        .order_by(models.Game.title)
        .all()
    )

    # Collections for the "part of collection" dropdown — needs all, not just current page
    collections = (
        db.query(models.UserLibraryEntry)
        .options(joinedload(models.UserLibraryEntry.release).joinedload(models.GameRelease.game))
        .join(models.GameRelease)
        .join(models.Game)
        .filter(
            models.UserLibraryEntry.user_id == current_user.id,
            models.Game.is_collection == True,
        )
        .order_by(models.Game.title)
        .all()
    )
    lib_platforms = (
        db.query(models.GameRelease.platform)
        .join(models.UserLibraryEntry)
        .filter(models.UserLibraryEntry.user_id == current_user.id)
        .distinct()
        .order_by(models.GameRelease.platform)
        .all()
    )
    lib_platform_list = [p[0] for p in lib_platforms]

    # Build filter_qs for pagination links (preserves active filters)
    filter_parts = []
    if q:
        filter_parts.append(f"q={q}")
    if platform:
        filter_parts.append(f"platform={platform}")
    if show_dlc_bool:
        filter_parts.append("show_dlc=1")
    if show_in_collection_bool:
        filter_parts.append("show_in_collection=1")
    filter_qs = ("&" + "&".join(filter_parts)) if filter_parts else ""

    return templates.TemplateResponse(
        request=request,
        name="library.html",
        context={
            "current_user": current_user,
            "entries": entries,
            "collections": collections,
            "base_game_options": base_game_options,
            "platforms": PLATFORMS,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "q": q,
            "platform": platform,
            "show_dlc": show_dlc_bool,
            "show_in_collection": show_in_collection_bool,
            "filter_qs": filter_qs,
            "lib_platforms": lib_platform_list,
        },
    )


@router.post("/library/games")
def add_game(
    request: Request,
    title: str = Form(...),
    platform: str = Form(...),
    is_dlc: bool = Form(False),
    is_collection: bool = Form(False),
    parent_game_id: int | None = Form(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    # Resolve parent release_id → game_id
    parent_id: int | None = None
    if parent_game_id:
        parent_release = db.query(models.GameRelease).filter(
            models.GameRelease.id == parent_game_id
        ).first()
        if parent_release:
            parent_id = parent_release.game_id

    game = models.Game(
        title=title.strip(),
        is_dlc=is_dlc,
        is_collection=is_collection,
        parent_id=parent_id,
    )
    db.add(game)
    db.flush()

    release = models.GameRelease(game_id=game.id, platform=platform, source="manual")
    db.add(release)
    db.flush()

    entry = models.UserLibraryEntry(
        user_id=current_user.id,
        release_id=release.id,
        import_source="manual",
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)

    return templates.TemplateResponse(
        request=request,
        name="partials/library_row.html",
        context={"entry": entry},
    )


@router.patch("/library/entries/{entry_id}")
def edit_library_entry(
    request: Request,
    entry_id: int,
    display_name: str = Form(""),
    is_dlc: bool = Form(False),
    is_collection: bool = Form(False),
    parent_game_id: int | None = Form(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    entry = (
        db.query(models.UserLibraryEntry)
        .filter_by(id=entry_id, user_id=current_user.id)
        .first()
    )
    if not entry:
        return Response(status_code=404)

    game = entry.release.game

    # display_name: empty string means "use raw title"
    game.display_name = display_name.strip() or None
    game.is_dlc = is_dlc
    game.is_collection = is_collection

    # Resolve parent_game_id (release id) → game.parent_id
    if parent_game_id:
        parent_release = db.query(models.GameRelease).filter_by(id=parent_game_id).first()
        game.parent_id = parent_release.game_id if parent_release else None
    else:
        game.parent_id = None

    db.commit()
    db.refresh(entry)
    return templates.TemplateResponse(
        request=request,
        name="partials/library_row.html",
        context={"entry": entry},
    )


@router.delete("/library/entries/{entry_id}")
def delete_library_entry(
    entry_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    entry = (
        db.query(models.UserLibraryEntry)
        .filter_by(id=entry_id, user_id=current_user.id)
        .first()
    )
    if entry:
        db.delete(entry)
        db.commit()
    return Response(status_code=200)


# --- Completions ---

@router.get("/completions")
def completions_page(
    request: Request,
    q: str = Query(""),
    platform: str = Query(""),
    completed_from: str = Query(""),
    completed_to: str = Query(""),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    completions_q = (
        db.query(models.Completion)
        .join(models.Completion.library_entry)
        .join(models.UserLibraryEntry.release)
        .join(models.GameRelease.game)
        .options(
            contains_eager(models.Completion.library_entry)
            .contains_eager(models.UserLibraryEntry.release)
            .contains_eager(models.GameRelease.game)
        )
        .filter(models.Completion.user_id == current_user.id)
    )
    q = q.strip()
    if q:
        completions_q = completions_q.filter(
            or_(
                models.Game.title.ilike(f"%{q}%"),
                models.Game.display_name.ilike(f"%{q}%"),
            )
        )
    if platform:
        completions_q = completions_q.filter(models.GameRelease.platform == platform)
    if completed_from:
        try:
            completions_q = completions_q.filter(
                models.Completion.completed_at >= datetime.date.fromisoformat(completed_from)
            )
        except ValueError:
            pass
    if completed_to:
        try:
            completions_q = completions_q.filter(
                models.Completion.completed_at <= datetime.date.fromisoformat(completed_to)
            )
        except ValueError:
            pass
    completions = completions_q.order_by(models.Completion.id.desc()).all()
    comp_platforms = (
        db.query(models.GameRelease.platform)
        .join(models.UserLibraryEntry, models.GameRelease.id == models.UserLibraryEntry.release_id)
        .join(models.Completion, models.UserLibraryEntry.id == models.Completion.library_entry_id)
        .filter(models.Completion.user_id == current_user.id)
        .distinct()
        .order_by(models.GameRelease.platform)
        .all()
    )
    comp_platform_list = [p[0] for p in comp_platforms]
    return templates.TemplateResponse(
        request=request,
        name="completions.html",
        context={
            "current_user": current_user,
            "completions": completions,
            "today": datetime.date.today().isoformat(),
            "q": q,
            "platform": platform,
            "completed_from": completed_from,
            "completed_to": completed_to,
            "comp_platforms": comp_platform_list,
        },
    )


@router.get("/completions/games/search")
def search_completion_games(
    request: Request,
    q: str = Query("", min_length=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    q = q.strip()
    if not q:
        return templates.TemplateResponse(
            request=request,
            name="partials/completion_game_results.html",
            context={"results": []},
        )
    results = (
        db.query(models.UserLibraryEntry)
        .join(models.UserLibraryEntry.release)
        .join(models.GameRelease.game)
        .options(
            contains_eager(models.UserLibraryEntry.release)
            .contains_eager(models.GameRelease.game)
        )
        .filter(
            models.UserLibraryEntry.user_id == current_user.id,
            or_(
                models.Game.title.ilike(f"{q}%"),
                models.Game.title.ilike(f"% {q}%"),
                models.Game.display_name.ilike(f"{q}%"),
                models.Game.display_name.ilike(f"% {q}%"),
            ),
        )
        .order_by(models.Game.title)
        .limit(40)
        .all()
    )
    seen: set[tuple] = set()
    unique: list[models.UserLibraryEntry] = []
    for entry in results:
        key = (entry.release.game_id, entry.release.platform)
        if key not in seen:
            seen.add(key)
            unique.append(entry)
    return templates.TemplateResponse(
        request=request,
        name="partials/completion_game_results.html",
        context={"results": unique[:20]},
    )


@router.post("/completions/log")
def log_completion(
    request: Request,
    library_entry_id: int = Form(...),
    completed_at: str = Form(...),
    playthroughs: str = Form("1"),
    notes: str = Form(""),
    completion_id: int | None = Form(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    if completion_id:
        completion = db.query(models.Completion).filter(
            models.Completion.id == completion_id,
            models.Completion.user_id == current_user.id,
        ).first()
        if not completion:
            return templates.TemplateResponse(
                request=request,
                name="partials/completion_row.html",
                context={},
                status_code=404,
            )
        completion.library_entry_id = library_entry_id
        completion.completed_at = datetime.date.fromisoformat(completed_at)
        completion.playthroughs = playthroughs.strip() or None
        completion.notes = notes.strip() or None
        db.commit()
        db.refresh(completion)
        response = templates.TemplateResponse(
            request=request,
            name="partials/completion_row.html",
            context={"completion": completion},
        )
        response.headers["HX-Retarget"] = f"#completion-{completion.id}"
        response.headers["HX-Reswap"] = "outerHTML"
        return response

    completion = models.Completion(
        user_id=current_user.id,
        library_entry_id=library_entry_id,
        completed_at=datetime.date.fromisoformat(completed_at),
        playthroughs=playthroughs.strip() or None,
        notes=notes.strip() or None,
    )
    db.add(completion)
    db.commit()
    db.refresh(completion)

    return templates.TemplateResponse(
        request=request,
        name="partials/completion_row.html",
        context={"completion": completion},
    )


@router.delete("/completions/{completion_id}")
def delete_completion(
    completion_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    completion = db.query(models.Completion).filter(
        models.Completion.id == completion_id,
        models.Completion.user_id == current_user.id,
    ).first()
    if completion:
        db.delete(completion)
        db.commit()
    return Response(status_code=200)
