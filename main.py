"""Minimal test entry point for Railway deployment debugging."""

from fastapi import FastAPI

app = FastAPI(title="Watercooler MCP Test")

@app.get("/")
def root():
    return {"status": "minimal test app running"}

@app.get("/health")
def health():
    return {"status": "healthy", "mode": "test"}

__all__ = ["app"]
