"""FastAPI application — the user-facing entry point for the agent pipeline.

Endpoints:
  POST /ask        — run the full pipeline, return a ReportResult
  GET  /health     — liveness check
  WS   /stream     — stream pipeline progress events then the final report
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel, Field

from src.agents.orchestrator import OrchestratorAgent
from src.config import get_settings
from src.models.schemas import ReportResult

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

settings = get_settings()

logger.remove()
logger.add(
    sys.stderr,
    level=settings.log_level,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    colorize=True,
)

# ---------------------------------------------------------------------------
# Application lifespan — build the orchestrator once, reuse across requests
# ---------------------------------------------------------------------------

_orchestrator: OrchestratorAgent | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _orchestrator
    logger.info(
        f"Starting Internal Agents API | mock_mode={settings.mock_mode} | "
        f"model={settings.default_model}"
    )
    _orchestrator = OrchestratorAgent()
    yield
    logger.info("Shutting down Internal Agents API")


app = FastAPI(
    title="Internal Agents API",
    description="Multi-agent business intelligence system backed by Jira, HubSpot, Xero, Harvest, and PandaDoc.",
    version="0.1.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="src/api/static"), name="static")


def _get_orchestrator() -> OrchestratorAgent:
    if _orchestrator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Orchestrator not initialised — server is starting up.",
        )
    return _orchestrator


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class AskRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=2000, description="Natural-language question or instruction.")


class AskResponse(BaseModel):
    """Returned by POST /ask when the pipeline produces a full report."""
    title: str
    audience: str
    tldr: str
    markdown: str
    sections: list[dict[str, str]]
    generated_at: datetime
    mock_mode: bool


class ClarificationResponse(BaseModel):
    """Returned by POST /ask when the orchestrator needs more information."""
    clarification_needed: bool = True
    question: str


class HealthResponse(BaseModel):
    status: str
    mock_mode: bool
    model: str
    timestamp: datetime


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/chat")


@app.get("/chat", include_in_schema=False)
async def chat_ui() -> FileResponse:
    return FileResponse("src/api/static/chat.html")


@app.get("/health", response_model=HealthResponse, tags=["meta"])
async def health() -> HealthResponse:
    """Liveness probe — always returns 200 if the server is running."""
    return HealthResponse(
        status="ok",
        mock_mode=settings.mock_mode,
        model=settings.default_model,
        timestamp=datetime.now(UTC),
    )


@app.post("/ask", tags=["pipeline"])
async def ask(body: AskRequest) -> JSONResponse:
    """Run the full agent pipeline for a natural-language prompt.

    Returns a ReportResult JSON when a report is produced, or a
    ClarificationResponse when the orchestrator needs more information.
    """
    orchestrator = _get_orchestrator()
    logger.info(f"POST /ask | prompt={body.prompt[:80]!r}")

    try:
        result = await orchestrator.run(body.prompt)
    except Exception as exc:
        logger.exception(f"Pipeline error: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Pipeline error: {exc}",
        )

    if isinstance(result, ReportResult):
        return JSONResponse(
            content=AskResponse(
                title=result.title,
                audience=result.audience.value,
                tldr=result.tldr,
                markdown=result.markdown,
                sections=[
                    {"heading": s.heading, "body": s.body}
                    for s in result.sections
                ],
                generated_at=result.generated_at,
                mock_mode=settings.mock_mode,
            ).model_dump(mode="json")
        )

    # Orchestrator returned a plain string — either a clarification question
    # or a direct text answer (e.g. when only analysis was requested).
    if result.endswith("?") or "clarif" in result.lower():
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=ClarificationResponse(question=result).model_dump(),
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"result": result, "mock_mode": settings.mock_mode},
    )


@app.websocket("/stream")
async def stream(websocket: WebSocket) -> None:
    """WebSocket endpoint — streams pipeline progress then the final report.

    Protocol:
      Client sends:  {"prompt": "..."}
      Server sends:  one or more  {"type": "progress", "message": "..."}
      Server sends:  one final    {"type": "report",   "markdown": "..."}
                  or              {"type": "result",   "text": "..."}
                  or              {"type": "error",    "detail": "..."}
    """
    await websocket.accept()
    orchestrator = _get_orchestrator()
    logger.info("WS /stream | client connected")

    try:
        while True:
            payload: dict[str, Any] = await websocket.receive_json()
            prompt: str = payload.get("prompt", "").strip()
            if not prompt:
                await websocket.send_json({"type": "error", "detail": "prompt is required"})
                continue

            logger.info(f"WS /stream | prompt={prompt[:80]!r}")

            try:
                async for chunk in orchestrator.run_stream(prompt):
                    if chunk.startswith("progress: "):
                        await websocket.send_json({
                            "type": "progress",
                            "message": chunk[len("progress: "):],
                        })
                    elif chunk.startswith("report: "):
                        await websocket.send_json({
                            "type": "report",
                            "markdown": chunk[len("report: "):],
                        })
                    elif chunk.startswith("result: "):
                        await websocket.send_json({
                            "type": "result",
                            "text": chunk[len("result: "):],
                        })
                    elif chunk.startswith("error: "):
                        await websocket.send_json({
                            "type": "error",
                            "detail": chunk[len("error: "):],
                        })
            except WebSocketDisconnect:
                raise  # let outer handler log the disconnect
            except Exception as exc:
                logger.exception(f"WS /stream pipeline error: {exc}")
                try:
                    await websocket.send_json({"type": "error", "detail": str(exc)})
                except Exception:
                    pass

    except WebSocketDisconnect:
        logger.info("WS /stream | client disconnected")
    except Exception as exc:
        logger.exception(f"WS /stream error: {exc}")
        try:
            await websocket.send_json({"type": "error", "detail": str(exc)})
        except Exception:
            pass
