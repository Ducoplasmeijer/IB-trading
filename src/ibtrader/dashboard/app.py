"""Local, read-only dashboard server. Binds to 127.0.0.1 only; exposes no order endpoints."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..config import Settings, get_settings
from .service import RANGES, DashboardService

STATIC = Path(__file__).parent / "static"
CSP = (
    "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    svc = DashboardService(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        svc.start()
        yield
        await svc.stop()

    app = FastAPI(title="IB dashboard", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    # Blocks DNS-rebinding: only requests addressed to localhost are served.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        # POSTs must carry a custom header; browsers can't add one cross-site without CORS (never enabled).
        if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "ibdash":
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def need_connection():
        if not svc.ib.isConnected():
            raise HTTPException(503, svc.last_connect_error or "Not connected to IB Gateway yet")

    @app.get("/api/status")
    def status():
        return svc.status()

    @app.get("/api/portfolio")
    def portfolio():
        need_connection()
        return svc.portfolio()

    @app.get("/api/gold/quotes")
    def gold_quotes():
        need_connection()
        return svc.gold_quotes()

    @app.get("/api/gold/history")
    async def gold_history(key: str, range: str = Query("1Y", pattern="^(" + "|".join(RANGES) + ")$")):  # noqa: A002
        need_connection()
        try:
            return {"key": key, "range": range, "bars": await svc.history(key, range)}
        except KeyError:
            raise HTTPException(404, "unknown product") from None
        except TimeoutError:
            raise HTTPException(504, "IB historical data request timed out") from None

    @app.get("/api/gold/tracking")
    async def gold_tracking(range: str = Query("1Y", pattern="^(3M|6M|1Y|2Y|5Y)$")):  # noqa: A002
        need_connection()
        return {"range": range, "rows": await svc.tracking(range)}

    @app.get("/api/costs/estimates")
    def cost_estimates(target: float = Query(10000, gt=0, le=1e8)):
        need_connection()
        return svc.cost_estimates(target)

    @app.get("/api/costs/executions")
    def executions():
        need_connection()
        return svc.executions()

    @app.get("/api/costs/flex")
    def flex_summary():
        return {"configured": bool(settings.flex_token and settings.flex_query_id), "data": svc.flex_summary()}

    @app.post("/api/costs/flex/refresh")
    async def flex_refresh():
        try:
            return {"configured": True, "data": await svc.refresh_flex()}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, str(e)) from None

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app


def run() -> None:
    import uvicorn

    settings = get_settings()
    print(f"Dashboard: http://127.0.0.1:{settings.dashboard_port}  (read-only, mode={settings.mode.value})")
    uvicorn.run(create_app(settings), host="127.0.0.1", port=settings.dashboard_port, log_level="warning")
