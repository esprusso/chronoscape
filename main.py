import os
import json
import re
import logging
from datetime import date, datetime
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, Text, Date, DateTime, ForeignKey
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
DATA_DIR = os.getenv("DATA_DIR", "./data")
os.makedirs(DATA_DIR, exist_ok=True)
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")

# ── Database ────────────────────────────────────────────────────────────────────

engine = create_engine(
    f"sqlite:///{DATA_DIR}/timeline.db",
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
    start_date_precision = Column(String(10), default="day")  # 'year', 'month', or 'day'
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
    created_at = Column(DateTime, default=datetime.utcnow)
    era = relationship("Era", back_populates="events")


Base.metadata.create_all(bind=engine)

# ── Lightweight migration for existing databases ────────────────────────────────
with engine.connect() as conn:
    migrations = [
        "ALTER TABLE events ADD COLUMN date_precision VARCHAR(10) DEFAULT 'day'",
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


# ── Pydantic Schemas ────────────────────────────────────────────────────────────


class EraCreate(BaseModel):
    name: str
    start_date: date
    end_date: date
    start_date_precision: str = "day"
    end_date_precision: str = "day"
    color_hex: str = "#B8C4D4"


class EraUpdate(BaseModel):
    name: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    start_date_precision: Optional[str] = None
    end_date_precision: Optional[str] = None
    color_hex: Optional[str] = None


class EraOut(BaseModel):
    id: int
    name: str
    start_date: date
    end_date: date
    start_date_precision: str
    end_date_precision: str
    color_hex: str
    model_config = {"from_attributes": True}


class EventCreate(BaseModel):
    headline: str = Field(max_length=120)
    explanation: str = ""
    date: date
    date_precision: str = "day"  # 'year', 'month', or 'day'
    sentiment_score: int = Field(ge=-5, le=5)
    era_id: Optional[int] = None
    reflection_qa: Optional[dict] = None


class EventUpdate(BaseModel):
    headline: Optional[str] = Field(None, max_length=120)
    explanation: Optional[str] = None
    date: Optional[date] = None
    date_precision: Optional[str] = None
    sentiment_score: Optional[int] = Field(None, ge=-5, le=5)
    era_id: Optional[int] = None
    reflection_qa: Optional[dict] = None


class EventOut(BaseModel):
    id: int
    headline: str
    explanation: str
    date: date
    date_precision: str
    sentiment_score: int
    era_id: Optional[int]
    reflection_qa: Optional[dict]
    created_at: datetime
    model_config = {"from_attributes": True}


class ProbeReq(BaseModel):
    headline: str
    date: date
    sentiment_score: int


class SynthReq(BaseModel):
    headline: str
    date: date
    sentiment_score: int
    questions: List[str]
    answers: List[str]


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
        if db.query(Event).count() == 0:
            db.add(
                Event(
                    headline="The beginning of something new",
                    explanation=(
                        "Every timeline starts somewhere. This is yours "
                        "— a place to hold the moments that shaped you."
                    ),
                    date=date.today(),
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
    return db.query(Event).order_by(Event.date).all()


@app.post("/api/events", response_model=EventOut, status_code=201)
def create_event(body: EventCreate, db: Session = Depends(get_db)):
    ev = Event(**body.model_dump())
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
        "positive" if body.sentiment_score > 0
        else "negative" if body.sentiment_score < 0
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
            response_format={"type": "json_object"},
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
        f"Q: {q}\nA: {a}"
        for q, a in zip(body.questions, body.answers)
        if a.strip()
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
            response_format={"type": "json_object"},
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
