"""Database tables and helpers (Postgres on Render, SQLite for local testing)."""
import base64
import hashlib
import os
from datetime import datetime, timezone

from cryptography.fernet import Fernet
from sqlalchemy import (Boolean, Column, DateTime, ForeignKey, Integer, LargeBinary,
                        MetaData, String, Table, Text, create_engine)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC


def make_engine():
    url = os.environ.get("DATABASE_URL", "sqlite:///local.db")
    if url.startswith("postgres://"):
        url = "postgresql+psycopg2://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    kw = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kw["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kw)


metadata = MetaData()

settings_t = Table("settings", metadata,
                   Column("key", String(64), primary_key=True),
                   Column("value", Text, nullable=False))

assets_t = Table("assets", metadata,
                 Column("name", String(64), primary_key=True),
                 Column("mime", String(64), nullable=False),
                 Column("data", LargeBinary, nullable=False))

accounts_t = Table("accounts", metadata,
                   Column("id", Integer, primary_key=True),
                   Column("email", String(255), unique=True, nullable=False),
                   Column("token_enc", Text, nullable=False),
                   Column("created_at", DateTime, nullable=False))

jobs_t = Table("jobs", metadata,
               Column("id", Integer, primary_key=True),
               Column("name", String(200), nullable=False),
               Column("account_id", Integer, ForeignKey("accounts.id")),
               Column("status", String(20), nullable=False),  # draft running paused stopped done
               Column("skip_sent", Boolean, nullable=False, default=True),
               Column("note", Text, nullable=False, default=""),
               Column("created_at", DateTime, nullable=False))

recipients_t = Table("recipients", metadata,
                     Column("id", Integer, primary_key=True),
                     Column("job_id", Integer, ForeignKey("jobs.id"), index=True, nullable=False),
                     Column("row_no", Integer, nullable=False),
                     Column("email", String(320), nullable=False),
                     Column("name", Text, nullable=False, default=""),
                     Column("greeting", Text, nullable=False, default=""),
                     Column("subject", Text, nullable=False, default=""),
                     Column("message", Text, nullable=False, default=""),
                     Column("attachments", Text, nullable=False, default=""),
                     # pending sending sent failed skipped uncertain
                     Column("status", String(20), nullable=False, index=True),
                     Column("detail", Text, nullable=False, default=""),
                     Column("sent_at", DateTime))

job_files_t = Table("job_files", metadata,
                    Column("id", Integer, primary_key=True),
                    Column("job_id", Integer, ForeignKey("jobs.id"), index=True, nullable=False),
                    Column("filename", String(255), nullable=False),
                    Column("mime", String(100), nullable=False),
                    Column("data", LargeBinary, nullable=False))

engine = make_engine()


def init_db():
    metadata.create_all(engine)


def _fernet():
    """Any long random string works as TOKEN_ENCRYPTION_KEY; the real key is derived from it."""
    secret = os.environ.get("TOKEN_ENCRYPTION_KEY", "")
    if len(secret) < 16:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is missing or too short (use 16+ random characters)")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def encrypt(s):
    return _fernet().encrypt(s.encode()).decode()


def decrypt(s):
    return _fernet().decrypt(s.encode()).decode()
