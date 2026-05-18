import datetime
import os

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from . import models, users
from .models import get_db

router = APIRouter()

TEMPLATES_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "frontend", "templates"))
templates = Jinja2Templates(directory=TEMPLATES_DIR)

PLATFORMS = ["Steam", "PS5", "PS4", "PS3", "Switch", "Xbox", "iOS", "Android", "Other"]
GAME_TYPES = ["game", "dlc", "collection"]
GAME_TYPE_LABELS = {"game": "Game", "dlc": "DLC", "collection": "Collection"}


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


# --- Library ---

@router.get("/library")
def library_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    entries = (
        db.query(models.UserLibraryEntry)
        .filter(models.UserLibraryEntry.user_id == current_user.id)
        .join(models.GameRelease)
        .join(models.Game)
        .order_by(models.Game.title)
        .all()
    )
    return templates.TemplateResponse(
        request=request,
        name="library.html",
        context={
            "current_user": current_user,
            "entries": entries,
            "platforms": PLATFORMS,
            "game_types": GAME_TYPES,
            "game_type_labels": GAME_TYPE_LABELS,
        },
    )


@router.post("/library/games")
def add_game(
    request: Request,
    title: str = Form(...),
    platform: str = Form(...),
    game_type: str = Form("game"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    game = models.Game(title=title.strip(), game_type=game_type)
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
        context={"entry": entry, "game_type_labels": GAME_TYPE_LABELS},
    )


# --- Completions ---

@router.get("/completions")
def completions_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
    completions = (
        db.query(models.Completion)
        .filter(models.Completion.user_id == current_user.id)
        .join(models.UserLibraryEntry)
        .join(models.GameRelease)
        .join(models.Game)
        .order_by(models.Completion.completed_at.desc())
        .all()
    )
    library_entries = (
        db.query(models.UserLibraryEntry)
        .filter(models.UserLibraryEntry.user_id == current_user.id)
        .join(models.GameRelease)
        .join(models.Game)
        .order_by(models.Game.title)
        .all()
    )
    return templates.TemplateResponse(
        request=request,
        name="completions.html",
        context={
            "current_user": current_user,
            "completions": completions,
            "library_entries": library_entries,
            "today": datetime.date.today().isoformat(),
        },
    )


@router.post("/completions/log")
def log_completion(
    request: Request,
    library_entry_id: int = Form(...),
    completed_at: str = Form(...),
    playthroughs: str = Form("1"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_web_user),
):
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
