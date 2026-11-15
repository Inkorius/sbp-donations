import asyncio
import hashlib
import hmac
import io
import logging
import os
import re
import secrets
import threading
import wave
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from uuid import UUID, uuid4

import asyncpg
import httpx
import jwt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBasic, HTTPBasicCredentials, HTTPBearer
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

YOOKASSA_API = "https://api.yookassa.ru/v3"
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", "https://sbpdonation.ru/donate.html?success=1").strip()
JWT_SECRET = os.getenv("JWT_SECRET", "").strip()
JWT_ALG = "HS256"
JWT_EXPIRE_HOURS = 72
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
DOCS_USER = os.getenv("DOCS_USER", "").strip()
DOCS_PASSWORD = os.getenv("DOCS_PASSWORD", "").strip()
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/app/uploads"))
PIPER_DIR = Path(os.getenv("PIPER_DIR", str(UPLOAD_DIR / "piper")))
STATIC_DIR = Path(os.getenv("STATIC_DIR", str(Path(__file__).resolve().parent / "static")))
TTS_VOICE_MODELS = {
    "irina": "ru_RU-irina-medium",
    "denis": "ru_RU-denis-medium",
}
_piper_voices: dict[str, object] = {}
_piper_thread_lock = threading.Lock()
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
        "sound_type": "Нужен звук MP3, WAV или OGG",
        "sound_size": "Звук слишком большой. Максимум 3 МБ",
        "tts_fail": "Не удалось создать озвучку",
        "bad_color": "Цвет кружка должен быть в формате #RRGGBB",
        "confirm_own": "Можно менять только свои донаты",
        "yookassa_off": "ЮKassa не подключена",
        "yookassa_fail": "Не удалось создать платёж ЮKassa",
        "yookassa_paid": "Этот донат уже подтверждён",
        "reg_disabled": "Public registration is disabled",
        "admin_only": "Нужны права администратора",
        "username_taken": "Это имя пользователя уже занято",
        "bad_username": "Имя пользователя: латиница, цифры и _",
        "admin_self": "Нельзя удалить свой аккаунт",
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
        "sound_type": "Use an MP3, WAV, or OGG sound",
        "sound_size": "Sound file is too large. Maximum size is 3 MB",
        "tts_fail": "Could not generate voice audio",
        "bad_color": "Circle color must be in #RRGGBB format",
        "confirm_own": "You can only change your own donations",
        "yookassa_off": "YooKassa is not connected",
        "yookassa_fail": "Could not create a YooKassa payment",
        "yookassa_paid": "This donation is already confirmed",
        "reg_disabled": "Public registration is disabled",
        "admin_only": "Admin access required",
        "username_taken": "This username is already taken",
        "bad_username": "Username may contain only latin letters, digits, and _",
        "admin_self": "You cannot delete your own account",
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
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS alert_sound_url TEXT",
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
    "ALTER TABLE donations ADD COLUMN IF NOT EXISTS tts_voice TEXT",
    "ALTER TABLE donations ADD COLUMN IF NOT EXISTS yookassa_payment_id TEXT",
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS yookassa_shop_id TEXT",
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS yookassa_secret_key TEXT",
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS self_employed BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE donations ADD COLUMN IF NOT EXISTS receipt_uuid TEXT",
    "ALTER TABLE donations ADD COLUMN IF NOT EXISTS receipt_url TEXT",
    """
    CREATE TABLE IF NOT EXISTS receipts_queue (
      id            UUID PRIMARY KEY,
      donation_id   UUID NOT NULL REFERENCES donations(id) ON DELETE CASCADE,
      amount        NUMERIC(12, 2) NOT NULL,
      service_name  TEXT NOT NULL,
      status        TEXT NOT NULL DEFAULT 'pending',
      attempts      INTEGER NOT NULL DEFAULT 0,
      last_error    TEXT,
      created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_receipts_queue_status ON receipts_queue (status, created_at)",
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
    await bootstrap_admin(db)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global pool
    if not JWT_SECRET:
        raise RuntimeError("JWT_SECRET is required")
    try:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        PIPER_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        logging.warning("Upload/Piper dirs are not writable: %s %s", UPLOAD_DIR, PIPER_DIR)
    dsn, ssl = parse_database_url()
    pool_kwargs: dict = {"dsn": dsn, "min_size": 1, "max_size": 10}
    if ssl is not None:
        pool_kwargs["ssl"] = ssl
    pool = await asyncpg.create_pool(**pool_kwargs)
    await init_schema(pool)
    asyncio.create_task(warmup_piper_voices())
    try:
        from nalog_service import receipts_retry_loop

        asyncio.create_task(receipts_retry_loop(pool))
    except Exception:
        logging.exception("Мой Налог retry loop not started")
    try:
        yield
    finally:
        await pool.close()
        pool = None


app = FastAPI(
    title="SBP Donations",
    description=(
        "Self-hosted SBP donations. Public registration is disabled. "
        "Create streamer accounts as admin via /api/admin/users."
    ),
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

_cors_origins = [item.strip() for item in os.getenv("CORS_ORIGINS", "*").split(",") if item.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def autoplay_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.endswith("obs-overlay.html") or path.startswith("/api/tts"):
        response.headers["Permissions-Policy"] = "autoplay=*"
    return response


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


class AdminUserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)
    username: str = Field(min_length=1, max_length=40)


class AdminPasswordReset(BaseModel):
    password: str = Field(min_length=6, max_length=128)


class AdminUserOut(BaseModel):
    id: UUID
    email: str
    username: Optional[str] = None
    is_admin: bool = False
    created_at: datetime


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
    alert_sound_url: Optional[str] = None
    yookassa_enabled: bool = False
    yookassa_shop_id: Optional[str] = None
    self_employed: bool = False
    created_at: datetime


class ProfileUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)
    avatar_color: Optional[str] = Field(default=None, max_length=7)
    clear_avatar: Optional[bool] = None
    clear_alert_sound: Optional[bool] = None
    yookassa_shop_id: Optional[str] = Field(default=None, max_length=64)
    yookassa_secret_key: Optional[str] = Field(default=None, max_length=200)
    clear_yookassa: Optional[bool] = None
    self_employed: Optional[bool] = None


class DonationCreate(BaseModel):
    profile_id: UUID
    donor_name: str = Field(min_length=1, max_length=100)
    amount: Decimal = Field(gt=0)
    message: Optional[str] = Field(default=None, max_length=200)
    tts_voice: Optional[str] = Field(default="irina", max_length=40)


class DonationOut(BaseModel):
    id: UUID
    profile_id: UUID
    donor_name: str
    amount: Decimal
    message: Optional[str]
    status: str
    tts_voice: Optional[str] = None
    confirmed_at: Optional[datetime] = None
    yookassa_payment_id: Optional[str] = None
    created_at: datetime


class YookassaPaymentOut(BaseModel):
    donation_id: UUID
    payment_id: str
    confirmation_url: str
    status: str


class DonationStatusUpdate(BaseModel):
    status: Literal["confirmed", "rejected"]


def yookassa_env_ready() -> bool:
    return bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY)


def profile_self_employed(row: Optional[asyncpg.Record]) -> bool:
    if not row:
        return False
    keys = set(row.keys())
    return bool("self_employed" in keys and row["self_employed"])


def profile_yookassa_auth(row: Optional[asyncpg.Record]) -> Optional[tuple[str, str]]:
    if row:
        keys = set(row.keys())
        shop = (row["yookassa_shop_id"] or "").strip() if "yookassa_shop_id" in keys else ""
        secret = (row["yookassa_secret_key"] or "").strip() if "yookassa_secret_key" in keys else ""
        if shop and secret:
            return shop, secret
    if yookassa_env_ready():
        return YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY
    return None


def row_to_profile(row: asyncpg.Record, *, public: bool = True) -> ProfileOut:
    keys = set(row.keys())
    shop = (row["yookassa_shop_id"] or "").strip() if "yookassa_shop_id" in keys and row["yookassa_shop_id"] else ""
    secret = (row["yookassa_secret_key"] or "").strip() if "yookassa_secret_key" in keys and row["yookassa_secret_key"] else ""
    self_employed = bool("self_employed" in keys and row["self_employed"])
    has_keys = bool(shop and secret) or yookassa_env_ready()
    return ProfileOut(
        id=row["id"],
        username=row["username"],
        display_name=row["display_name"],
        description=row["description"],
        sber_qr_url=row["sber_qr_url"],
        avatar_url=row["avatar_url"],
        avatar_color=row["avatar_color"] if "avatar_color" in keys else None,
        alert_sound_url=row["alert_sound_url"] if "alert_sound_url" in keys else None,
        yookassa_enabled=self_employed and has_keys,
        yookassa_shop_id=None if public else (shop or None),
        self_employed=self_employed,
        created_at=row["created_at"],
    )


def row_to_donation(row: asyncpg.Record) -> DonationOut:
    keys = set(row.keys())
    return DonationOut(
        id=row["id"],
        profile_id=row["profile_id"],
        donor_name=row["donor_name"],
        amount=row["amount"],
        message=row["message"],
        status=row["status"],
        tts_voice=row["tts_voice"] if "tts_voice" in keys else None,
        confirmed_at=row["confirmed_at"] if "confirmed_at" in keys else None,
        yookassa_payment_id=row["yookassa_payment_id"] if "yookassa_payment_id" in keys else None,
        created_at=row["created_at"],
    )


async def broadcast_donation(event: str, donation: DonationOut) -> None:
    await hub.broadcast(
        str(donation.profile_id),
        {"type": event, "data": donation.model_dump(mode="json")},
    )


async def mark_donation_confirmed(donation_id: UUID, payment_id: Optional[str] = None) -> Optional[DonationOut]:
    row = await pool.fetchrow(
        """
        UPDATE donations
        SET
            status = 'confirmed',
            confirmed_at = COALESCE(confirmed_at, NOW()),
            yookassa_payment_id = COALESCE($2, yookassa_payment_id),
            payment_ref = COALESCE($2, payment_ref),
            payment_source = CASE WHEN $2 IS NULL THEN payment_source ELSE 'yookassa' END
        WHERE id = $1 AND status <> 'rejected'
        RETURNING id, profile_id, donor_name, amount, message, status, tts_voice, confirmed_at, yookassa_payment_id, created_at
        """,
        donation_id,
        payment_id,
    )
    if not row:
        return None
    donation = row_to_donation(row)
    await broadcast_donation("donation.confirmed", donation)
    return donation


def yookassa_return_url(username: str) -> str:
    base = YOOKASSA_RETURN_URL
    sep = "&" if "?" in base else "?"
    if "user=" in base:
        return base
    return f"{base}{sep}user={username}&paid=1"


def resolve_tts_voice(name: Optional[str]) -> str:
    raw = (name or "irina").strip().lower()
    if raw in TTS_VOICE_MODELS:
        return raw
    for key, model_id in TTS_VOICE_MODELS.items():
        if raw == model_id.lower() or raw.replace("-", "_").endswith(key):
            return key
    return "irina"


def download_piper_model(model_id: str) -> Path:
    PIPER_DIR.mkdir(parents=True, exist_ok=True)
    onnx = PIPER_DIR / f"{model_id}.onnx"
    cfg = PIPER_DIR / f"{model_id}.onnx.json"
    if onnx.is_file() and onnx.stat().st_size > 1000 and cfg.is_file() and cfg.stat().st_size > 10:
        return onnx
    from piper.download_voices import download_voice

    logging.info("Downloading Piper voice %s into %s", model_id, PIPER_DIR)
    download_voice(model_id, PIPER_DIR)
    if not onnx.is_file():
        raise RuntimeError(f"Piper model missing after download: {onnx}")
    return onnx


def load_piper_voice(key: str):
    with _piper_thread_lock:
        cached = _piper_voices.get(key)
        if cached is not None:
            return cached
        from piper import PiperVoice

        model_id = TTS_VOICE_MODELS[key]
        path = download_piper_model(model_id)
        voice = PiperVoice.load(str(path))
        _piper_voices[key] = voice
        logging.info("Loaded Piper voice %s", model_id)
        return voice


def piper_wav_bytes(key: str, text: str) -> bytes:
    voice = load_piper_voice(key)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        voice.synthesize_wav(text, wav_file)
    payload = buf.getvalue()
    if len(payload) < 44:
        raise RuntimeError("empty piper wav")
    return payload


async def warmup_piper_voices() -> None:
    for key in TTS_VOICE_MODELS:
        try:
            await asyncio.to_thread(load_piper_voice, key)
        except Exception:
            logging.exception("Could not prepare Piper voice %s", key)


async def synthesize_speech(text: str, voice_key: str) -> bytes:
    return await asyncio.to_thread(piper_wav_bytes, voice_key, text)


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
        SELECT u.id, u.email, u.email_verified, u.is_admin, u.created_at, p.username
        FROM users u
        LEFT JOIN profiles p ON p.id = u.id
        WHERE u.id = $1
        """,
        user_id,
    )
    if not user:
        raise HTTPException(status_code=401, detail=msg(request, "bad_token"))
    return user


def user_is_admin(row: Optional[asyncpg.Record]) -> bool:
    if not row:
        return False
    keys = set(row.keys())
    if "is_admin" in keys and row["is_admin"]:
        return True
    return bool(ADMIN_EMAIL and str(row["email"]).lower() == ADMIN_EMAIL)


async def current_admin(
    request: Request,
    user: asyncpg.Record = Depends(current_user),
) -> asyncpg.Record:
    if not user_is_admin(user):
        raise HTTPException(status_code=403, detail=msg(request, "admin_only"))
    return user


docs_basic = HTTPBasic(auto_error=False)
optional_bearer = HTTPBearer(auto_error=False)


def docs_enabled() -> bool:
    return bool(DOCS_USER and DOCS_PASSWORD)


def _basic_ok(got: str, expected: str) -> bool:
    try:
        return secrets.compare_digest(got.encode("utf-8"), expected.encode("utf-8"))
    except Exception:
        return False


async def require_docs_auth(
    creds: Optional[HTTPBasicCredentials] = Depends(docs_basic),
) -> None:
    if not docs_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    if creds is None or not (_basic_ok(creds.username, DOCS_USER) and _basic_ok(creds.password, DOCS_PASSWORD)):
        raise HTTPException(
            status_code=401,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Basic"},
        )


def normalize_username(raw: str) -> str:
    cleaned = _USERNAME_RE.sub("", (raw or "").lower()).strip("_")
    return cleaned[:40]


async def unique_username(conn: asyncpg.Connection, base: str) -> str:
    candidate = base
    suffix = 0
    while await conn.fetchval("SELECT 1 FROM profiles WHERE username = $1", candidate):
        suffix += 1
        candidate = f"{base}{suffix}"
    return candidate


async def create_user_with_profile(
    conn: asyncpg.Connection,
    email: str,
    password: str,
    username: Optional[str] = None,
    is_admin: bool = False,
) -> tuple[asyncpg.Record, str]:
    if username:
        uname = normalize_username(username)
        if not uname:
            raise ValueError("bad_username")
        taken = await conn.fetchval("SELECT 1 FROM profiles WHERE username = $1", uname)
        if taken:
            raise ValueError("username_taken")
    else:
        uname = await unique_username(conn, username_from_email(email))
    user = await conn.fetchrow(
        """
        INSERT INTO users (email, password_hash, email_verified, is_admin)
        VALUES ($1, $2, TRUE, $3)
        RETURNING id, email, email_verified, is_admin, created_at
        """,
        email,
        hash_password(password),
        is_admin,
    )
    await conn.execute(
        """
        INSERT INTO profiles (id, username, display_name)
        VALUES ($1, $2, $2)
        """,
        user["id"],
        uname,
    )
    return user, uname


async def bootstrap_admin(db: asyncpg.Pool) -> None:
    if not ADMIN_EMAIL:
        logging.warning("ADMIN_EMAIL is not set; no admin account will be bootstrapped")
        return
    async with db.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow("SELECT id, is_admin FROM users WHERE email = $1", ADMIN_EMAIL)
            if existing:
                await conn.execute("UPDATE users SET is_admin = TRUE WHERE id = $1", existing["id"])
                logging.info("Admin flag set for %s", ADMIN_EMAIL)
                return
            if len(ADMIN_PASSWORD) < 6:
                logging.warning("ADMIN_PASSWORD is missing; create the admin user later via SQL or env")
                return
            try:
                await create_user_with_profile(
                    conn,
                    ADMIN_EMAIL,
                    ADMIN_PASSWORD,
                    username_from_email(ADMIN_EMAIL),
                    is_admin=True,
                )
                logging.info("Created admin user %s", ADMIN_EMAIL)
            except asyncpg.UniqueViolationError:
                await conn.execute("UPDATE users SET is_admin = TRUE WHERE email = $1", ADMIN_EMAIL)


def row_to_admin_user(row: asyncpg.Record) -> AdminUserOut:
    keys = set(row.keys())
    return AdminUserOut(
        id=row["id"],
        email=row["email"],
        username=row["username"] if "username" in keys else None,
        is_admin=user_is_admin(row),
        created_at=row["created_at"],
    )


@app.get("/docs", include_in_schema=False)
async def custom_docs(_: None = Depends(require_docs_auth)):
    html = get_swagger_ui_html(
        openapi_url="/openapi.json",
        title="SBP Donations API",
    ).body.decode()
    extra = '<script src="/js/swagger-i18n.js"></script></body>'
    return HTMLResponse(html.replace("</body>", extra))


@app.get("/redoc", include_in_schema=False)
async def custom_redoc(_: None = Depends(require_docs_auth)):
    return get_redoc_html(openapi_url="/openapi.json", title="SBP Donations API")


@app.get("/openapi.json", include_in_schema=False)
async def openapi_json(_: None = Depends(require_docs_auth)):
    return JSONResponse(app.openapi())


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


@app.post("/api/auth/register", status_code=201, summary="Регистрация (только администратор)")
async def register(
    request: Request,
    body: AuthRequest,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> AuthResponse:
    admin = False
    if creds:
        try:
            payload = jwt.decode(creds.credentials, JWT_SECRET, algorithms=[JWT_ALG])
            row = await pool.fetchrow(
                "SELECT id, email, is_admin FROM users WHERE id = $1",
                UUID(payload["sub"]),
            )
            admin = user_is_admin(row)
        except Exception:
            admin = False
    if not admin:
        raise HTTPException(status_code=403, detail="Public registration is disabled")
    email = str(body.email).lower()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                user, username = await create_user_with_profile(conn, email, body.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=msg(request, str(exc))) from exc
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


@app.get("/api/admin/users", summary="Список пользователей", tags=["admin"])
async def admin_list_users(_admin: asyncpg.Record = Depends(current_admin)) -> list[AdminUserOut]:
    rows = await pool.fetch(
        """
        SELECT u.id, u.email, u.is_admin, u.created_at, p.username
        FROM users u
        LEFT JOIN profiles p ON p.id = u.id
        ORDER BY u.created_at DESC
        """
    )
    return [row_to_admin_user(row) for row in rows]


@app.post("/api/admin/users", status_code=201, summary="Создать пользователя", tags=["admin"])
async def admin_create_user(
    request: Request,
    body: AdminUserCreate,
    _admin: asyncpg.Record = Depends(current_admin),
) -> AdminUserOut:
    email = str(body.email).lower()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                user, username = await create_user_with_profile(
                    conn,
                    email,
                    body.password,
                    body.username,
                    is_admin=False,
                )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=msg(request, str(exc))) from exc
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail=msg(request, "email_taken")) from exc
    except asyncpg.PostgresError as exc:
        logging.exception("Admin create user failed")
        raise HTTPException(status_code=500, detail=f"{msg(request, 'db_error')}: {exc}") from exc
    return AdminUserOut(
        id=user["id"],
        email=user["email"],
        username=username,
        is_admin=bool(user["is_admin"]),
        created_at=user["created_at"],
    )


@app.delete("/api/admin/users/{user_id}", summary="Удалить пользователя", tags=["admin"])
async def admin_delete_user(
    request: Request,
    user_id: UUID,
    admin: asyncpg.Record = Depends(current_admin),
) -> dict:
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail=msg(request, "admin_self"))
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM profiles WHERE id = $1", user_id)
            result = await conn.execute("DELETE FROM users WHERE id = $1", user_id)
    if result.endswith("0"):
        raise HTTPException(status_code=404, detail=msg(request, "no_profile"))
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/reset-password", summary="Сбросить пароль", tags=["admin"])
async def admin_reset_password(
    request: Request,
    user_id: UUID,
    body: AdminPasswordReset,
    _admin: asyncpg.Record = Depends(current_admin),
) -> dict:
    updated = await pool.fetchval(
        """
        UPDATE users
        SET password_hash = $2, updated_at = NOW()
        WHERE id = $1
        RETURNING id
        """,
        user_id,
        hash_password(body.password),
    )
    if not updated:
        raise HTTPException(status_code=404, detail=msg(request, "no_profile"))
    return {"ok": True}


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
    return row_to_profile(row, public=False)


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
    shop = body.yookassa_shop_id.strip() if body.yookassa_shop_id else None
    secret = body.yookassa_secret_key.strip() if body.yookassa_secret_key else None
    row = await pool.fetchrow(
        """
        UPDATE profiles
        SET
            display_name = COALESCE($2, display_name),
            description = COALESCE($3, description),
            avatar_color = COALESCE($4, avatar_color),
            avatar_url = CASE WHEN $5 THEN NULL ELSE avatar_url END,
            alert_sound_url = CASE WHEN $6 THEN NULL ELSE alert_sound_url END,
            yookassa_shop_id = CASE WHEN $7 THEN NULL ELSE COALESCE($8, yookassa_shop_id) END,
            yookassa_secret_key = CASE WHEN $7 THEN NULL ELSE COALESCE($9, yookassa_secret_key) END,
            self_employed = COALESCE($10, self_employed),
            updated_at = NOW()
        WHERE id = $1
        RETURNING *
        """,
        user["id"],
        body.display_name,
        body.description,
        color,
        bool(body.clear_avatar),
        bool(body.clear_alert_sound),
        bool(body.clear_yookassa),
        shop,
        secret,
        body.self_employed,
    )
    if not row:
        raise HTTPException(status_code=404, detail=msg(request, "no_profile"))
    return row_to_profile(row, public=False)


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


@app.post("/api/profiles/me/alert-sound", summary="Загрузить звук уведомления")
async def upload_alert_sound(
    request: Request,
    file: UploadFile = File(...),
    user: asyncpg.Record = Depends(current_user),
) -> ProfileOut:
    suffix = Path(file.filename or "alert.mp3").suffix.lower()
    allowed_types = {
        "audio/mpeg",
        "audio/mp3",
        "audio/wav",
        "audio/x-wav",
        "audio/wave",
        "audio/ogg",
        "audio/webm",
        "audio/mp4",
    }
    allowed_suffix = {".mp3", ".wav", ".ogg", ".m4a", ".webm"}
    if suffix not in allowed_suffix and (file.content_type or "") not in allowed_types:
        raise HTTPException(status_code=400, detail=msg(request, "sound_type"))
    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail=msg(request, "qr_missing"))
    if len(payload) > 3 * 1024 * 1024:
        raise HTTPException(status_code=400, detail=msg(request, "sound_size"))
    if suffix not in allowed_suffix:
        suffix = ".mp3"
    name = f"{user['id']}_alert_{uuid4().hex}{suffix}"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOAD_DIR / name).write_bytes(payload)
    public_url = f"/uploads/{name}"
    row = await pool.fetchrow(
        """
        UPDATE profiles
        SET alert_sound_url = $2, updated_at = NOW()
        WHERE id = $1
        RETURNING *
        """,
        user["id"],
        public_url,
    )
    if not row:
        raise HTTPException(status_code=404, detail=msg(request, "no_profile"))
    return row_to_profile(row)


@app.get("/api/tts", summary="Озвучка доната")
async def overlay_tts(
    request: Request,
    text: str = Query(..., min_length=1, max_length=500),
    voice: str = Query(default="irina", max_length=40),
):
    clean = re.sub(r"\s+", " ", text).strip()
    if not clean:
        raise HTTPException(status_code=400, detail=msg(request, "tts_fail"))
    try:
        payload = await synthesize_speech(clean, resolve_tts_voice(voice))
    except Exception as exc:
        logging.exception("TTS failed")
        raise HTTPException(status_code=502, detail=msg(request, "tts_fail")) from exc
    return Response(
        content=payload,
        media_type="audio/wav",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": "inline",
            "Accept-Ranges": "bytes",
        },
    )


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
        INSERT INTO donations (profile_id, donor_name, amount, message, status, tts_voice)
        VALUES ($1, $2, $3, $4, 'pending', $5)
        RETURNING id, profile_id, donor_name, amount, message, status, tts_voice, confirmed_at, yookassa_payment_id, created_at
        """,
        body.profile_id,
        body.donor_name.strip(),
        body.amount,
        (body.message or "").strip() or None,
        resolve_tts_voice(body.tts_voice),
    )
    donation = row_to_donation(row)
    await broadcast_donation("donation.new", donation)
    return donation


@app.post("/api/donations/{donation_id}/yookassa", summary="Создать платёж СБП через ЮKassa")
async def create_yookassa_payment(request: Request, donation_id: UUID) -> YookassaPaymentOut:
    donation_row = await pool.fetchrow("SELECT * FROM donations WHERE id = $1", donation_id)
    if not donation_row:
        raise HTTPException(status_code=404, detail=msg(request, "no_profile"))
    if donation_row["status"] == "confirmed":
        raise HTTPException(status_code=409, detail=msg(request, "yookassa_paid"))
    if donation_row["status"] == "rejected":
        raise HTTPException(status_code=409, detail=msg(request, "yookassa_fail"))
    profile = await pool.fetchrow("SELECT * FROM profiles WHERE id = $1", donation_row["profile_id"])
    if not profile_self_employed(profile):
        raise HTTPException(status_code=400, detail=msg(request, "yookassa_off"))
    auth = profile_yookassa_auth(profile)
    if not auth:
        raise HTTPException(status_code=400, detail=msg(request, "yookassa_off"))
    shop_id, secret_key = auth
    amount = Decimal(donation_row["amount"]).quantize(Decimal("0.01"))
    payload = {
        "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
        "capture": True,
        "payment_method_data": {"type": "sbp"},
        "confirmation": {
            "type": "redirect",
            "return_url": yookassa_return_url(profile["username"] if profile else ""),
        },
        "description": f"Донат для {profile['username'] if profile else 'стримера'}",
        "metadata": {"donation_id": str(donation_id)},
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{YOOKASSA_API}/payments",
                auth=(shop_id, secret_key),
                headers={
                    "Idempotence-Key": str(uuid4()),
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except httpx.HTTPError as exc:
        logging.exception("YooKassa create failed")
        raise HTTPException(status_code=502, detail=msg(request, "yookassa_fail")) from exc
    if response.status_code >= 400:
        logging.error("YooKassa create error %s %s", response.status_code, response.text)
        raise HTTPException(status_code=502, detail=msg(request, "yookassa_fail"))
    data = response.json()
    confirmation = data.get("confirmation") or {}
    confirmation_url = confirmation.get("confirmation_url")
    payment_id = data.get("id")
    if not confirmation_url or not payment_id:
        raise HTTPException(status_code=502, detail=msg(request, "yookassa_fail"))
    await pool.execute(
        """
        UPDATE donations
        SET yookassa_payment_id = $2, payment_ref = $2, payment_source = 'yookassa'
        WHERE id = $1
        """,
        donation_id,
        payment_id,
    )
    return YookassaPaymentOut(
        donation_id=donation_id,
        payment_id=payment_id,
        confirmation_url=confirmation_url,
        status=data.get("status") or "pending",
    )


@app.post("/api/yookassa/webhook", summary="Вебхук ЮKassa")
async def yookassa_webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail=msg(request, "validation"))
    event = body.get("event")
    obj = body.get("object") or {}
    payment_id = obj.get("id")
    if event != "payment.succeeded" or not payment_id:
        return JSONResponse({"ok": True, "ignored": True})
    metadata = obj.get("metadata") or {}
    donation_id_raw = metadata.get("donation_id")
    donation_row = None
    if donation_id_raw:
        try:
            donation_row = await pool.fetchrow(
                "SELECT * FROM donations WHERE id = $1",
                UUID(str(donation_id_raw)),
            )
        except Exception:
            donation_row = None
    if not donation_row:
        donation_row = await pool.fetchrow(
            "SELECT * FROM donations WHERE yookassa_payment_id = $1",
            payment_id,
        )
    if not donation_row:
        logging.warning("YooKassa webhook for unknown donation payment %s", payment_id)
        return JSONResponse({"ok": True, "unknown": True})
    profile = await pool.fetchrow("SELECT * FROM profiles WHERE id = $1", donation_row["profile_id"])
    if not profile_self_employed(profile):
        return JSONResponse({"ok": True, "ignored": True})
    auth = profile_yookassa_auth(profile)
    if not auth:
        raise HTTPException(status_code=502, detail=msg(request, "yookassa_off"))
    shop_id, secret_key = auth
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            verify = await client.get(
                f"{YOOKASSA_API}/payments/{payment_id}",
                auth=(shop_id, secret_key),
            )
    except httpx.HTTPError as exc:
        logging.exception("YooKassa verify failed")
        raise HTTPException(status_code=502, detail=msg(request, "yookassa_fail")) from exc
    if verify.status_code >= 400:
        raise HTTPException(status_code=502, detail=msg(request, "yookassa_fail"))
    payment = verify.json()
    if payment.get("status") != "succeeded" or payment.get("paid") is not True:
        return JSONResponse({"ok": True, "ignored": True})
    try:
        paid = Decimal(str((payment.get("amount") or {}).get("value") or "0"))
    except Exception:
        paid = Decimal("0")
    if paid.quantize(Decimal("0.01")) != Decimal(donation_row["amount"]).quantize(Decimal("0.01")):
        logging.warning(
            "YooKassa amount mismatch donation=%s paid=%s expected=%s",
            donation_row["id"],
            paid,
            donation_row["amount"],
        )
        return JSONResponse({"ok": True, "mismatch": True})
    donation = await mark_donation_confirmed(donation_row["id"], payment_id)
    if donation:
        try:
            from nalog_service import enqueue_receipt

            await enqueue_receipt(pool, donation.id, Decimal(donation.amount))
        except Exception:
            logging.exception("Could not enqueue Мой Налог receipt")
    return JSONResponse({"ok": True})


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
    since_ms: Optional[int] = Query(default=None),
    status: Optional[str] = Query(default=None),
) -> list[DonationOut]:
    cutoff = None
    if since_ms is not None:
        cutoff = datetime.fromtimestamp(since_ms / 1000.0, tz=timezone.utc)
    elif since is not None:
        cutoff = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    confirmed_only = status == "confirmed"
    select_cols = "id, profile_id, donor_name, amount, message, status, tts_voice, confirmed_at, yookassa_payment_id, created_at"
    if cutoff and confirmed_only:
        rows = await pool.fetch(
            f"""
            SELECT {select_cols}
            FROM donations
            WHERE profile_id = $1 AND status = 'confirmed'
              AND COALESCE(confirmed_at, created_at) > $2
            ORDER BY COALESCE(confirmed_at, created_at) ASC
            LIMIT $3
            """,
            profile_id,
            cutoff,
            limit,
        )
    elif cutoff:
        rows = await pool.fetch(
            f"""
            SELECT {select_cols}
            FROM donations
            WHERE profile_id = $1 AND created_at > $2
            ORDER BY created_at ASC
            LIMIT $3
            """,
            profile_id,
            cutoff,
            limit,
        )
    else:
        rows = await pool.fetch(
            f"""
            SELECT {select_cols}
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
    rows = await pool.fetch(
        """
        UPDATE donations
        SET status = 'confirmed', confirmed_at = COALESCE(confirmed_at, NOW())
        WHERE profile_id = $1 AND status = 'pending'
        RETURNING id, profile_id, donor_name, amount, message, status, tts_voice, confirmed_at, yookassa_payment_id, created_at
        """,
        user["id"],
    )
    donations = [row_to_donation(row) for row in rows]
    for donation in donations:
        await broadcast_donation("donation.confirmed", donation)
    return {"updated": len(donations)}


@app.patch("/api/donations/{donation_id}", summary="Подтвердить или отклонить донат")
async def update_donation(
    request: Request,
    donation_id: UUID,
    body: DonationStatusUpdate,
    user: asyncpg.Record = Depends(current_user),
) -> DonationOut:
    owned = await pool.fetchval(
        "SELECT 1 FROM donations WHERE id = $1 AND profile_id = $2",
        donation_id,
        user["id"],
    )
    if not owned:
        raise HTTPException(status_code=404, detail=msg(request, "confirm_own"))
    if body.status == "confirmed":
        donation = await mark_donation_confirmed(donation_id)
        if not donation:
            raise HTTPException(status_code=404, detail=msg(request, "confirm_own"))
        return donation
    row = await pool.fetchrow(
        """
        UPDATE donations
        SET status = $3
        WHERE id = $1 AND profile_id = $2
        RETURNING id, profile_id, donor_name, amount, message, status, tts_voice, confirmed_at, yookassa_payment_id, created_at
        """,
        donation_id,
        user["id"],
        body.status,
    )
    if not row:
        raise HTTPException(status_code=404, detail=msg(request, "confirm_own"))
    return row_to_donation(row)


@app.get("/uploads/{name}", include_in_schema=False)
async def uploaded_file(name: str):
    path = (UPLOAD_DIR / Path(name).name).resolve()
    if not str(path).startswith(str(UPLOAD_DIR.resolve())) or not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    types = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg", ".m4a": "audio/mp4", ".webm": "audio/webm"}
    media = types.get(path.suffix.lower())
    if media:
        return FileResponse(path, media_type=media)
    return FileResponse(path)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    path = STATIC_DIR / "favicon.ico"
    if path.is_file():
        return FileResponse(path, media_type="image/x-icon")
    raise HTTPException(status_code=404, detail="not found")


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
