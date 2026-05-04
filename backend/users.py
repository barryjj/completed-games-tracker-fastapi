from pydantic import BaseModel, ConfigDict
from fastapi import HTTPException
from sqlalchemy.orm import Session
import datetime
from typing import List

from . import models
import secrets
from passlib.hash import bcrypt
from pydantic import Field


class UserCreate(BaseModel):
    name: str
    username: str | None = None
    password: str | None = None


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    created_at: datetime.datetime


class UserUpdate(BaseModel):
    name: str | None = None


class AuthRequest(BaseModel):
    username: str
    password: str


class AuthResponse(BaseModel):
    token: str



def create_user(db: Session, user: UserCreate) -> models.User:
    db_user = models.User(name=user.name)
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


def signup_user(db: Session, username: str, password: str, name: str | None = None) -> models.User:
    # simple signup: hash password, generate api token
    pw_hash = bcrypt.hash(password)
    token = secrets.token_urlsafe(32)
    db_user = models.User(name=name or username, username=username, password_hash=pw_hash, api_token=token)
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


def authenticate(db: Session, username: str, password: str) -> models.User | None:
    u = db.query(models.User).filter(models.User.username == username).first()
    if not u or not u.password_hash:
        return None
    if not bcrypt.verify(password, u.password_hash):
        return None
    # ensure token exists
    if not u.api_token:
        u.api_token = secrets.token_urlsafe(32)
        db.commit()
        db.refresh(u)
    return u


def get_user_by_token(db: Session, token: str) -> models.User | None:
    if not token:
        return None
    return db.query(models.User).filter(models.User.api_token == token).first()


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
