from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from alembic.config import Config
from alembic import command
from sqlalchemy.orm import Session
import os

from . import models
from . import users
from .models import get_db
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

security = HTTPBearer()

TEMPLATES_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "frontend", "templates"))
STATIC_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "frontend", "static"))
templates = Jinja2Templates(directory=TEMPLATES_DIR)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.environ.get("TESTING"):
        alembic_cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
        command.upgrade(alembic_cfg, "head")
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# Redirect unauthenticated web visitors to login
class RequiresLoginException(Exception):
    pass


@app.exception_handler(RequiresLoginException)
async def requires_login_handler(request: Request, exc: RequiresLoginException):
    return RedirectResponse("/login", status_code=302)


# Import and register the pages router after app is created to avoid circular imports
from . import pages  # noqa: E402
app.include_router(pages.router)


@app.get("/health")
def health():
    return {"status": "ok"}


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security), db: Session = Depends(get_db)) -> models.User:
    token = credentials.credentials
    user = users.get_user_by_token(db, token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid authentication credentials")
    return user


@app.post("/signup", response_model=users.UserResponse)
def signup(auth: users.AuthRequest, db: Session = Depends(get_db)) -> users.UserResponse:
    existing = db.query(models.User).filter(models.User.username == auth.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="username already exists")
    u = users.signup_user(db, auth.username, auth.password)
    return users.UserResponse.model_validate(u, from_attributes=True)


@app.post("/signin", response_model=users.AuthResponse)
def signin(auth: users.AuthRequest, db: Session = Depends(get_db)) -> users.AuthResponse:
    u = users.authenticate(db, auth.username, auth.password)
    if not u:
        raise HTTPException(status_code=401, detail="invalid username or password")
    return users.AuthResponse(token=u.api_token)


@app.get("/me", response_model=users.UserResponse)
def me(current_user: models.User = Depends(get_current_user)):
    return users.UserResponse.model_validate(current_user, from_attributes=True)


@app.get("/users/{id}", response_model=users.UserResponse)
def get_user(id: int, db: Session = Depends(get_db)) -> users.UserResponse:
    u = users.get_user(db, id)
    return users.UserResponse.model_validate(u, from_attributes=True)


@app.get("/")
def index():
    return RedirectResponse("/library", status_code=302)
