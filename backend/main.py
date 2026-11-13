import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from uuid import UUID

import asyncpg
import bcrypt
import jwt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field

load_dotenv()

pool: Optional[asyncpg.Pool] = None
security = HTTPBearer()

JWT_SECRET = os.getenv("JWT_SECRET", "").strip()
JWT_ALG = "HS256"
JWT_EXPIRE_HOURS = 72
_DSN_SSL_KEYS = {"ssl", "sslmode"}
_USERNAME_RE = re.compile(r"[^a-z0-9_]+")


def parse_database_url() -> tuple[str, bool | None]:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is required")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]

    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    sslmode = (query.get("sslmode") or query.get("ssl") or "").lower()

    ssl: bool | None = None
    if sslmode in {"disable", "false", "0"}:
        ssl = False
    elif sslmode in {"require", "verify-ca", "verify-full", "true", "1"}:
        ssl = True

    for key in list(query):
        if key.lower() in _DSN_SSL_KEYS:
            del query[key]

    dsn = urlunparse(parsed._replace(query=urlencode(query)))
    return dsn, ssl


def username_from_email(email: str) -> str:
    local = email.split("@", 1)[0].lower()
    cleaned = _USERNAME_RE.sub("", local).strip("_")
    return cleaned or "user"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_token(user_id: UUID) -> str:
    payload = {
        "sub": str(user_id),
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global pool
    if not JWT_SECRET:
        raise RuntimeError("JWT_SECRET is required")
    dsn, ssl = parse_database_url()
    pool_kwargs: dict = {"dsn": dsn, "min_size": 1, "max_size": 10}
    if ssl is not None:
        pool_kwargs["ssl"] = ssl
    pool = await asyncpg.create_pool(**pool_kwargs)
    try:
        yield
    finally:
        await pool.close()
        pool = None


app = FastAPI(title="sbp-donation-api", lifespan=lifespan)
_cors_origins = [item.strip() for item in os.getenv("CORS_ORIGINS", "*").split(",") if item.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AuthRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)


class AuthResponse(BaseModel):
    token: str
    user_id: UUID
    email: str
    username: str


class UserOut(BaseModel):
    id: UUID
    email: str
    email_verified: bool
    username: Optional[str] = None
    created_at: datetime


class ProfileOut(BaseModel):
    id: UUID
    username: str
    display_name: Optional[str]
    description: Optional[str]
    sber_qr_url: Optional[str]
    avatar_url: Optional[str]
    created_at: datetime


class ProfileUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)


class DonationCreate(BaseModel):
    profile_id: UUID
    donor_name: str = Field(min_length=1, max_length=100)
    amount: Decimal = Field(gt=0)
    message: Optional[str] = Field(default=None, max_length=500)


class DonationOut(BaseModel):
    id: UUID
    profile_id: UUID
    donor_name: str
    amount: Decimal
    message: Optional[str]
    status: str
    created_at: datetime


def row_to_profile(row: asyncpg.Record) -> ProfileOut:
    return ProfileOut(
        id=row["id"],
        username=row["username"],
        display_name=row["display_name"],
        description=row["description"],
        sber_qr_url=row["sber_qr_url"],
        avatar_url=row["avatar_url"],
        created_at=row["created_at"],
    )


def row_to_donation(row: asyncpg.Record) -> DonationOut:
    return DonationOut(
        id=row["id"],
        profile_id=row["profile_id"],
        donor_name=row["donor_name"],
        amount=row["amount"],
        message=row["message"],
        status=row["status"],
        created_at=row["created_at"],
    )


async def current_user(
    creds: HTTPAuthorizationCredentials = Depends(security),
) -> asyncpg.Record:
    try:
        payload = jwt.decode(creds.credentials, JWT_SECRET, algorithms=[JWT_ALG])
        user_id = UUID(payload["sub"])
    except Exception as exc:
        raise HTTPException(status_code=401, detail="invalid token") from exc

    user = await pool.fetchrow(
        """
        SELECT u.id, u.email, u.email_verified, u.created_at, p.username
        FROM users u
        LEFT JOIN profiles p ON p.id = u.id
        WHERE u.id = $1
        """,
        user_id,
    )
    if not user:
        raise HTTPException(status_code=401, detail="user not found")
    return user


async def unique_username(conn: asyncpg.Connection, base: str) -> str:
    candidate = base
    suffix = 0
    while await conn.fetchval("SELECT 1 FROM profiles WHERE username = $1", candidate):
        suffix += 1
        candidate = f"{base}{suffix}"
    return candidate


@app.get("/health")
async def health():
    return {"status": "ok", "service": "sbp-donation-api"}


@app.get("/health/db")
async def health_db():
    try:
        value = await pool.fetchval("SELECT 1")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": "ok", "database": "connected", "check": value}


@app.post("/api/auth/register", status_code=201)
async def register(body: AuthRequest) -> AuthResponse:
    email = str(body.email).lower()
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchval("SELECT 1 FROM users WHERE email = $1", email)
            if existing:
                raise HTTPException(status_code=409, detail="email already registered")

            user = await conn.fetchrow(
                """
                INSERT INTO users (email, password_hash, email_verified)
                VALUES ($1, $2, TRUE)
                RETURNING id, email
                """,
                email,
                hash_password(body.password),
            )
            username = await unique_username(conn, username_from_email(email))
            await conn.execute(
                """
                INSERT INTO profiles (id, username, display_name)
                VALUES ($1, $2, $2)
                """,
                user["id"],
                username,
            )

    return AuthResponse(
        token=create_token(user["id"]),
        user_id=user["id"],
        email=user["email"],
        username=username,
    )


@app.post("/api/auth/login")
async def login(body: AuthRequest) -> AuthResponse:
    email = str(body.email).lower()
    user = await pool.fetchrow(
        """
        SELECT u.id, u.email, u.password_hash, p.username
        FROM users u
        LEFT JOIN profiles p ON p.id = u.id
        WHERE u.email = $1
        """,
        email,
    )
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="invalid email or password")
    if not user["username"]:
        raise HTTPException(status_code=409, detail="profile is missing")

    return AuthResponse(
        token=create_token(user["id"]),
        user_id=user["id"],
        email=user["email"],
        username=user["username"],
    )


@app.get("/api/auth/me")
async def me(user: asyncpg.Record = Depends(current_user)) -> UserOut:
    return UserOut(
        id=user["id"],
        email=user["email"],
        email_verified=user["email_verified"],
        username=user["username"],
        created_at=user["created_at"],
    )


@app.get("/api/profiles/me")
async def my_profile(user: asyncpg.Record = Depends(current_user)) -> ProfileOut:
    row = await pool.fetchrow("SELECT * FROM profiles WHERE id = $1", user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="profile not found")
    return row_to_profile(row)


@app.patch("/api/profiles/me")
async def update_my_profile(
    body: ProfileUpdate,
    user: asyncpg.Record = Depends(current_user),
) -> ProfileOut:
    row = await pool.fetchrow(
        """
        UPDATE profiles
        SET
            display_name = COALESCE($2, display_name),
            description = COALESCE($3, description),
            updated_at = NOW()
        WHERE id = $1
        RETURNING *
        """,
        user["id"],
        body.display_name,
        body.description,
    )
    if not row:
        raise HTTPException(status_code=404, detail="profile not found")
    return row_to_profile(row)


@app.get("/api/profiles/{username}")
async def public_profile(username: str) -> ProfileOut:
    row = await pool.fetchrow(
        "SELECT * FROM profiles WHERE username = $1",
        username.lower(),
    )
    if not row:
        raise HTTPException(status_code=404, detail="profile not found")
    return row_to_profile(row)


@app.post("/api/donations", status_code=201)
async def create_donation(body: DonationCreate) -> DonationOut:
    profile_exists = await pool.fetchval(
        "SELECT 1 FROM profiles WHERE id = $1",
        body.profile_id,
    )
    if not profile_exists:
        raise HTTPException(status_code=404, detail="profile not found")

    row = await pool.fetchrow(
        """
        INSERT INTO donations (profile_id, donor_name, amount, message, status)
        VALUES ($1, $2, $3, $4, 'pending')
        RETURNING id, profile_id, donor_name, amount, message, status, created_at
        """,
        body.profile_id,
        body.donor_name.strip(),
        body.amount,
        (body.message or "").strip() or None,
    )
    return row_to_donation(row)


@app.get("/api/donations")
async def list_donations(
    profile_id: UUID = Query(...),
    limit: int = Query(20, ge=1, le=100),
) -> list[DonationOut]:
    rows = await pool.fetch(
        """
        SELECT id, profile_id, donor_name, amount, message, status, created_at
        FROM donations
        WHERE profile_id = $1
        ORDER BY created_at DESC
        LIMIT $2
        """,
        profile_id,
        limit,
    )
    return [row_to_donation(row) for row in rows]
