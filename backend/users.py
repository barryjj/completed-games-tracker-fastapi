from pydantic import BaseModel, ConfigDict
from fastapi import HTTPException
from sqlalchemy.orm import Session
import datetime
from typing import List

from . import models


class UserCreate(BaseModel):
    name: str


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    created_at: datetime.datetime


class UserUpdate(BaseModel):
    name: str | None = None


def create_user(db: Session, user: UserCreate) -> models.User:
    db_user = models.User(name=user.name)
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


def get_user(db: Session, id: int) -> models.User:
    u = db.query(models.User).filter(models.User.id == id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    return u


def list_users(db: Session, limit: int = 100) -> List[models.User]:
    return db.query(models.User).limit(limit).all()


def update_user(db: Session, id: int, user: UserUpdate) -> models.User:
    u = db.query(models.User).filter(models.User.id == id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    if user.name is not None:
        u.name = user.name
    db.commit()
    db.refresh(u)
    return u


def delete_user(db: Session, id: int) -> None:
    u = db.query(models.User).filter(models.User.id == id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    db.delete(u)
    db.commit()
