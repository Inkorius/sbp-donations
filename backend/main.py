import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from uuid import UUID, uuid4

import asyncpg
import jwt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field

load_dotenv()
logging.basicConfig(level=logging.INFO)

pool: Optional[asyncpg.Pool] = None
security = HTTPBearer()


class DonationHub:
    def __init__(self):
        self.clients: dict[str, set[WebSocket]] = {}

    async def connect(self, profile_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self.clients.setdefault(profile_id, set()).add(websocket)

    def disconnect(self, profile_id: str, websocket: WebSocket) -> None:
        group = self.clients.get(profile_id)
        if not group:
            return
        group.discard(websocket)
        if not group:
            self.clients.pop(profile_id, None)

    async def broadcast(self, profile_id: str, payload: dict) -> None:
        group = list(self.clients.get(profile_id, ()))
        for websocket in group:
            try:
                await websocket.send_json(payload)
            except Exception:
                self.disconnect(profile_id, websocket)


hub = DonationHub()

JWT_SECRET = os.getenv("JWT_SECRET", "").strip()
JWT_ALG = "HS256"
JWT_EXPIRE_HOURS = 72
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/app/uploads"))
STATIC_DIR = Path(os.getenv("STATIC_DIR", str(Path(__file__).resolve().parent / "static")))
_DSN_SSL_KEYS = {"ssl", "sslmode"}
_USERNAME_RE = re.compile(r"[^a-z0-9_]+")

TEXTS = {
    "ru": {
        "email_taken": "Этот email уже зарегистрирован",
        "bad_login": "Неверный email или пароль",
        "no_profile": "Профиль не найден",
        "bad_token": "Сессия недействительна. Войдите снова",
        "validation": "Проверьте введённые данные",
        "db_error": "Ошибка базы данных",
        "server_error": "Внутренняя ошибка сервера",
        "qr_missing": "Выберите файл с QR-кодом",
        "qr_type": "Нужно изображение JPG, PNG или WebP",
        "qr_size": "Файл слишком большой. Максимум 2 МБ",
        "bad_color": "Цвет кружка должен быть в формате #RRGGBB",
        "confirm_own": "Можно менять только свои донаты",
    },
    "en": {
        "email_taken": "This email is already registered",
        "bad_login": "Invalid email or password",
        "no_profile": "Profile not found",
        "bad_token": "Session expired. Please sign in again",
        "validation": "Please check the submitted data",
        "db_error": "Database error",
        "server_error": "Internal server error",
        "qr_missing": "Choose a QR code image",
        "qr_type": "Use a JPG, PNG, or WebP image",
        "qr_size": "File is too large. Maximum size is 2 MB",
        "bad_color": "Circle color must be in #RRGGBB format",
        "confirm_own": "You can only change your own donations",
    },
}


def get_lang(request: Request) -> str:
    cookie = request.cookies.get("lang")
    if cookie in TEXTS:
        return cookie
    header = (request.headers.get("accept-language") or "ru").lower()
    return "en" if header.startswith("en") else "ru"


def msg(request: Request, key: str) -> str:
    return TEXTS[get_lang(request)][key]


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

    return urlunparse(parsed._replace(query=urlencode(query))), ssl


def username_from_email(email: str) -> str:
    local = email.split("@", 1)[0].lower()
    cleaned = _USERNAME_RE.sub("", local).strip("_")
    return cleaned or "user"


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"pbkdf2_sha256$120000${salt}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algo, rounds, salt, digest = password_hash.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        check = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            int(rounds),
        )
        return hmac.compare_digest(check.hex(), digest)
    except (TypeError, ValueError):
        return False


def create_token(user_id: UUID) -> str:
    payload = {
        "sub": str(user_id),
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


SCHEMA_STATEMENTS = [
    "CREATE EXTENSION IF NOT EXISTS pgcrypto",
    """
    CREATE TABLE IF NOT EXISTS users (
      id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      email           TEXT UNIQUE NOT NULL,
      password_hash   TEXT NOT NULL,
      email_verified  BOOLEAN NOT NULL DEFAULT FALSE,
      created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS email_verification_tokens (
      id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      token_hash TEXT NOT NULL,
      expires_at TIMESTAMPTZ NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    # Existing profiles from the old app are kept. IF NOT EXISTS will not replace them.
    """
    CREATE TABLE IF NOT EXISTS profiles (
      id           UUID PRIMARY KEY,
      username     TEXT UNIQUE NOT NULL,
      display_name TEXT,
      description  TEXT,
      sber_qr_url  TEXT,
      avatar_url   TEXT,
      created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS display_name TEXT",
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS description TEXT",
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS sber_qr_url TEXT",
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS avatar_url TEXT",
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS avatar_color TEXT",
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    """
    DO $$
    DECLARE r record;
    BEGIN
      FOR r IN
        SELECT c.conname
        FROM pg_constraint c
        JOIN pg_class t ON c.conrelid = t.oid
        WHERE t.relname = 'profiles' AND c.contype = 'f'
      LOOP
        EXECUTE format('ALTER TABLE profiles DROP CONSTRAINT IF EXISTS %I', r.conname);
      END LOOP;
    END $$
    """,
    """
    CREATE TABLE IF NOT EXISTS donations (
      id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      profile_id     UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
      donor_name     TEXT NOT NULL,
      amount         NUMERIC(12, 2) NOT NULL CHECK (amount >= 0),
      message        TEXT,
      status         TEXT NOT NULL DEFAULT 'pending',
      payment_ref    TEXT,
      payment_source TEXT NOT NULL DEFAULT 'manual',
      matched_at     TIMESTAMPTZ,
      confirmed_at   TIMESTAMPTZ,
      created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "ALTER TABLE donations ADD COLUMN IF NOT EXISTS payment_ref TEXT",
    "ALTER TABLE donations ADD COLUMN IF NOT EXISTS payment_source TEXT NOT NULL DEFAULT 'manual'",
    "ALTER TABLE donations ADD COLUMN IF NOT EXISTS matched_at TIMESTAMPTZ",
    "ALTER TABLE donations ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMPTZ",
    """
    CREATE INDEX IF NOT EXISTS idx_donations_profile_created
      ON donations (profile_id, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_donations_profile_status
      ON donations (profile_id, status)
    """,
    """
    CREATE OR REPLACE FUNCTION notify_donation_change()
    RETURNS TRIGGER AS $$
    BEGIN
      PERFORM pg_notify(
        'donation_events',
        json_build_object(
          'op', TG_OP,
          'id', NEW.id,
          'profile_id', NEW.profile_id,
          'status', NEW.status,
          'donor_name', NEW.donor_name,
          'amount', NEW.amount,
          'message', NEW.message,
          'created_at', NEW.created_at
        )::text
      );
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_donation_notify ON donations",
    """
    CREATE TRIGGER trg_donation_notify
      AFTER INSERT OR UPDATE OF status ON donations
      FOR EACH ROW
      EXECUTE FUNCTION notify_donation_change()
    """,
]


async def init_schema(db: asyncpg.Pool) -> None:
    db_name = await db.fetchval("SELECT current_database()")
    logging.info("Connected to database: %s", db_name)
    async with db.acquire() as conn:
        for statement in SCHEMA_STATEMENTS:
            await conn.execute(statement)
    logging.info("Database schema is ready")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global pool
    if not JWT_SECRET:
        raise RuntimeError("JWT_SECRET is required")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dsn, ssl = parse_database_url()
    pool_kwargs: dict = {"dsn": dsn, "min_size": 1, "max_size": 10}
    if ssl is not None:
        pool_kwargs["ssl"] = ssl
    pool = await asyncpg.create_pool(**pool_kwargs)
    await init_schema(pool)
    try:
        yield
    finally:
        await pool.close()
        pool = None


app = FastAPI(
    title="SBP Donations",
    description=(
        "Техническая документация API. Сайт для стримеров открывается с главной страницы `/`, "
        "а не отсюда. Technical API docs. The streamer website is `/`, not this page."
    ),
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)

_cors_origins = [item.strip() for item in os.getenv("CORS_ORIGINS", "*").split(",") if item.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, _exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": msg(request, "validation")})


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(_request: Request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    logging.exception("Unhandled error")
    detail = msg(request, "server_error")
    if isinstance(exc, asyncpg.PostgresError):
        detail = f"{msg(request, 'db_error')}: {exc}"
    return JSONResponse(status_code=500, content={"detail": detail})


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
    avatar_color: Optional[str]
    created_at: datetime


class ProfileUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)
    avatar_color: Optional[str] = Field(default=None, max_length=7)
    clear_avatar: Optional[bool] = None


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


class DonationStatusUpdate(BaseModel):
    status: Literal["confirmed", "rejected"]


def row_to_profile(row: asyncpg.Record) -> ProfileOut:
    keys = set(row.keys())
    return ProfileOut(
        id=row["id"],
        username=row["username"],
        display_name=row["display_name"],
        description=row["description"],
        sber_qr_url=row["sber_qr_url"],
        avatar_url=row["avatar_url"],
        avatar_color=row["avatar_color"] if "avatar_color" in keys else None,
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
    request: Request,
    creds: HTTPAuthorizationCredentials = Depends(security),
) -> asyncpg.Record:
    try:
        payload = jwt.decode(creds.credentials, JWT_SECRET, algorithms=[JWT_ALG])
        user_id = UUID(payload["sub"])
    except Exception as exc:
        raise HTTPException(status_code=401, detail=msg(request, "bad_token")) from exc

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
        raise HTTPException(status_code=401, detail=msg(request, "bad_token"))
    return user


async def unique_username(conn: asyncpg.Connection, base: str) -> str:
    candidate = base
    suffix = 0
    while await conn.fetchval("SELECT 1 FROM profiles WHERE username = $1", candidate):
        suffix += 1
        candidate = f"{base}{suffix}"
    return candidate


@app.get("/docs", include_in_schema=False)
async def custom_docs():
    html = get_swagger_ui_html(
        openapi_url="/openapi.json",
        title="SBP Donations API",
    ).body.decode()
    extra = '<script src="/js/swagger-i18n.js"></script></body>'
    return HTMLResponse(html.replace("</body>", extra))


@app.get("/health", summary="Проверка сервера")
async def health():
    return {"status": "ok", "service": "sbp-donation-api"}


@app.get("/health/db", summary="Проверка базы данных")
async def health_db():
    try:
        value = await pool.fetchval("SELECT 1")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": "ok", "database": "connected", "check": value}


@app.post("/api/auth/register", status_code=201, summary="Регистрация")
async def register(request: Request, body: AuthRequest) -> AuthResponse:
    email = str(body.email).lower()
    password_hash = hash_password(body.password)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                user = await conn.fetchrow(
                    """
                    INSERT INTO users (email, password_hash, email_verified)
                    VALUES ($1, $2, TRUE)
                    RETURNING id, email
                    """,
                    email,
                    password_hash,
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
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail=msg(request, "email_taken")) from exc
    except asyncpg.PostgresError as exc:
        logging.exception("Register failed")
        raise HTTPException(status_code=500, detail=f"{msg(request, 'db_error')}: {exc}") from exc

    return AuthResponse(
        token=create_token(user["id"]),
        user_id=user["id"],
        email=user["email"],
        username=username,
    )


@app.post("/api/auth/login", summary="Вход")
async def login(request: Request, body: AuthRequest) -> AuthResponse:
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
        raise HTTPException(status_code=401, detail=msg(request, "bad_login"))
    if not user["username"]:
        raise HTTPException(status_code=409, detail=msg(request, "no_profile"))

    return AuthResponse(
        token=create_token(user["id"]),
        user_id=user["id"],
        email=user["email"],
        username=user["username"],
    )


@app.get("/api/auth/me", summary="Текущий пользователь")
async def me(user: asyncpg.Record = Depends(current_user)) -> UserOut:
    return UserOut(
        id=user["id"],
        email=user["email"],
        email_verified=user["email_verified"],
        username=user["username"],
        created_at=user["created_at"],
    )


@app.get("/api/profiles/me", summary="Мой профиль")
async def my_profile(request: Request, user: asyncpg.Record = Depends(current_user)) -> ProfileOut:
    row = await pool.fetchrow("SELECT * FROM profiles WHERE id = $1", user["id"])
    if not row:
        raise HTTPException(status_code=404, detail=msg(request, "no_profile"))
    return row_to_profile(row)


@app.patch("/api/profiles/me", summary="Обновить профиль")
async def update_my_profile(
    request: Request,
    body: ProfileUpdate,
    user: asyncpg.Record = Depends(current_user),
) -> ProfileOut:
    color = body.avatar_color
    if color:
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            raise HTTPException(status_code=400, detail=msg(request, "bad_color"))
        color = color.lower()
    row = await pool.fetchrow(
        """
        UPDATE profiles
        SET
            display_name = COALESCE($2, display_name),
            description = COALESCE($3, description),
            avatar_color = COALESCE($4, avatar_color),
            avatar_url = CASE WHEN $5 THEN NULL ELSE avatar_url END,
            updated_at = NOW()
        WHERE id = $1
        RETURNING *
        """,
        user["id"],
        body.display_name,
        body.description,
        color,
        bool(body.clear_avatar),
    )
    if not row:
        raise HTTPException(status_code=404, detail=msg(request, "no_profile"))
    return row_to_profile(row)


async def save_profile_image(
    request: Request,
    file: UploadFile,
    user: asyncpg.Record,
    column: str,
) -> ProfileOut:
    if not file.content_type or file.content_type not in {
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/webp",
    }:
        raise HTTPException(status_code=400, detail=msg(request, "qr_type"))
    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail=msg(request, "qr_missing"))
    if len(payload) > 2 * 1024 * 1024:
        raise HTTPException(status_code=400, detail=msg(request, "qr_size"))

    if column not in {"sber_qr_url", "avatar_url"}:
        raise HTTPException(status_code=500, detail=msg(request, "server_error"))
    suffix = Path(file.filename or "image.png").suffix.lower() or ".png"
    name = f"{user['id']}_{column}_{uuid4().hex}{suffix}"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOAD_DIR / name).write_bytes(payload)
    public_url = f"/uploads/{name}"
    row = await pool.fetchrow(
        f"""
        UPDATE profiles
        SET {column} = $2, updated_at = NOW()
        WHERE id = $1
        RETURNING *
        """,
        user["id"],
        public_url,
    )
    if not row:
        raise HTTPException(status_code=404, detail=msg(request, "no_profile"))
    return row_to_profile(row)


@app.post("/api/profiles/me/qr", summary="Загрузить QR-код")
async def upload_qr(
    request: Request,
    file: UploadFile = File(...),
    user: asyncpg.Record = Depends(current_user),
) -> ProfileOut:
    return await save_profile_image(request, file, user, "sber_qr_url")


@app.post("/api/profiles/me/avatar", summary="Загрузить фото кружка")
async def upload_avatar(
    request: Request,
    file: UploadFile = File(...),
    user: asyncpg.Record = Depends(current_user),
) -> ProfileOut:
    return await save_profile_image(request, file, user, "avatar_url")


@app.get("/api/profiles/{username}", summary="Публичный профиль стримера")
async def public_profile(request: Request, username: str) -> ProfileOut:
    row = await pool.fetchrow(
        "SELECT * FROM profiles WHERE username = $1",
        username.lower(),
    )
    if not row:
        raise HTTPException(status_code=404, detail=msg(request, "no_profile"))
    return row_to_profile(row)


@app.post("/api/donations", status_code=201, summary="Создать донат")
async def create_donation(request: Request, body: DonationCreate) -> DonationOut:
    profile_exists = await pool.fetchval(
        "SELECT 1 FROM profiles WHERE id = $1",
        body.profile_id,
    )
    if not profile_exists:
        raise HTTPException(status_code=404, detail=msg(request, "no_profile"))

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
    donation = row_to_donation(row)
    await hub.broadcast(
        str(body.profile_id),
        {"type": "donation.new", "data": donation.model_dump(mode="json")},
    )
    return donation


@app.websocket("/ws/donations")
async def donations_socket(websocket: WebSocket, profile_id: UUID):
    key = str(profile_id)
    await hub.connect(key, websocket)
    try:
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=25)
            except TimeoutError:
                await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        hub.disconnect(key, websocket)
    except Exception:
        hub.disconnect(key, websocket)


@app.get("/api/donations", summary="Список донатов")
async def list_donations(
    profile_id: UUID = Query(...),
    limit: int = Query(20, ge=1, le=100),
    since: Optional[datetime] = Query(default=None),
) -> list[DonationOut]:
    if since:
        rows = await pool.fetch(
            """
            SELECT id, profile_id, donor_name, amount, message, status, created_at
            FROM donations
            WHERE profile_id = $1 AND created_at > $2
            ORDER BY created_at ASC
            LIMIT $3
            """,
            profile_id,
            since,
            limit,
        )
    else:
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


@app.post("/api/donations/confirm-all", summary="Подтвердить все ожидающие")
async def confirm_all(user: asyncpg.Record = Depends(current_user)):
    result = await pool.execute(
        """
        UPDATE donations
        SET status = 'confirmed', confirmed_at = NOW()
        WHERE profile_id = $1 AND status = 'pending'
        """,
        user["id"],
    )
    return {"updated": int(result.split()[-1])}


@app.patch("/api/donations/{donation_id}", summary="Подтвердить или отклонить донат")
async def update_donation(
    request: Request,
    donation_id: UUID,
    body: DonationStatusUpdate,
    user: asyncpg.Record = Depends(current_user),
) -> DonationOut:
    confirmed_at = datetime.now(timezone.utc) if body.status == "confirmed" else None
    row = await pool.fetchrow(
        """
        UPDATE donations
        SET status = $3, confirmed_at = COALESCE($4, confirmed_at)
        WHERE id = $1 AND profile_id = $2
        RETURNING id, profile_id, donor_name, amount, message, status, created_at
        """,
        donation_id,
        user["id"],
        body.status,
        confirmed_at,
    )
    if not row:
        raise HTTPException(status_code=404, detail=msg(request, "confirm_own"))
    return row_to_donation(row)


@app.get("/uploads/{name}", include_in_schema=False)
async def uploaded_file(name: str):
    path = (UPLOAD_DIR / Path(name).name).resolve()
    if not str(path).startswith(str(UPLOAD_DIR.resolve())) or not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path)


@app.get("/", include_in_schema=False)
async def home_page():
    index = STATIC_DIR / "index.html"
    if index.is_file():
        return FileResponse(index)
    return HTMLResponse(
        "<p>Сайт ещё не скопирован в контейнер. Откройте <a href='/docs'>/docs</a>.</p>"
    )


if STATIC_DIR.is_dir():
    app.mount("/js", StaticFiles(directory=str(STATIC_DIR / "js")), name="js")
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
