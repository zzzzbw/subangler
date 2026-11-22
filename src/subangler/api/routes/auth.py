"""鉴权路由：登录、登出、当前用户、修改密码。"""

from __future__ import annotations

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from ...auth import COOKIE_NAME, AuthError, get_auth_manager, require_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class UserInfo(BaseModel):
    id: int
    username: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8, max_length=256)


@router.post("/login")
def login(payload: LoginRequest, response: Response) -> UserInfo:
    manager = get_auth_manager()
    token = manager.login(payload.username, payload.password)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=int(manager.session_hours * 3600),
        path="/",
    )
    return UserInfo(id=0, username=payload.username)


@router.post("/logout")
def logout(response: Response, session_token: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, str]:
    if session_token:
        get_auth_manager().logout(session_token)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"status": "ok"}


@router.get("/me")
def me(user: dict = Depends(require_user)) -> UserInfo:
    return UserInfo(id=user["id"], username=user["username"])


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    user: dict = Depends(require_user),
) -> dict[str, str]:
    ok = get_auth_manager().change_password(user["username"], payload.old_password, payload.new_password)
    if not ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="原密码错误")
    return {"status": "ok"}
