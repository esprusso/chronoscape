import os
import json
import re
import logging
import csv
import io
from datetime import date as date_type, datetime, UTC
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Depends, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Text,
    Date,
    DateTime,
    ForeignKey,
)
from sqlalchemy.types import JSON
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker, relationship
from pydantic import BaseModel, Field
from openai import OpenAI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────────

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://host.docker.internal:1234/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3.5-122b-a10b")
LLM_API_KEY = os.getenv("LLM_API_KEY", "lm-studio")
# Default to Vercel's writable temp volume; local/dev environments can override DATA_DIR.
DATA_DIR = os.getenv("DATA_DIR", "/tmp/data")
os.makedirs(DATA_DIR, exist_ok=True)
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
DB_PATH = os.path.join(DATA_DIR, "timeline.db")
DB_ALREADY_EXISTS = os.path.exists(DB_PATH)
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

# ── Database ────────────────────────────────────────────────────────────────────

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


class Era(Base):
    __tablename__ = "eras"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    start_date_precision = Column(
        String(10), default="day"
    )  # 'year', 'month', or 'day'
    end_date_precision = Column(String(10), default="day")  # 'year', 'month', or 'day'
    color_hex = Column(String(7), default="#B8C4D4")
    events = relationship("Event", back_populates="era")


class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True, index=True)
    headline = Column(String(120), nullable=False)
    explanation = Column(Text, default="")
    date = Column(Date, nullable=False)
    date_precision = Column(String(10), default="day")  # 'year', 'month', or 'day'
    sentiment_score = Column(Integer, nullable=False)
    era_id = Column(Integer, ForeignKey("eras.id"), nullable=True)
    reflection_qa = Column(JSON, nullable=True)
    sort_index = Column(Integer, nullable=False, default=0, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None))
    era = relationship("Era", back_populates="events")


Base.metadata.create_all(bind=engine)

# ── Lightweight migration for existing databases ────────────────────────────────
with engine.connect() as conn:
    migrations = [
        "ALTER TABLE events ADD COLUMN date_precision VARCHAR(10) DEFAULT 'day'",
        "ALTER TABLE events ADD COLUMN sort_index INTEGER DEFAULT 0",
        "ALTER TABLE eras ADD COLUMN start_date_precision VARCHAR(10) DEFAULT 'day'",
        "ALTER TABLE eras ADD COLUMN end_date_precision VARCHAR(10) DEFAULT 'day'",
    ]
    for sql in migrations:
        try:
            conn.execute(__import__("sqlalchemy").text(sql))
            conn.commit()
            logger.info("Migrated: %s", sql.split("ADD COLUMN ")[1])
        except Exception:
            pass  # Column already exists


def normalize_event_sort_indexes():
    db = SessionLocal()
    try:
        ordered_events = db.query(Event).order_by(Event.sort_index, Event.date, Event.id).all()
        needs_reindex = False
        seen_indexes = set()

        for expected_index, event in enumerate(ordered_events):
            if event.sort_index is None or event.sort_index in seen_indexes:
                needs_reindex = True
                break
            seen_indexes.add(event.sort_index)
            if event.sort_index != expected_index:
                needs_reindex = True
                break

        if needs_reindex:
            fallback_order = db.query(Event).order_by(Event.date, Event.id).all()
            for index, event in enumerate(fallback_order):
                event.sort_index = index
            db.commit()
            logger.info("Normalized event sort indexes")
    finally:
        db.close()


normalize_event_sort_indexes()


# ── Pydantic Schemas ────────────────────────────────────────────────────────────


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
    date_precision: str = "day"  # 'year', 'month', or 'day'
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
    ids: List[int] = Field(default_factory=list)


class ProbeReq(BaseModel):
    headline: str
    date: date_type
    sentiment_score: int


class SynthReq(BaseModel):
    headline: str
    date: date_type
    sentiment_score: int
    questions: List[str]
    answers: List[str]


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
    eras: List[BackupEraRecord] = Field(default_factory=list)
    events: List[BackupEventRecord] = Field(default_factory=list)


# ── Dependencies ────────────────────────────────────────────────────────────────


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Settings persistence ───────────────────────────────────────────────────────


def load_settings() -> dict:
    defaults = {
        "llm_base_url": LLM_BASE_URL,
        "llm_model": LLM_MODEL,
        "llm_api_key": LLM_API_KEY,
    }
    if os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH) as f:
            defaults.update(json.load(f))
    return defaults


def persist_settings(data: dict):
    with open(SETTINGS_PATH, "w") as f:
        json.dump(data, f, indent=2)


# ── Backup helpers ─────────────────────────────────────────────────────────────


def utc_now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _backup_era_key(era_id: int) -> str:
    return f"era-{era_id}"


def build_backup_payload(db: Session) -> BackupPayload:
    generated_at = utc_now_naive()
    eras = db.query(Era).order_by(Era.start_date, Era.id).all()
    events = db.query(Event).order_by(Event.sort_index, Event.id).all()
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
                "created_at": (
                    event.created_at.isoformat() if event.created_at is not None else ""
                ),
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


def _validate_backup_version(value: str):
    if str(value) != BACKUP_VERSION:
        raise HTTPException(400, f"Unsupported backup version: {value}")


def parse_backup_csv(text: str) -> BackupPayload:
    try:
        reader = csv.DictReader(io.StringIO(text))
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


def parse_backup_markdown(text: str) -> BackupPayload:
    pattern = (
        re.escape(BACKUP_MARKER_START)
        + r"\s*(.*?)\s*"
        + re.escape(BACKUP_MARKER_END)
    )
    match = re.search(pattern, text, flags=re.DOTALL)
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


def parse_backup_upload(filename: str, text: str) -> BackupPayload:
    lower_name = (filename or "").lower()
    if lower_name.endswith(".csv"):
        return parse_backup_csv(text)
    if lower_name.endswith(".md"):
        return parse_backup_markdown(text)
    raise HTTPException(400, "Unsupported backup file type. Use a .csv or .md backup.")


def restore_backup_payload(payload: BackupPayload, db: Session) -> dict:
    _validate_backup_version(payload.backup_version)
    era_id_map = {}

    try:
        db.query(Event).delete()
        db.query(Era).delete()

        for era in payload.eras:
            restored_era = Era(
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
            db.add(
                Event(
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
            )

        db.commit()
        return {"eras_restored": len(payload.eras), "events_restored": len(payload.events)}
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(400, f"Restore failed: {exc}") from exc


# ── LLM helpers ────────────────────────────────────────────────────────────────


def llm_client(settings: dict | None = None) -> OpenAI:
    s = settings or load_settings()
    return OpenAI(base_url=s["llm_base_url"], api_key=s["llm_api_key"])


def parse_llm_json(raw: str) -> dict:
    """Defensively extract JSON from LLM output that may contain markdown
    fences, <think> blocks (Qwen3), or other wrapper text."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.error("LLM JSON parse failure — raw output: %s", raw)
        raise HTTPException(502, "LLM returned malformed JSON")


# ── FastAPI app ─────────────────────────────────────────────────────────────────

app = FastAPI(title="Chronoscape")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def seed():
    db = SessionLocal()
    try:
        if not DB_ALREADY_EXISTS and db.query(Event).count() == 0:
            db.add(
                Event(
                    headline="The beginning of something new",
                    explanation=(
                        "Every timeline starts somewhere. This is yours "
                        "— a place to hold the moments that shaped you."
                    ),
                    date=date_type.today(),
                    sentiment_score=3,
                )
            )
            db.commit()
            logger.info("Seeded initial event")
    finally:
        db.close()


# ── Event endpoints ─────────────────────────────────────────────────────────────


@app.get("/api/events", response_model=List[EventOut])
def list_events(db: Session = Depends(get_db)):
    return db.query(Event).order_by(Event.sort_index, Event.id).all()


@app.post("/api/events", response_model=EventOut, status_code=201)
def create_event(body: EventCreate, db: Session = Depends(get_db)):
    payload = body.model_dump()
    if payload["sort_index"] is None:
        last_event = db.query(Event).order_by(Event.sort_index.desc(), Event.id.desc()).first()
        payload["sort_index"] = 0 if last_event is None else last_event.sort_index + 1
    ev = Event(**payload)
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev


@app.put("/api/events/{eid}", response_model=EventOut)
def update_event(eid: int, body: EventUpdate, db: Session = Depends(get_db)):
    ev = db.query(Event).get(eid)
    if not ev:
        raise HTTPException(404)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(ev, k, v)
    db.commit()
    db.refresh(ev)
    return ev


@app.post("/api/events/reorder", response_model=List[EventOut])
def reorder_events(body: EventReorderRequest, db: Session = Depends(get_db)):
    current_events = db.query(Event).order_by(Event.sort_index, Event.id).all()
    current_ids = [event.id for event in current_events]

    if sorted(body.ids) != sorted(current_ids):
        raise HTTPException(400, "Reorder request must include every event exactly once")

    event_map = {event.id: event for event in current_events}
    for index, event_id in enumerate(body.ids):
        event_map[event_id].sort_index = index

    db.commit()
    return db.query(Event).order_by(Event.sort_index, Event.id).all()


@app.delete("/api/events/{eid}", status_code=204)
def delete_event(eid: int, db: Session = Depends(get_db)):
    ev = db.query(Event).get(eid)
    if not ev:
        raise HTTPException(404)
    db.delete(ev)
    db.commit()


# ── Era endpoints ───────────────────────────────────────────────────────────────


@app.get("/api/eras", response_model=List[EraOut])
def list_eras(db: Session = Depends(get_db)):
    return db.query(Era).order_by(Era.start_date).all()


@app.post("/api/eras", response_model=EraOut, status_code=201)
def create_era(body: EraCreate, db: Session = Depends(get_db)):
    era = Era(**body.model_dump())
    db.add(era)
    db.commit()
    db.refresh(era)
    return era


@app.put("/api/eras/{eid}", response_model=EraOut)
def update_era(eid: int, body: EraUpdate, db: Session = Depends(get_db)):
    era = db.query(Era).get(eid)
    if not era:
        raise HTTPException(404)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(era, k, v)
    db.commit()
    db.refresh(era)
    return era


@app.delete("/api/eras/{eid}", status_code=204)
def delete_era(eid: int, db: Session = Depends(get_db)):
    era = db.query(Era).get(eid)
    if not era:
        raise HTTPException(404)
    db.delete(era)
    db.commit()


# ── Settings endpoints ──────────────────────────────────────────────────────────


@app.get("/api/settings")
def get_settings():
    return load_settings()


@app.put("/api/settings")
def put_settings(body: dict):
    allowed = {"llm_base_url", "llm_model", "llm_api_key"}
    current = load_settings()
    current.update({k: v for k, v in body.items() if k in allowed})
    persist_settings(current)
    return current


@app.get("/api/backup")
def download_backup(format: str, db: Session = Depends(get_db)):
    fmt = (format or "").lower()
    if fmt not in {"csv", "md"}:
        raise HTTPException(400, "Unsupported backup format. Use csv or md.")

    payload = build_backup_payload(db)
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
async def restore_backup(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not file.filename:
        raise HTTPException(400, "A backup file is required")

    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(400, "Backup file is empty")

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(400, "Backup file must be UTF-8 encoded") from exc

    payload = parse_backup_upload(file.filename, text)
    return restore_backup_payload(payload, db)


# ── Reflection endpoints ────────────────────────────────────────────────────────


@app.post("/reflect/probe")
def probe(body: ProbeReq):
    settings = load_settings()
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
        r = client.chat.completions.create(
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
        parsed = parse_llm_json(r.choices[0].message.content)
        if "questions" not in parsed or not isinstance(parsed["questions"], list):
            raise HTTPException(502, "Missing 'questions' array in LLM response")
        return parsed
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Probe error: %s", e)
        raise HTTPException(502, str(e))


@app.post("/reflect/synthesize")
def synthesize(body: SynthReq):
    settings = load_settings()
    client = llm_client(settings)
    system = (
        "Synthesize the user's fragmented responses into a 2-3 sentence reflection "
        "in their voice, first-person, past tense. Preserve specific details they "
        "mentioned. Do not add facts they didn't provide. Do not moralize or offer "
        "advice.\n\n"
        'Return ONLY valid JSON: {"reflection": "..."}'
    )
    qa = "\n".join(
        f"Q: {q}\nA: {a}" for q, a in zip(body.questions, body.answers) if a.strip()
    )
    user_msg = (
        f'Event: "{body.headline}" on {body.date.isoformat()}.\n'
        f"Emotional weight: {body.sentiment_score}/5.\n\n{qa}"
    )
    try:
        r = client.chat.completions.create(
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
        parsed = parse_llm_json(r.choices[0].message.content)
        if "reflection" not in parsed:
            raise HTTPException(502, "Missing 'reflection' in LLM response")
        return parsed
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Synthesize error: %s", e)
        raise HTTPException(502, str(e))


# ── Health ──────────────────────────────────────────────────────────────────────


@app.get("/health/llm")
def health_llm(base_url: str = None, model: str = None):
    s = load_settings()
    url = base_url or s["llm_base_url"]
    mdl = model or s["llm_model"]
    try:
        c = OpenAI(base_url=url, api_key=s["llm_api_key"])
        models = c.models.list()
        avail = [m.id for m in models.data]
        return {
            "status": "ok",
            "base_url": url,
            "model": mdl,
            "model_available": mdl in avail,
            "available_models": avail,
        }
    except Exception as e:
        return {"status": "error", "base_url": url, "model": mdl, "error": str(e)}


# ── Static file serving ────────────────────────────────────────────────────────

STATIC_DIR = os.path.dirname(os.path.abspath(__file__))


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/styles.css")
def css():
    return FileResponse(os.path.join(STATIC_DIR, "styles.css"), media_type="text/css")


@app.get("/app.js")
def js():
    return FileResponse(
        os.path.join(STATIC_DIR, "app.js"), media_type="application/javascript"
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
