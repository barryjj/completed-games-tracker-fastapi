
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
import datetime
from sqlalchemy.orm import Session
from typing import Iterator
import os

from . import models


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize DB tables on startup
    models.init_db()
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


class UserCreate(BaseModel):
    name: str


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    created_at: datetime.datetime


class UserUpdate(BaseModel):
    name: str | None = None


def get_db() -> Iterator[Session]:
    db = models.SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.post("/users", response_model=UserResponse)
def create_user(user: UserCreate, db: Session = Depends(get_db)) -> UserResponse:
    db_user = models.User(name=user.name)
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return UserResponse.model_validate(db_user, from_attributes=True)


@app.get("/users/{id}", response_model=UserResponse)
def get_user(id: int, db: Session = Depends(get_db)) -> UserResponse:
    u = db.query(models.User).filter(models.User.id == id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    return UserResponse.model_validate(u, from_attributes=True)


@app.patch("/users/{id}", response_model=UserResponse)
def update_user(id: int, user: UserUpdate, db: Session = Depends(get_db)) -> UserResponse:
    u = db.query(models.User).filter(models.User.id == id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    if user.name is not None:
        u.name = user.name
    db.commit()
    db.refresh(u)
    return UserResponse.model_validate(u, from_attributes=True)


@app.delete("/users/{id}", status_code=204)
def delete_user(id: int, db: Session = Depends(get_db)) -> Response:
    u = db.query(models.User).filter(models.User.id == id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    db.delete(u)
    db.commit()
    return Response(status_code=204)


@app.get("/", response_class=FileResponse)
def index():
    index_path = os.path.join(os.path.dirname(__file__), "..", "frontend", "index.html")
    index_path = os.path.normpath(index_path)
    return FileResponse(index_path)
