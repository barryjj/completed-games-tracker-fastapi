import asyncio
import logging
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
from . import worker_state
from .models import get_db, SessionLocal
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

_worker_logger = logging.getLogger("steam.enrichment")


async def _enrichment_worker():
    """
    Ambient background task: quietly enriches Steam appdetails metadata for any
    entries that haven't been processed yet. Runs for the lifetime of the server.
    - Processes 5 entries per cycle at 0.3s each (~1.5s of work per cycle)
    - Sleeps 2s between cycles when there's a backlog, 5min when caught up
    - Pauses automatically while a library sync is running
    - Naturally resumable: metadata_fetched_at tracks what's done
    """
    await asyncio.sleep(15)  # let the app finish starting up first
    while True:
        try:
            if worker_state.enrichment_paused:
                await asyncio.sleep(1)
                continue
            db = SessionLocal()
            try:
                from . import steam
                pending = await asyncio.to_thread(steam.enrich_next_batch, db)
                if pending == 0:
                    await asyncio.sleep(300)  # fully caught up, check again in 5 min
                else:
                    _worker_logger.debug("Enrichment: %d entries remaining", pending)
                    await asyncio.sleep(2)
            finally:
                db.close()
        except Exception as e:
            _worker_logger.warning("Enrichment worker error: %s", e)
            await asyncio.sleep(30)

security = HTTPBearer()

TEMPLATES_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "frontend", "templates"))
STATIC_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "frontend", "static"))
templates = Jinja2Templates(directory=TEMPLATES_DIR)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.environ.get("TESTING"):
        try:
            models.engine.dispose()
            base_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
            alembic_cfg = Config()
            alembic_cfg.set_main_option("script_location", os.path.join(base_dir, "alembic"))
            alembic_cfg.set_main_option("sqlalchemy.url", models.DB_URL)
            command.upgrade(alembic_cfg, "head")
        except Exception as e:
            logging.getLogger(__name__).warning("Alembic migration failed: %s", e)
        asyncio.create_task(_enrichment_worker())
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
from . import pages, integrations  # noqa: E402
app.include_router(pages.router)
app.include_router(integrations.router)


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
