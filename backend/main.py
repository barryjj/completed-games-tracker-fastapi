
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Response
from fastapi.responses import FileResponse
 
from sqlalchemy.orm import Session
from typing import Iterator
import os

from . import models
from . import users


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize DB tables on startup
    models.init_db()
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


def get_db() -> Iterator[Session]:
    db = models.SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.post("/users", response_model=users.UserResponse)
def create_user(user: users.UserCreate, db: Session = Depends(get_db)) -> users.UserResponse:
    db_user = users.create_user(db, user)
    return users.UserResponse.model_validate(db_user, from_attributes=True)


@app.get("/users/{id}", response_model=users.UserResponse)
def get_user(id: int, db: Session = Depends(get_db)) -> users.UserResponse:
    u = users.get_user(db, id)
    return users.UserResponse.model_validate(u, from_attributes=True)


@app.get("/users", response_model=list[users.UserResponse])
def list_users(limit: int = 100, db: Session = Depends(get_db)) -> list[users.UserResponse]:
    us = users.list_users(db, limit=limit)
    return [users.UserResponse.model_validate(u, from_attributes=True) for u in us]


@app.patch("/users/{id}", response_model=users.UserResponse)
def update_user(id: int, user: users.UserUpdate, db: Session = Depends(get_db)) -> users.UserResponse:
    u = users.update_user(db, id, user)
    return users.UserResponse.model_validate(u, from_attributes=True)


@app.delete("/users/{id}", status_code=204)
def delete_user(id: int, db: Session = Depends(get_db)) -> Response:
    users.delete_user(db, id)
    return Response(status_code=204)


@app.get("/", response_class=FileResponse)
def index():
    index_path = os.path.join(os.path.dirname(__file__), "..", "frontend", "index.html")
    index_path = os.path.normpath(index_path)
    return FileResponse(index_path)
