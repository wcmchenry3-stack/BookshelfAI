"""CORS exposes Retry-After so browser clients can read the 429/503 wait (#766)."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient


def test_cors_exposes_retry_after_for_allowed_origin(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("CORS_ORIGINS", "[]")

    from app.main import app

    # Re-mount the CORS middleware exactly as app.main configured it, only
    # swapping in an allowed origin (the real list comes from the environment).
    configured = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
    origin = "https://app.example.test"
    probe = FastAPI()
    probe.add_middleware(
        CORSMiddleware, **{**configured.kwargs, "allow_origins": [origin]}
    )

    @probe.get("/ping")
    def ping():
        return {"ok": True}

    response = TestClient(probe).get("/ping", headers={"Origin": origin})

    assert response.headers["access-control-allow-origin"] == origin
    exposed = [
        h.strip().lower()
        for h in response.headers.get("access-control-expose-headers", "").split(",")
    ]
    assert "retry-after" in exposed
