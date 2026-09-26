"""Local dashboard server. Binds to 127.0.0.1 only.

Data endpoints are read-only. Order endpoints go through Session.preview_order / submit_order, which apply the
guardrails in safety.py; live orders additionally need .env flags and a typed "LIVE" confirmation.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..config import Settings, get_settings
from ..safety import OrderBlockedError
from .service import RANGES, InstrumentRef, OrderRequest, Session, SessionManager

STATIC = Path(__file__).parent / "static"
CSP = (
    "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
SESSION = Query("live", pattern="^(paper|live)$")
USER_RANGES = "|".join(k for k in RANGES if k != "LAST")


class SubmitBody(BaseModel):
    preview_id: str
    confirm: str | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    mgr = SessionManager(settings)
    allowed_origins = {f"http://127.0.0.1:{settings.dashboard_port}", f"http://localhost:{settings.dashboard_port}"}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        mgr.start()
        yield
        await mgr.stop()

    app = FastAPI(title="IB dashboard", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    # Blocks DNS-rebinding: only requests addressed to localhost are served.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.method not in ("GET", "HEAD"):
            # POSTs need our custom header (browsers can't add it cross-site without CORS, which is never enabled)
            # and, when the browser sends an Origin, it must be this dashboard.
            origin = request.headers.get("origin")
            if request.headers.get("x-requested-with") != "ibdash" or (origin and origin not in allowed_origins):
                return JSONResponse({"detail": "forbidden"}, status_code=403)
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def sess(name: str, need_connection: bool = True) -> Session:
        s = mgr.get(name)
        if need_connection and not s.ib.isConnected():
            raise HTTPException(503, s.last_connect_error or f"{name} session not connected yet")
        return s

    # ----- status -----
    @app.get("/api/sessions")
    def sessions():
        return mgr.overview()

    @app.get("/api/status")
    def status(session: str = SESSION):
        return mgr.get(session).status()

    # ----- portfolio -----
    @app.get("/api/portfolio")
    def portfolio(session: str = SESSION):
        return sess(session).portfolio()

    # ----- metals -----
    @app.get("/api/metals/groups")
    def groups():
        return mgr.groups()

    @app.get("/api/metals/quotes")
    def metal_quotes(group: str, session: str = SESSION):
        try:
            return sess(session).metal_quotes(group)
        except KeyError:
            raise HTTPException(404, "unknown group") from None

    @app.get("/api/metals/history")
    async def metal_history(key: str, range: str = Query("1Y", pattern=f"^({USER_RANGES})$"),  # noqa: A002
                            session: str = SESSION):
        try:
            return {"key": key, "range": range, "bars": await sess(session).history(key, range)}
        except KeyError:
            raise HTTPException(404, "unknown product") from None
        except TimeoutError:
            raise HTTPException(504, "IB historical data request timed out") from None

    @app.get("/api/metals/tracking")
    async def metal_tracking(group: str, range: str = Query("1Y", pattern="^(3M|6M|1Y|2Y|5Y)$"),  # noqa: A002
                             session: str = SESSION):
        try:
            return {"range": range, **await sess(session).tracking(group, range)}
        except KeyError:
            raise HTTPException(404, "unknown group") from None

    # ----- costs -----
    @app.get("/api/costs/estimates")
    def cost_estimates(group: str, target: float = Query(10000, gt=0, le=1e8), session: str = SESSION):
        try:
            return sess(session).cost_estimates(group, target)
        except KeyError:
            raise HTTPException(404, "unknown group") from None

    @app.get("/api/costs/executions")
    def executions(session: str = SESSION):
        return sess(session).executions()

    @app.get("/api/costs/flex")
    def flex_summary():
        return {"configured": bool(settings.flex_token and settings.flex_query_id), "data": mgr.flex_summary()}

    @app.post("/api/costs/flex/refresh")
    async def flex_refresh():
        try:
            return {"configured": True, "data": await mgr.refresh_flex()}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, str(e)) from None

    # ----- trading -----
    @app.get("/api/instruments")
    def instruments(session: str = SESSION):
        return sess(session).instruments()

    @app.post("/api/book")
    async def book(ref: InstrumentRef, session: str = SESSION):
        try:
            return await sess(session).book(ref)
        except (KeyError, ValueError) as e:
            raise HTTPException(400, str(e)) from None

    @app.get("/api/orders")
    def orders(session: str = SESSION):
        return sess(session).orders()

    @app.post("/api/orders/preview")
    async def preview(req: OrderRequest, session: str = SESSION):
        try:
            return await sess(session).preview_order(req)
        except (KeyError, ValueError) as e:
            raise HTTPException(400, str(e)) from None

    @app.post("/api/orders/submit")
    async def submit(body: SubmitBody, session: str = SESSION):
        try:
            return await sess(session).submit_order(body.preview_id, body.confirm)
        except OrderBlockedError as e:
            raise HTTPException(403, str(e)) from None

    @app.post("/api/orders/{order_id}/cancel")
    def cancel(order_id: int, session: str = SESSION):
        try:
            return sess(session).cancel_order(order_id)
        except KeyError:
            raise HTTPException(404, "No cancellable order with that id from this dashboard") from None

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app


def run() -> None:
    import uvicorn

    settings = get_settings()
    print(f"Dashboard: http://127.0.0.1:{settings.dashboard_port}")
    print(f"  paper session -> port {settings.paper_port}, live session -> port {settings.live_port}")
    uvicorn.run(create_app(settings), host="127.0.0.1", port=settings.dashboard_port, log_level="warning")
