from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.dependencies import get_db

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", summary="Liveness: the process is up")
def live() -> dict:
    return {"status": "ok"}


@router.get("/ready", summary="Readiness: PostgreSQL answers SELECT 1")
def ready(session: Session = Depends(get_db)):
    try:
        session.execute(text("SELECT 1"))
        db = session.execute(text("SELECT current_database()")).scalar_one()
        return {"status": "ok", "database": db}
    except Exception:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "DATABASE_UNAVAILABLE",
                    "message": "PostgreSQL is not reachable.",
                    "details": None,
                }
            },
        )
