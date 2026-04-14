import base64
import csv
import hashlib
import io
import json
import logging
import os
import re
import secrets
from datetime import UTC, date as date_type, datetime, timedelta
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

from authlib.jose import JsonWebToken
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from httpx import AsyncClient, HTTPStatusError
from itsdangerous import BadSignature, BadTimeSignature, SignatureExpired, URLSafeSerializer
from itsdangerous.url_safe import URLSafeTimedSerializer
from openai import OpenAI
from pydantic import BaseModel, Field
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker
from sqlalchemy.pool import NullPool
from sqlalchemy.types import JSON


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ── Config ──────────────────────────────────────────────────────────────────────


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def utc_now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


DEFAULT_LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://host.docker.internal:1234/v1")
DEFAULT_LLM_MODEL = os.getenv("LLM_MODEL", "qwen3.5-122b-a10b")
DEFAULT_LLM_API_KEY = os.getenv("LLM_API_KEY", "lm-studio")

APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "chronoscape-dev-secret-change-me")
SETTINGS_ENCRYPTION_KEY = os.getenv("SETTINGS_ENCRYPTION_KEY", APP_SECRET_KEY)
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_DISCOVERY_URL = os.getenv(
    "GOOGLE_DISCOVERY_URL",
    "https://accounts.google.com/.well-known/openid-configuration",
)
GOOGLE_ISSUER = os.getenv("GOOGLE_ISSUER", "https://accounts.google.com")
GOOGLE_AUTHORIZATION_ENDPOINT = os.getenv(
    "GOOGLE_AUTHORIZATION_ENDPOINT",
    "https://accounts.google.com/o/oauth2/v2/auth",
)
GOOGLE_TOKEN_ENDPOINT = os.getenv(
    "GOOGLE_TOKEN_ENDPOINT",
    "https://oauth2.googleapis.com/token",
)
GOOGLE_JWKS_URI = os.getenv(
    "GOOGLE_JWKS_URI",
    "https://www.googleapis.com/oauth2/v3/certs",
)
SESSION_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "30"))
AUTH_FLOW_TTL_SECONDS = int(os.getenv("AUTH_FLOW_TTL_SECONDS", "600"))
IS_VERCEL = bool(os.getenv("VERCEL"))
COOKIE_SECURE = env_bool("COOKIE_SECURE", IS_VERCEL)
COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN") or None
ALLOWED_LLM_BASE_URLS = env_list("ALLOWED_LLM_BASE_URLS")
CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS")

SESSION_COOKIE_NAME = "chronoscape_session"
CSRF_COOKIE_NAME = "chronoscape_csrf"
AUTH_FLOW_COOKIE_NAME = "chronoscape_auth_flow"

DATA_DIR = os.getenv("DATA_DIR", "/tmp/data")
os.makedirs(DATA_DIR, exist_ok=True)
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
DB_PATH = os.path.join(DATA_DIR, "timeline.db")
DATABASE_URL = (
    os.getenv("DATABASE_URL")
    or os.getenv("POSTGRES_URL")
    or os.getenv("POSTGRES_URL_NON_POOLING")
)
BACKUP_VERSION = "1"
BACKUP_MARKER_START = "<!-- CHRONOSCAPE_BACKUP_V1_BEGIN -->"
BACKUP_MARKER_END = "<!-- CHRONOSCAPE_BACKUP_V1_END -->"
CSV_FIELDNAMES = [
    "record_type",
    "backup_version",
    "generated_at",
    "backup_key",
    "name",
    "color_hex",
    "start_date",
    "start_date_precision",
    "end_date",
    "end_date_precision",
    "headline",
    "explanation",
    "date",
    "date_precision",
    "sentiment_score",
    "sort_index",
    "era_backup_key",
    "reflection_qa_json",
    "created_at",
]

session_cookie_serializer = URLSafeSerializer(APP_SECRET_KEY, salt="chronoscape.session")
auth_flow_cookie_serializer = URLSafeTimedSerializer(
    APP_SECRET_KEY, salt="chronoscape.auth-flow"
)
google_id_token_jwt = JsonWebToken(["RS256"])


def build_fernet(secret: str) -> Fernet:
    try:
        return Fernet(secret.encode("utf-8"))
    except ValueError:
        derived = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
        return Fernet(derived)


settings_fernet = build_fernet(SETTINGS_ENCRYPTION_KEY)


def normalize_http_url(value: str) -> str:
    candidate = (value or "").strip().rstrip("/")
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(400, "LLM base URL must be a valid http(s) URL")
    return candidate


def base_url_allowed(value: str) -> bool:
    if not ALLOWED_LLM_BASE_URLS:
        return True
    normalized = normalize_http_url(value)
    allowed = {normalize_http_url(item) for item in ALLOWED_LLM_BASE_URLS}
    return normalized in allowed


def mask_secret(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * max(4, len(value) - 4)}{value[-2:]}"


def encrypt_secret(value: str) -> str:
    return settings_fernet.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return settings_fernet.decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        logger.error("Failed to decrypt user setting")
        raise HTTPException(500, "Stored settings could not be decrypted") from exc


def build_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
    return verifier, challenge


def sanitize_next_path(value: str | None) -> str:
    candidate = (value or "/").strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    return candidate


GOOGLE_OIDC_CACHE: dict[str, Any] = {"metadata": None, "jwks": None}


def default_google_metadata() -> dict[str, Any]:
    return {
        "issuer": GOOGLE_ISSUER,
        "authorization_endpoint": GOOGLE_AUTHORIZATION_ENDPOINT,
        "token_endpoint": GOOGLE_TOKEN_ENDPOINT,
        "jwks_uri": GOOGLE_JWKS_URI,
    }


def normalize_database_url(raw: Optional[str]) -> str:
    if raw:
        candidate = raw.strip()
        if candidate.startswith("postgres://"):
            return candidate.replace("postgres://", "postgresql+psycopg://", 1)
        if candidate.startswith("postgresql://"):
            return candidate.replace("postgresql://", "postgresql+psycopg://", 1)
        return candidate
    return f"sqlite:///{DB_PATH}"


RESOLVED_DATABASE_URL = normalize_database_url(DATABASE_URL)
IS_SQLITE = RESOLVED_DATABASE_URL.startswith("sqlite")
IS_POSTGRES = RESOLVED_DATABASE_URL.startswith("postgresql")


# ── Database ────────────────────────────────────────────────────────────────────

engine_kwargs: dict[str, Any] = {}
if IS_SQLITE:
    engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    # Avoid long-lived in-process pools in serverless runtimes; rely on the DB URL's
    # own pooling layer when available.
    engine_kwargs["poolclass"] = NullPool
    engine_kwargs["pool_pre_ping"] = True

engine = create_engine(RESOLVED_DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

logger.info("Using database backend: %s", engine.url.get_backend_name())
if IS_VERCEL and IS_SQLITE:
    logger.warning(
        "Running on Vercel with SQLite at %s. Sessions and data will not persist reliably; "
        "configure DATABASE_URL or Vercel Postgres env vars.",
        DB_PATH,
    )


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    google_sub = Column(String(255), nullable=False, unique=True, index=True)
    email = Column(String(255), nullable=False, index=True)
    email_verified = Column(Boolean, nullable=False, default=False)
    display_name = Column(String(255), nullable=True)
    given_name = Column(String(255), nullable=True)
    family_name = Column(String(255), nullable=True)
    avatar_url = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utc_now_naive)
    updated_at = Column(DateTime, nullable=False, default=utc_now_naive, onupdate=utc_now_naive)

    sessions = relationship("UserSession", back_populates="user", cascade="all, delete-orphan")
    settings = relationship("UserSetting", back_populates="user", uselist=False, cascade="all, delete-orphan")
    events = relationship("Event", back_populates="user")
    eras = relationship("Era", back_populates="user")
    reflections = relationship("LLMReflection", back_populates="user")
    backup_audits = relationship("BackupAudit", back_populates="user")


class UserSession(Base):
    __tablename__ = "user_sessions"

    session_id = Column(String(128), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    csrf_token = Column(String(128), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utc_now_naive)
    expires_at = Column(DateTime, nullable=False)

    user = relationship("User", back_populates="sessions")


class UserSetting(Base):
    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    llm_base_url = Column(Text, nullable=False, default=DEFAULT_LLM_BASE_URL)
    llm_model = Column(String(255), nullable=False, default=DEFAULT_LLM_MODEL)
    llm_api_key_encrypted = Column(Text, nullable=True)
    welcome_dismissed = Column(Boolean, nullable=False, default=False)
    first_event_completed = Column(Boolean, nullable=False, default=False)
    ai_nudge_dismissed = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=utc_now_naive)
    updated_at = Column(DateTime, nullable=False, default=utc_now_naive, onupdate=utc_now_naive)

    user = relationship("User", back_populates="settings")


class Era(Base):
    __tablename__ = "eras"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    name = Column(String(100), nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    start_date_precision = Column(String(10), default="day")
    end_date_precision = Column(String(10), default="day")
    color_hex = Column(String(7), default="#B8C4D4")

    user = relationship("User", back_populates="eras")
    events = relationship("Event", back_populates="era")


class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    headline = Column(String(120), nullable=False)
    explanation = Column(Text, default="")
    date = Column(Date, nullable=False)
    date_precision = Column(String(10), default="day")
    sentiment_score = Column(Integer, nullable=False)
    era_id = Column(Integer, ForeignKey("eras.id"), nullable=True)
    reflection_qa = Column(JSON, nullable=True)
    sort_index = Column(Integer, nullable=False, default=0, index=True)
    created_at = Column(DateTime, default=utc_now_naive)

    user = relationship("User", back_populates="events")
    era = relationship("Era", back_populates="events")


class LLMReflection(Base):
    __tablename__ = "llm_reflections"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="SET NULL"), nullable=True, index=True)
    headline = Column(String(120), nullable=False)
    event_date = Column(Date, nullable=False)
    sentiment_score = Column(Integer, nullable=False)
    questions = Column(JSON, nullable=True)
    answers = Column(JSON, nullable=True)
    reflection = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=utc_now_naive)

    user = relationship("User", back_populates="reflections")


class BackupAudit(Base):
    __tablename__ = "backup_audits"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    action = Column(String(32), nullable=False)
    format = Column(String(16), nullable=False)
    era_count = Column(Integer, nullable=False, default=0)
    event_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=utc_now_naive)

    user = relationship("User", back_populates="backup_audits")


Base.metadata.create_all(bind=engine)


def table_columns(conn, table_name: str) -> set[str]:
    inspector = inspect(conn)
    if not inspector.has_table(table_name):
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def add_column_if_missing(conn, table_name: str, column_name: str, ddl: str) -> None:
    if column_name in table_columns(conn, table_name):
        return
    if conn.dialect.name == "postgresql":
        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {ddl}"))
    else:
        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {ddl}"))
    logger.info("Migrated %s.%s", table_name, column_name)


def run_startup_migrations() -> None:
    with engine.begin() as conn:
        add_column_if_missing(conn, "events", "date_precision", "date_precision VARCHAR(10) DEFAULT 'day'")
        add_column_if_missing(conn, "events", "sort_index", "sort_index INTEGER DEFAULT 0")
        add_column_if_missing(conn, "events", "user_id", "user_id INTEGER")
        add_column_if_missing(conn, "eras", "start_date_precision", "start_date_precision VARCHAR(10) DEFAULT 'day'")
        add_column_if_missing(conn, "eras", "end_date_precision", "end_date_precision VARCHAR(10) DEFAULT 'day'")
        add_column_if_missing(conn, "eras", "user_id", "user_id INTEGER")
        add_column_if_missing(
            conn,
            "user_settings",
            "welcome_dismissed",
            "welcome_dismissed BOOLEAN NOT NULL DEFAULT FALSE",
        )
        add_column_if_missing(
            conn,
            "user_settings",
            "first_event_completed",
            "first_event_completed BOOLEAN NOT NULL DEFAULT FALSE",
        )
        add_column_if_missing(
            conn,
            "user_settings",
            "ai_nudge_dismissed",
            "ai_nudge_dismissed BOOLEAN NOT NULL DEFAULT FALSE",
        )

        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_events_user_id ON events(user_id)"))
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_events_user_sort_index "
                "ON events(user_id, sort_index, id)"
            )
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_eras_user_id ON eras(user_id)"))
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_eras_user_start_date "
                "ON eras(user_id, start_date, id)"
            )
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_llm_reflections_user_created ON llm_reflections(user_id, created_at)")
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_backup_audits_user_created ON backup_audits(user_id, created_at)")
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_user_sessions_expires_at ON user_sessions(expires_at)")
        )
        conn.execute(
            text(
                "UPDATE user_settings SET first_event_completed = TRUE "
                "WHERE first_event_completed = FALSE "
                "AND user_id IN (SELECT DISTINCT user_id FROM events WHERE user_id IS NOT NULL)"
            )
        )


def normalize_event_sort_indexes_for_scope(db: Session, user_id: Optional[int]) -> None:
    query = db.query(Event)
    if user_id is None:
        query = query.filter(Event.user_id.is_(None))
    else:
        query = query.filter(Event.user_id == user_id)

    ordered_events = query.order_by(Event.sort_index, Event.date, Event.id).all()
    seen_indexes: set[int] = set()
    needs_reindex = False

    for expected_index, event in enumerate(ordered_events):
        if event.sort_index is None or event.sort_index in seen_indexes:
            needs_reindex = True
            break
        seen_indexes.add(event.sort_index)
        if event.sort_index != expected_index:
            needs_reindex = True
            break

    if not needs_reindex:
        return

    fallback = query.order_by(Event.date, Event.id).all()
    for index, event in enumerate(fallback):
        event.sort_index = index


def normalize_all_event_sort_indexes() -> None:
    db = SessionLocal()
    try:
        user_ids = [
            row[0]
            for row in db.query(Event.user_id).distinct().all()
        ]
        for user_id in user_ids:
            normalize_event_sort_indexes_for_scope(db, user_id)
        db.commit()
    finally:
        db.close()


def cleanup_expired_sessions() -> None:
    db = SessionLocal()
    try:
        db.query(UserSession).filter(UserSession.expires_at <= utc_now_naive()).delete(
            synchronize_session=False
        )
        db.commit()
    finally:
        db.close()


run_startup_migrations()
normalize_all_event_sort_indexes()
cleanup_expired_sessions()


# ── Schemas ─────────────────────────────────────────────────────────────────────


class EraCreate(BaseModel):
    name: str
    start_date: date_type
    end_date: date_type
    start_date_precision: str = "day"
    end_date_precision: str = "day"
    color_hex: str = "#B8C4D4"


class EraUpdate(BaseModel):
    name: Optional[str] = None
    start_date: Optional[date_type] = None
    end_date: Optional[date_type] = None
    start_date_precision: Optional[str] = None
    end_date_precision: Optional[str] = None
    color_hex: Optional[str] = None


class EraOut(BaseModel):
    id: int
    name: str
    start_date: date_type
    end_date: date_type
    start_date_precision: str
    end_date_precision: str
    color_hex: str

    model_config = {"from_attributes": True}


class EventCreate(BaseModel):
    headline: str = Field(max_length=120)
    explanation: str = ""
    date: date_type
    date_precision: str = "day"
    sentiment_score: int = Field(ge=-5, le=5)
    era_id: Optional[int] = None
    reflection_qa: Optional[dict] = None
    sort_index: Optional[int] = None


class EventUpdate(BaseModel):
    headline: Optional[str] = Field(None, max_length=120)
    explanation: Optional[str] = None
    date: Optional[date_type] = None
    date_precision: Optional[str] = None
    sentiment_score: Optional[int] = Field(None, ge=-5, le=5)
    era_id: Optional[int] = None
    reflection_qa: Optional[dict] = None
    sort_index: Optional[int] = None


class EventOut(BaseModel):
    id: int
    headline: str
    explanation: str
    date: date_type
    date_precision: str
    sentiment_score: int
    era_id: Optional[int]
    reflection_qa: Optional[dict]
    sort_index: int
    created_at: datetime

    model_config = {"from_attributes": True}


class EventReorderRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)


class ProbeReq(BaseModel):
    headline: str
    date: date_type
    sentiment_score: int


class SynthReq(BaseModel):
    headline: str
    date: date_type
    sentiment_score: int
    questions: list[str]
    answers: list[str]


class BackupEraRecord(BaseModel):
    backup_key: str
    name: str
    color_hex: str = "#B8C4D4"
    start_date: date_type
    start_date_precision: str = "day"
    end_date: date_type
    end_date_precision: str = "day"


class BackupEventRecord(BaseModel):
    headline: str = Field(max_length=120)
    explanation: str = ""
    date: date_type
    date_precision: str = "day"
    sentiment_score: int = Field(ge=-5, le=5)
    sort_index: Optional[int] = None
    era_backup_key: Optional[str] = None
    reflection_qa: Optional[dict] = None
    created_at: Optional[datetime] = None


class BackupPayload(BaseModel):
    backup_version: str
    generated_at: datetime
    eras: list[BackupEraRecord] = Field(default_factory=list)
    events: list[BackupEventRecord] = Field(default_factory=list)


class SettingsUpdate(BaseModel):
    llm_base_url: Optional[str] = None
    llm_model: Optional[str] = None
    llm_api_key: Optional[str] = None
    clear_llm_api_key: bool = False


class OnboardingUpdate(BaseModel):
    welcome_dismissed: Optional[bool] = None
    ai_nudge_dismissed: Optional[bool] = None


class LLMHealthRequest(BaseModel):
    llm_base_url: Optional[str] = None
    llm_model: Optional[str] = None


# ── Dependencies and helpers ───────────────────────────────────────────────────


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def default_setting_payload() -> dict[str, str]:
    return {
        "llm_base_url": normalize_http_url(DEFAULT_LLM_BASE_URL),
        "llm_model": DEFAULT_LLM_MODEL,
        "llm_api_key": DEFAULT_LLM_API_KEY,
    }


def load_legacy_settings_file() -> dict[str, str]:
    settings = default_setting_payload()
    if not os.path.exists(SETTINGS_PATH):
        return settings

    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception as exc:
        logger.warning("Could not read legacy settings.json: %s", exc)
        return settings

    if isinstance(raw, dict):
        if raw.get("llm_base_url"):
            settings["llm_base_url"] = normalize_http_url(raw["llm_base_url"])
        if raw.get("llm_model"):
            settings["llm_model"] = str(raw["llm_model"]).strip()
        if raw.get("llm_api_key"):
            settings["llm_api_key"] = str(raw["llm_api_key"])
    return settings


def get_or_create_user_settings(db: Session, user_id: int) -> UserSetting:
    settings = db.query(UserSetting).filter(UserSetting.user_id == user_id).first()
    if settings:
        if (
            not settings.first_event_completed
            and db.query(Event.id).filter(Event.user_id == user_id).first() is not None
        ):
            settings.first_event_completed = True
            settings.updated_at = utc_now_naive()
            db.flush()
        return settings

    defaults = default_setting_payload()
    settings = UserSetting(
        user_id=user_id,
        llm_base_url=defaults["llm_base_url"],
        llm_model=defaults["llm_model"],
        llm_api_key_encrypted=encrypt_secret(defaults["llm_api_key"])
        if defaults.get("llm_api_key")
        else None,
        first_event_completed=db.query(Event.id).filter(Event.user_id == user_id).first() is not None,
    )
    db.add(settings)
    db.flush()
    return settings


def serialize_onboarding(settings: UserSetting) -> dict[str, bool]:
    return {
        "welcome_dismissed": bool(settings.welcome_dismissed),
        "first_event_completed": bool(settings.first_event_completed),
        "ai_nudge_dismissed": bool(settings.ai_nudge_dismissed),
    }


def serialize_user_settings(settings: UserSetting) -> dict[str, Any]:
    plain_key = decrypt_secret(settings.llm_api_key_encrypted)
    return {
        "llm_base_url": settings.llm_base_url,
        "llm_model": settings.llm_model,
        "llm_api_key_set": bool(plain_key),
        "llm_api_key_masked": mask_secret(plain_key),
    }


def resolve_user_llm_settings(db: Session, user_id: int) -> dict[str, str]:
    settings = get_or_create_user_settings(db, user_id)
    return {
        "llm_base_url": settings.llm_base_url,
        "llm_model": settings.llm_model,
        "llm_api_key": decrypt_secret(settings.llm_api_key_encrypted) or DEFAULT_LLM_API_KEY,
    }


def record_backup_audit(
    db: Session,
    user_id: int,
    action: str,
    fmt: str,
    era_count: int,
    event_count: int,
) -> None:
    db.add(
        BackupAudit(
            user_id=user_id,
            action=action,
            format=fmt,
            era_count=era_count,
            event_count=event_count,
        )
    )


def sign_session_cookie_value(session_id: str) -> str:
    return session_cookie_serializer.dumps(session_id)


def load_session_cookie_value(value: str) -> str:
    try:
        return session_cookie_serializer.loads(value)
    except BadSignature as exc:
        raise HTTPException(401, "Invalid session cookie") from exc


def set_cookie(response, name: str, value: str, *, max_age: int, httponly: bool) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        expires=max_age,
        path="/",
        domain=COOKIE_DOMAIN,
        secure=COOKIE_SECURE,
        httponly=httponly,
        samesite="lax",
    )


def delete_cookie(response, name: str, *, httponly: bool) -> None:
    response.delete_cookie(
        name,
        path="/",
        domain=COOKIE_DOMAIN,
        secure=COOKIE_SECURE,
        httponly=httponly,
        samesite="lax",
    )


def set_auth_cookies(response, session: UserSession) -> None:
    max_age = max(0, int((session.expires_at - utc_now_naive()).total_seconds()))
    set_cookie(
        response,
        SESSION_COOKIE_NAME,
        sign_session_cookie_value(session.session_id),
        max_age=max_age,
        httponly=True,
    )
    set_cookie(
        response,
        CSRF_COOKIE_NAME,
        session.csrf_token,
        max_age=max_age,
        httponly=False,
    )


def clear_auth_cookies(response) -> None:
    delete_cookie(response, SESSION_COOKIE_NAME, httponly=True)
    delete_cookie(response, CSRF_COOKIE_NAME, httponly=False)
    delete_cookie(response, AUTH_FLOW_COOKIE_NAME, httponly=True)


def create_user_session(db: Session, user_id: int) -> UserSession:
    session = UserSession(
        session_id=secrets.token_urlsafe(48),
        user_id=user_id,
        csrf_token=secrets.token_urlsafe(32),
        expires_at=utc_now_naive() + timedelta(days=SESSION_TTL_DAYS),
    )
    db.add(session)
    db.flush()
    return session


def get_current_session(request: Request, db: Session = Depends(get_db)) -> UserSession:
    signed_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not signed_cookie:
        raise HTTPException(401, "Authentication required")

    session_id = load_session_cookie_value(signed_cookie)
    session = db.query(UserSession).filter(UserSession.session_id == session_id).first()
    if not session:
        raise HTTPException(401, "Session not found")
    if session.expires_at <= utc_now_naive():
        db.delete(session)
        db.commit()
        raise HTTPException(401, "Session expired")
    if not session.user:
        db.delete(session)
        db.commit()
        raise HTTPException(401, "Session user no longer exists")
    return session


def get_current_user(session: UserSession = Depends(get_current_session)) -> User:
    return session.user


def require_csrf(
    request: Request,
    session: UserSession = Depends(get_current_session),
) -> None:
    header_token = request.headers.get("X-CSRF-Token")
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    expected = session.csrf_token
    if not header_token or not cookie_token:
        raise HTTPException(403, "Missing CSRF token")
    if not (
        secrets.compare_digest(header_token, cookie_token)
        and secrets.compare_digest(header_token, expected)
    ):
        raise HTTPException(403, "Invalid CSRF token")


def get_owned_era(db: Session, user_id: int, era_id: int) -> Era:
    era = db.query(Era).filter(Era.id == era_id, Era.user_id == user_id).first()
    if not era:
        raise HTTPException(400, "Selected era does not belong to the current user")
    return era


def get_owned_event(db: Session, user_id: int, event_id: int) -> Event:
    event = db.query(Event).filter(Event.id == event_id, Event.user_id == user_id).first()
    if not event:
        raise HTTPException(404, "Event not found")
    return event


def get_owned_era_or_404(db: Session, user_id: int, era_id: int) -> Era:
    era = db.query(Era).filter(Era.id == era_id, Era.user_id == user_id).first()
    if not era:
        raise HTTPException(404, "Era not found")
    return era


def attach_or_create_reflection_history(
    db: Session,
    user_id: int,
    event: Event,
    questions: list[str],
    answers: list[str],
) -> None:
    candidates = (
        db.query(LLMReflection)
        .filter(
            LLMReflection.user_id == user_id,
            LLMReflection.event_id.is_(None),
            LLMReflection.headline == event.headline,
            LLMReflection.event_date == event.date,
            LLMReflection.sentiment_score == event.sentiment_score,
        )
        .order_by(LLMReflection.created_at.desc(), LLMReflection.id.desc())
        .limit(10)
        .all()
    )

    for candidate in candidates:
        if (candidate.questions or []) == questions and (candidate.answers or []) == answers:
            candidate.event_id = event.id
            candidate.reflection = event.explanation or candidate.reflection
            return

    db.add(
        LLMReflection(
            user_id=user_id,
            event_id=event.id,
            headline=event.headline,
            event_date=event.date,
            sentiment_score=event.sentiment_score,
            questions=questions,
            answers=answers,
            reflection=event.explanation or "",
        )
    )


def parse_llm_json(raw: str) -> dict:
    text_value = raw.strip()
    text_value = re.sub(r"^```(?:json)?\s*", "", text_value)
    text_value = re.sub(r"\s*```$", "", text_value)
    text_value = re.sub(r"<think>.*?</think>", "", text_value, flags=re.DOTALL)
    text_value = text_value.strip()
    start = text_value.find("{")
    end = text_value.rfind("}")
    if start != -1 and end > start:
        text_value = text_value[start : end + 1]
    try:
        return json.loads(text_value)
    except json.JSONDecodeError:
        logger.error("LLM JSON parse failure — raw output: %s", raw)
        raise HTTPException(502, "LLM returned malformed JSON")


def llm_client(settings: dict[str, str]) -> OpenAI:
    return OpenAI(base_url=settings["llm_base_url"], api_key=settings["llm_api_key"])


# ── Backup helpers ─────────────────────────────────────────────────────────────


def _backup_era_key(era_id: int) -> str:
    return f"era-{era_id}"


def build_backup_payload(db: Session, user_id: int) -> BackupPayload:
    generated_at = utc_now_naive()
    eras = (
        db.query(Era)
        .filter(Era.user_id == user_id)
        .order_by(Era.start_date, Era.id)
        .all()
    )
    events = (
        db.query(Event)
        .filter(Event.user_id == user_id)
        .order_by(Event.sort_index, Event.id)
        .all()
    )
    era_keys = {era.id: _backup_era_key(era.id) for era in eras}

    return BackupPayload(
        backup_version=BACKUP_VERSION,
        generated_at=generated_at,
        eras=[
            BackupEraRecord(
                backup_key=era_keys[era.id],
                name=era.name,
                color_hex=era.color_hex,
                start_date=era.start_date,
                start_date_precision=era.start_date_precision,
                end_date=era.end_date,
                end_date_precision=era.end_date_precision,
            )
            for era in eras
        ],
        events=[
            BackupEventRecord(
                headline=event.headline,
                explanation=event.explanation or "",
                date=event.date,
                date_precision=event.date_precision,
                sentiment_score=event.sentiment_score,
                sort_index=event.sort_index,
                era_backup_key=era_keys.get(event.era_id),
                reflection_qa=event.reflection_qa,
                created_at=event.created_at,
            )
            for event in events
        ],
    )


def build_backup_csv(payload: BackupPayload) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDNAMES)
    writer.writeheader()
    writer.writerow(
        {
            "record_type": "meta",
            "backup_version": payload.backup_version,
            "generated_at": payload.generated_at.isoformat(),
        }
    )

    for era in payload.eras:
        writer.writerow(
            {
                "record_type": "era",
                "backup_key": era.backup_key,
                "name": era.name,
                "color_hex": era.color_hex,
                "start_date": era.start_date.isoformat(),
                "start_date_precision": era.start_date_precision,
                "end_date": era.end_date.isoformat(),
                "end_date_precision": era.end_date_precision,
            }
        )

    for event in payload.events:
        writer.writerow(
            {
                "record_type": "event",
                "headline": event.headline,
                "explanation": event.explanation,
                "date": event.date.isoformat(),
                "date_precision": event.date_precision,
                "sentiment_score": event.sentiment_score,
                "sort_index": event.sort_index,
                "era_backup_key": event.era_backup_key or "",
                "reflection_qa_json": (
                    json.dumps(event.reflection_qa, ensure_ascii=False)
                    if event.reflection_qa is not None
                    else ""
                ),
                "created_at": event.created_at.isoformat() if event.created_at else "",
            }
        )

    return buffer.getvalue()


def build_backup_markdown(payload: BackupPayload) -> str:
    backup_json = json.dumps(payload.model_dump(mode="json"), indent=2, ensure_ascii=False)
    return (
        "# Chronoscape Backup\n\n"
        f"- Backup version: `{payload.backup_version}`\n"
        f"- Generated at: `{payload.generated_at.isoformat()}`\n"
        f"- Eras: `{len(payload.eras)}`\n"
        f"- Events: `{len(payload.events)}`\n\n"
        "This file can be restored by Chronoscape. The machine-readable payload is embedded below.\n\n"
        f"{BACKUP_MARKER_START}\n"
        f"{backup_json}\n"
        f"{BACKUP_MARKER_END}\n"
    )


def _validate_backup_version(value: str) -> None:
    if str(value) != BACKUP_VERSION:
        raise HTTPException(400, f"Unsupported backup version: {value}")


def parse_backup_csv(text_value: str) -> BackupPayload:
    try:
        reader = csv.DictReader(io.StringIO(text_value))
    except csv.Error as exc:
        raise HTTPException(400, f"Invalid CSV backup: {exc}") from exc

    if not reader.fieldnames:
        raise HTTPException(400, "CSV backup is missing a header row")

    meta = None
    eras = []
    events = []

    try:
        for row in reader:
            record_type = (row.get("record_type") or "").strip()
            if not record_type:
                continue
            if record_type == "meta":
                if meta is not None:
                    raise HTTPException(400, "CSV backup contains multiple meta rows")
                meta = row
                continue
            if record_type == "era":
                eras.append(
                    {
                        "backup_key": row.get("backup_key") or "",
                        "name": row.get("name") or "",
                        "color_hex": row.get("color_hex") or "#B8C4D4",
                        "start_date": row.get("start_date") or "",
                        "start_date_precision": row.get("start_date_precision") or "day",
                        "end_date": row.get("end_date") or "",
                        "end_date_precision": row.get("end_date_precision") or "day",
                    }
                )
                continue
            if record_type == "event":
                reflection_raw = row.get("reflection_qa_json") or ""
                events.append(
                    {
                        "headline": row.get("headline") or "",
                        "explanation": row.get("explanation") or "",
                        "date": row.get("date") or "",
                        "date_precision": row.get("date_precision") or "day",
                        "sentiment_score": row.get("sentiment_score"),
                        "sort_index": row.get("sort_index") or None,
                        "era_backup_key": row.get("era_backup_key") or None,
                        "reflection_qa": json.loads(reflection_raw) if reflection_raw else None,
                        "created_at": row.get("created_at") or None,
                    }
                )
                continue
            raise HTTPException(400, f"Unknown CSV record_type: {record_type}")
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "CSV backup contains invalid reflection_qa_json") from exc
    except csv.Error as exc:
        raise HTTPException(400, f"Invalid CSV backup: {exc}") from exc

    if meta is None:
        raise HTTPException(400, "CSV backup is missing the meta row")

    _validate_backup_version(meta.get("backup_version"))

    try:
        return BackupPayload.model_validate(
            {
                "backup_version": str(meta.get("backup_version")),
                "generated_at": meta.get("generated_at"),
                "eras": eras,
                "events": events,
            }
        )
    except Exception as exc:
        raise HTTPException(400, f"Invalid CSV backup payload: {exc}") from exc


def parse_backup_markdown(text_value: str) -> BackupPayload:
    pattern = re.escape(BACKUP_MARKER_START) + r"\s*(.*?)\s*" + re.escape(BACKUP_MARKER_END)
    match = re.search(pattern, text_value, flags=re.DOTALL)
    if not match:
        raise HTTPException(400, "Markdown backup is missing the embedded payload block")

    payload_raw = match.group(1).strip()
    try:
        payload_data = json.loads(payload_raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "Markdown backup contains invalid embedded JSON") from exc

    try:
        payload = BackupPayload.model_validate(payload_data)
    except Exception as exc:
        raise HTTPException(400, f"Invalid Markdown backup payload: {exc}") from exc

    _validate_backup_version(payload.backup_version)
    return payload


def parse_backup_upload(filename: str, text_value: str) -> BackupPayload:
    lower_name = (filename or "").lower()
    if lower_name.endswith(".csv"):
        return parse_backup_csv(text_value)
    if lower_name.endswith(".md"):
        return parse_backup_markdown(text_value)
    raise HTTPException(400, "Unsupported backup file type. Use a .csv or .md backup.")


def restore_backup_payload(payload: BackupPayload, db: Session, user_id: int) -> dict[str, int]:
    _validate_backup_version(payload.backup_version)
    era_id_map: dict[str, int] = {}

    db.query(LLMReflection).filter(LLMReflection.user_id == user_id).delete(synchronize_session=False)
    db.query(Event).filter(Event.user_id == user_id).delete(synchronize_session=False)
    db.query(Era).filter(Era.user_id == user_id).delete(synchronize_session=False)

    for era in payload.eras:
        restored_era = Era(
            user_id=user_id,
            name=era.name,
            color_hex=era.color_hex,
            start_date=era.start_date,
            start_date_precision=era.start_date_precision,
            end_date=era.end_date,
            end_date_precision=era.end_date_precision,
        )
        db.add(restored_era)
        db.flush()
        era_id_map[era.backup_key] = restored_era.id

    for index, event in enumerate(payload.events):
        if event.era_backup_key and event.era_backup_key not in era_id_map:
            raise HTTPException(
                400,
                f"Event references unknown era backup key: {event.era_backup_key}",
            )

        restored_event = Event(
            user_id=user_id,
            headline=event.headline,
            explanation=event.explanation,
            date=event.date,
            date_precision=event.date_precision,
            sentiment_score=event.sentiment_score,
            sort_index=event.sort_index if event.sort_index is not None else index,
            era_id=era_id_map.get(event.era_backup_key),
            reflection_qa=event.reflection_qa,
            created_at=event.created_at or utc_now_naive(),
        )
        db.add(restored_event)
        db.flush()

        if event.reflection_qa:
            db.add(
                LLMReflection(
                    user_id=user_id,
                    event_id=restored_event.id,
                    headline=restored_event.headline,
                    event_date=restored_event.date,
                    sentiment_score=restored_event.sentiment_score,
                    questions=event.reflection_qa.get("questions") or [],
                    answers=event.reflection_qa.get("answers") or [],
                    reflection=restored_event.explanation or "",
                )
            )

    normalize_event_sort_indexes_for_scope(db, user_id)
    return {"eras_restored": len(payload.eras), "events_restored": len(payload.events)}


# ── Google OIDC helpers ────────────────────────────────────────────────────────


def require_google_oauth_config() -> None:
    if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
        return
    missing = []
    if not GOOGLE_CLIENT_ID:
        missing.append("GOOGLE_CLIENT_ID")
    if not GOOGLE_CLIENT_SECRET:
        missing.append("GOOGLE_CLIENT_SECRET")
    raise HTTPException(
        503,
        f"Google authentication is not configured. Missing: {', '.join(missing)}",
    )


async def fetch_google_metadata() -> dict[str, Any]:
    cached = GOOGLE_OIDC_CACHE.get("metadata")
    if cached:
        return cached

    metadata = default_google_metadata()
    discovery_url = (GOOGLE_DISCOVERY_URL or "").strip()
    if not discovery_url:
        GOOGLE_OIDC_CACHE["metadata"] = metadata
        return metadata

    try:
        async with AsyncClient() as client:
            response = await client.get(discovery_url)
            response.raise_for_status()
            discovered = response.json()
        metadata.update(
            {
                "issuer": discovered.get("issuer") or metadata["issuer"],
                "authorization_endpoint": discovered.get("authorization_endpoint")
                or metadata["authorization_endpoint"],
                "token_endpoint": discovered.get("token_endpoint") or metadata["token_endpoint"],
                "jwks_uri": discovered.get("jwks_uri") or metadata["jwks_uri"],
            }
        )
    except Exception as exc:
        logger.warning("Falling back to static Google OIDC metadata: %s", exc)

    GOOGLE_OIDC_CACHE["metadata"] = metadata
    return metadata


async def fetch_google_jwks(metadata: dict[str, Any]) -> dict[str, Any]:
    cached = GOOGLE_OIDC_CACHE.get("jwks")
    if cached:
        return cached

    async with AsyncClient() as client:
        response = await client.get(metadata["jwks_uri"])
        response.raise_for_status()
        jwks = response.json()
    GOOGLE_OIDC_CACHE["jwks"] = jwks
    return jwks


async def exchange_google_code_for_token(
    *,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    metadata = await fetch_google_metadata()

    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "code_verifier": code_verifier,
    }

    try:
        async with AsyncClient() as client:
            response = await client.post(
                metadata["token_endpoint"],
                data=payload,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            token = response.json()
    except HTTPStatusError as exc:
        detail = exc.response.text
        logger.error("Google token exchange failed: %s", detail)
        raise HTTPException(400, f"Google token exchange failed: {detail}") from exc
    return token, metadata


async def validate_google_id_token(
    token: dict[str, Any],
    metadata: dict[str, Any],
    nonce: str,
) -> dict[str, Any]:
    id_token = token.get("id_token")
    if not id_token:
        raise HTTPException(400, "Google response did not include an ID token")

    jwks = await fetch_google_jwks(metadata)
    claims = google_id_token_jwt.decode(
        id_token,
        key=jwks,
        claims_options={
            "iss": {
                "essential": True,
                "values": [metadata.get("issuer"), "https://accounts.google.com", "accounts.google.com"],
            },
            "aud": {"essential": True, "values": [GOOGLE_CLIENT_ID]},
            "sub": {"essential": True},
            "exp": {"essential": True},
            "nonce": {"essential": True, "value": nonce},
        },
    )
    claims.validate(leeway=30)
    if claims.get("nonce") != nonce:
        raise HTTPException(400, "ID token nonce mismatch")
    return dict(claims)


def upsert_user_from_google_claims(db: Session, claims: dict[str, Any]) -> tuple[User, bool]:
    google_sub = str(claims["sub"])
    user = db.query(User).filter(User.google_sub == google_sub).first()
    should_claim_legacy = user is None and db.query(User).count() == 0

    if user is None:
        user = User(
            google_sub=google_sub,
            email=claims.get("email") or f"{google_sub}@example.invalid",
        )
        db.add(user)

    user.email = claims.get("email") or user.email
    user.email_verified = bool(claims.get("email_verified"))
    user.display_name = claims.get("name")
    user.given_name = claims.get("given_name")
    user.family_name = claims.get("family_name")
    user.avatar_url = claims.get("picture")
    user.updated_at = utc_now_naive()

    db.flush()
    return user, should_claim_legacy


def claim_legacy_state(db: Session, user_id: int) -> None:
    legacy_settings = load_legacy_settings_file()
    settings = db.query(UserSetting).filter(UserSetting.user_id == user_id).first()
    if settings is None:
        settings = UserSetting(
            user_id=user_id,
            llm_base_url=legacy_settings["llm_base_url"],
            llm_model=legacy_settings["llm_model"],
            llm_api_key_encrypted=encrypt_secret(legacy_settings["llm_api_key"])
            if legacy_settings.get("llm_api_key")
            else None,
        )
        db.add(settings)

    db.query(Era).filter(Era.user_id.is_(None)).update(
        {Era.user_id: user_id},
        synchronize_session=False,
    )
    db.query(Event).filter(Event.user_id.is_(None)).update(
        {Event.user_id: user_id},
        synchronize_session=False,
    )
    normalize_event_sort_indexes_for_scope(db, user_id)


# ── FastAPI app ─────────────────────────────────────────────────────────────────

app = FastAPI(title="Chronoscape")

if CORS_ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["Content-Type", "X-CSRF-Token"],
    )


@app.middleware("http")
async def harden_responses(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if request.url.path.startswith("/auth/"):
        response.headers["Cache-Control"] = "no-store"
    return response


# ── Auth routes ────────────────────────────────────────────────────────────────


@app.get("/auth/login")
async def auth_login(request: Request):
    require_google_oauth_config()
    metadata = await fetch_google_metadata()

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    code_verifier, code_challenge = build_pkce_pair()
    next_path = sanitize_next_path(request.query_params.get("next"))
    redirect_uri = str(request.url_for("auth_callback"))

    auth_query = urlencode(
        {
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
    )
    authorization_endpoint = metadata.get("authorization_endpoint") or GOOGLE_AUTHORIZATION_ENDPOINT
    auth_url = f"{authorization_endpoint}?{auth_query}"

    response = RedirectResponse(auth_url, status_code=303)
    flow_cookie = auth_flow_cookie_serializer.dumps(
        {
            "state": state,
            "nonce": nonce,
            "code_verifier": code_verifier,
            "next_path": next_path,
        }
    )
    set_cookie(
        response,
        AUTH_FLOW_COOKIE_NAME,
        flow_cookie,
        max_age=AUTH_FLOW_TTL_SECONDS,
        httponly=True,
    )
    return response


@app.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request, db: Session = Depends(get_db)):
    flow_cookie = request.cookies.get(AUTH_FLOW_COOKIE_NAME)
    if not flow_cookie:
        raise HTTPException(400, "Missing OAuth flow cookie")

    try:
        flow = auth_flow_cookie_serializer.loads(flow_cookie, max_age=AUTH_FLOW_TTL_SECONDS)
    except SignatureExpired as exc:
        raise HTTPException(400, "OAuth flow cookie expired") from exc
    except (BadTimeSignature, BadSignature) as exc:
        raise HTTPException(400, "Invalid OAuth flow cookie") from exc

    error = request.query_params.get("error")
    if error:
        raise HTTPException(400, f"Google login failed: {error}")

    code = request.query_params.get("code")
    returned_state = request.query_params.get("state")
    if not code or not returned_state:
        raise HTTPException(400, "OAuth callback is missing code or state")
    if returned_state != flow.get("state"):
        raise HTTPException(400, "OAuth state mismatch")

    redirect_uri = str(request.url_for("auth_callback"))

    try:
        token, metadata = await exchange_google_code_for_token(
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=flow["code_verifier"],
        )
        claims = await validate_google_id_token(token, metadata, flow["nonce"])
        user, should_claim_legacy = upsert_user_from_google_claims(db, claims)
        if should_claim_legacy:
            claim_legacy_state(db, user.id)
        else:
            get_or_create_user_settings(db, user.id)

        session = create_user_session(db, user.id)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        logger.exception("OAuth callback failed")
        raise HTTPException(502, f"OAuth callback failed: {exc}") from exc

    response = RedirectResponse(sanitize_next_path(flow.get("next_path")), status_code=303)
    set_auth_cookies(response, session)
    delete_cookie(response, AUTH_FLOW_COOKIE_NAME, httponly=True)
    return response


@app.get("/auth/me")
def auth_me(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    settings = get_or_create_user_settings(db, current_user.id)
    db.commit()
    return {
        "authenticated": True,
        "user": {
            "id": current_user.id,
            "email": current_user.email,
            "display_name": current_user.display_name or current_user.email,
            "avatar_url": current_user.avatar_url,
        },
        "onboarding": serialize_onboarding(settings),
    }


@app.post("/auth/logout")
def auth_logout(
    current_session: UserSession = Depends(get_current_session),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    db.delete(current_session)
    db.commit()
    response = JSONResponse({"ok": True})
    clear_auth_cookies(response)
    return response


# ── Event endpoints ─────────────────────────────────────────────────────────────


@app.get("/api/events", response_model=list[EventOut])
def list_events(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return (
        db.query(Event)
        .filter(Event.user_id == current_user.id)
        .order_by(Event.sort_index, Event.id)
        .all()
    )


@app.post("/api/events", response_model=EventOut, status_code=201)
def create_event(
    body: EventCreate,
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    payload = body.model_dump()
    if payload["era_id"] is not None:
        get_owned_era(db, current_user.id, payload["era_id"])

    if payload["sort_index"] is None:
        last_event = (
            db.query(Event)
            .filter(Event.user_id == current_user.id)
            .order_by(Event.sort_index.desc(), Event.id.desc())
            .first()
        )
        payload["sort_index"] = 0 if last_event is None else last_event.sort_index + 1

    event = Event(user_id=current_user.id, **payload)
    db.add(event)
    db.flush()

    reflection_qa = payload.get("reflection_qa") or {}
    questions = reflection_qa.get("questions") or []
    answers = reflection_qa.get("answers") or []
    if questions or answers:
        attach_or_create_reflection_history(db, current_user.id, event, questions, answers)

    settings = get_or_create_user_settings(db, current_user.id)
    if not settings.first_event_completed:
        settings.first_event_completed = True
        settings.updated_at = utc_now_naive()

    db.commit()
    db.refresh(event)
    return event


@app.put("/api/events/{eid}", response_model=EventOut)
def update_event(
    eid: int,
    body: EventUpdate,
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    event = get_owned_event(db, current_user.id, eid)
    updates = body.model_dump(exclude_unset=True)
    if "era_id" in updates and updates["era_id"] is not None:
        get_owned_era(db, current_user.id, updates["era_id"])

    for key, value in updates.items():
        setattr(event, key, value)

    reflection_qa = updates.get("reflection_qa") or {}
    questions = reflection_qa.get("questions") or []
    answers = reflection_qa.get("answers") or []
    if questions or answers:
        attach_or_create_reflection_history(db, current_user.id, event, questions, answers)

    db.commit()
    db.refresh(event)
    return event


@app.post("/api/events/reorder", response_model=list[EventOut])
def reorder_events(
    body: EventReorderRequest,
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    current_events = (
        db.query(Event)
        .filter(Event.user_id == current_user.id)
        .order_by(Event.sort_index, Event.id)
        .all()
    )
    current_ids = [event.id for event in current_events]

    if sorted(body.ids) != sorted(current_ids):
        raise HTTPException(400, "Reorder request must include every event exactly once")

    event_map = {event.id: event for event in current_events}
    for index, event_id in enumerate(body.ids):
        event_map[event_id].sort_index = index

    db.commit()
    return (
        db.query(Event)
        .filter(Event.user_id == current_user.id)
        .order_by(Event.sort_index, Event.id)
        .all()
    )


@app.delete("/api/events/{eid}", status_code=204)
def delete_event(
    eid: int,
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    event = get_owned_event(db, current_user.id, eid)
    db.query(LLMReflection).filter(
        LLMReflection.user_id == current_user.id,
        LLMReflection.event_id == event.id,
    ).delete(synchronize_session=False)
    db.delete(event)
    db.commit()
    return None


# ── Era endpoints ───────────────────────────────────────────────────────────────


@app.get("/api/eras", response_model=list[EraOut])
def list_eras(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return (
        db.query(Era)
        .filter(Era.user_id == current_user.id)
        .order_by(Era.start_date, Era.id)
        .all()
    )


@app.post("/api/eras", response_model=EraOut, status_code=201)
def create_era(
    body: EraCreate,
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    era = Era(user_id=current_user.id, **body.model_dump())
    db.add(era)
    db.commit()
    db.refresh(era)
    return era


@app.put("/api/eras/{eid}", response_model=EraOut)
def update_era(
    eid: int,
    body: EraUpdate,
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    era = get_owned_era_or_404(db, current_user.id, eid)
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(era, key, value)
    db.commit()
    db.refresh(era)
    return era


@app.delete("/api/eras/{eid}", status_code=204)
def delete_era(
    eid: int,
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    era = get_owned_era_or_404(db, current_user.id, eid)
    db.query(Event).filter(
        Event.user_id == current_user.id,
        Event.era_id == era.id,
    ).update({Event.era_id: None}, synchronize_session=False)
    db.delete(era)
    db.commit()
    return None


# ── Settings endpoints ──────────────────────────────────────────────────────────


@app.get("/api/settings")
def get_settings(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settings = get_or_create_user_settings(db, current_user.id)
    db.commit()
    return serialize_user_settings(settings)


@app.put("/api/settings")
def put_settings(
    body: SettingsUpdate,
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    settings = get_or_create_user_settings(db, current_user.id)

    if body.llm_base_url is not None:
        normalized_url = normalize_http_url(body.llm_base_url)
        if ALLOWED_LLM_BASE_URLS and not base_url_allowed(normalized_url):
            raise HTTPException(400, "LLM base URL is not in the allowed list")
        settings.llm_base_url = normalized_url

    if body.llm_model is not None:
        model_value = body.llm_model.strip()
        if not model_value:
            raise HTTPException(400, "LLM model must not be blank")
        settings.llm_model = model_value

    if body.llm_api_key is not None and body.llm_api_key.strip():
        settings.llm_api_key_encrypted = encrypt_secret(body.llm_api_key.strip())
    elif body.clear_llm_api_key:
        settings.llm_api_key_encrypted = None

    settings.updated_at = utc_now_naive()
    db.commit()
    db.refresh(settings)
    return serialize_user_settings(settings)


@app.put("/api/onboarding")
def put_onboarding(
    body: OnboardingUpdate,
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    settings = get_or_create_user_settings(db, current_user.id)

    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(settings, key, value)

    settings.updated_at = utc_now_naive()
    db.commit()
    db.refresh(settings)
    return serialize_onboarding(settings)


@app.get("/api/backup")
def download_backup(
    format: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    fmt = (format or "").lower()
    if fmt not in {"csv", "md"}:
        raise HTTPException(400, "Unsupported backup format. Use csv or md.")

    payload = build_backup_payload(db, current_user.id)
    record_backup_audit(db, current_user.id, "backup", fmt, len(payload.eras), len(payload.events))
    db.commit()

    filename = f"chronoscape-backup-{utc_now_naive().date().isoformat()}.{fmt}"
    if fmt == "csv":
        content = build_backup_csv(payload).encode("utf-8")
        media_type = "text/csv; charset=utf-8"
    else:
        content = build_backup_markdown(payload).encode("utf-8")
        media_type = "text/markdown; charset=utf-8"

    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(io.BytesIO(content), media_type=media_type, headers=headers)


@app.post("/api/restore")
async def restore_backup(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    if not file.filename:
        raise HTTPException(400, "A backup file is required")

    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(400, "Backup file is empty")

    try:
        text_value = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(400, "Backup file must be UTF-8 encoded") from exc

    payload = parse_backup_upload(file.filename, text_value)
    fmt = "csv" if file.filename.lower().endswith(".csv") else "md"

    try:
        result = restore_backup_payload(payload, db, current_user.id)
        record_backup_audit(
            db,
            current_user.id,
            "restore",
            fmt,
            result["eras_restored"],
            result["events_restored"],
        )
        db.commit()
        return result
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(400, f"Restore failed: {exc}") from exc


# ── Reflection endpoints ────────────────────────────────────────────────────────


@app.post("/reflect/probe")
def probe(
    body: ProbeReq,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settings = resolve_user_llm_settings(db, current_user.id)
    client = llm_client(settings)
    system = (
        "You are a thoughtful biographer and reflective journaling companion. "
        "Help the user recover texture and meaning from a briefly described memory. "
        "Ask 3 open-ended questions — one about sensory detail, one about the people "
        "involved or their absence, one about what this moment meant then versus now.\n\n"
        'Return ONLY valid JSON: {"questions": ["q1", "q2", "q3"]}'
    )
    sentiment_word = (
        "positive"
        if body.sentiment_score > 0
        else "negative"
        if body.sentiment_score < 0
        else "neutral"
    )
    user_msg = (
        f'Event: "{body.headline}" on {body.date.isoformat()}. '
        f"Emotional weight: {body.sentiment_score}/5 ({sentiment_word})."
    )
    try:
        response = client.chat.completions.create(
            model=settings["llm_model"],
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "probe", "schema": {"type": "object"}},
            },
            temperature=0.7,
        )
        parsed = parse_llm_json(response.choices[0].message.content)
        if "questions" not in parsed or not isinstance(parsed["questions"], list):
            raise HTTPException(502, "Missing 'questions' array in LLM response")
        return parsed
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Probe error: %s", exc)
        raise HTTPException(502, str(exc)) from exc


@app.post("/reflect/synthesize")
def synthesize(
    body: SynthReq,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settings = resolve_user_llm_settings(db, current_user.id)
    client = llm_client(settings)
    system = (
        "Synthesize the user's fragmented responses into a 2-3 sentence reflection "
        "in their voice, first-person, past tense. Preserve specific details they "
        "mentioned. Do not add facts they didn't provide. Do not moralize or offer "
        "advice.\n\n"
        'Return ONLY valid JSON: {"reflection": "..."}'
    )
    qa = "\n".join(
        f"Q: {question}\nA: {answer}"
        for question, answer in zip(body.questions, body.answers)
        if answer.strip()
    )
    user_msg = (
        f'Event: "{body.headline}" on {body.date.isoformat()}.\n'
        f"Emotional weight: {body.sentiment_score}/5.\n\n{qa}"
    )
    try:
        response = client.chat.completions.create(
            model=settings["llm_model"],
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "synthesize", "schema": {"type": "object"}},
            },
            temperature=0.7,
        )
        parsed = parse_llm_json(response.choices[0].message.content)
        if "reflection" not in parsed:
            raise HTTPException(502, "Missing 'reflection' in LLM response")

        db.add(
            LLMReflection(
                user_id=current_user.id,
                event_id=None,
                headline=body.headline,
                event_date=body.date,
                sentiment_score=body.sentiment_score,
                questions=body.questions,
                answers=body.answers,
                reflection=parsed["reflection"],
            )
        )
        db.commit()
        return parsed
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        logger.error("Synthesize error: %s", exc)
        raise HTTPException(502, str(exc)) from exc


# ── Health ──────────────────────────────────────────────────────────────────────


@app.post("/health/llm")
def health_llm(
    body: LLMHealthRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stored = resolve_user_llm_settings(db, current_user.id)
    url = stored["llm_base_url"]
    model = stored["llm_model"]

    if body.llm_base_url:
        candidate = normalize_http_url(body.llm_base_url)
        if candidate != stored["llm_base_url"]:
            if not ALLOWED_LLM_BASE_URLS:
                raise HTTPException(400, "Testing unsaved LLM base URLs is disabled")
            if not base_url_allowed(candidate):
                raise HTTPException(400, "LLM base URL is not in the allowed list")
        url = candidate

    if body.llm_model:
        model = body.llm_model.strip() or model

    try:
        client = OpenAI(base_url=url, api_key=stored["llm_api_key"])
        models = client.models.list()
        available = [model_obj.id for model_obj in models.data]
        return {
            "status": "ok",
            "base_url": url,
            "model": model,
            "model_available": model in available,
            "available_models": available,
        }
    except Exception as exc:
        return {"status": "error", "base_url": url, "model": model, "error": str(exc)}


# ── Static file serving ────────────────────────────────────────────────────────

STATIC_DIR = os.path.dirname(os.path.abspath(__file__))


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/styles.css")
def css():
    return FileResponse(os.path.join(STATIC_DIR, "styles.css"), media_type="text/css")


@app.get("/tailwind.config.js")
def tailwind_config():
    return FileResponse(
        os.path.join(STATIC_DIR, "tailwind.config.js"),
        media_type="application/javascript",
    )


@app.get("/app.js")
def js():
    return FileResponse(
        os.path.join(STATIC_DIR, "app.js"),
        media_type="application/javascript",
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
