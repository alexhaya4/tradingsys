"""The operational HTTP surface: health, readiness, and metrics.

Three endpoints, no application traffic. Keeping them on their own app means the
liveness probe still answers while the trading loop is busy, and means this port can be
bound to an internal interface without exposing anything else.

``/health`` is liveness: it answers as long as the event loop is running and does not
touch any dependency. ``/ready`` is readiness: it runs the registered checks and returns
503 when any fails, which is what a load balancer or orchestrator acts on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from tradingsys.core.clock import utc_now
from tradingsys.observability.correlation import CORRELATION_ID_HEADER, correlation_id
from tradingsys.observability.health import HealthRegistry
from tradingsys.observability.metrics import METRICS_CONTENT_TYPE, Metrics

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from starlette.requests import Request

    from tradingsys.config.settings import ObservabilitySettings

__all__ = ["CorrelationIdMiddleware", "build_operational_app"]

HTTP_SERVICE_UNAVAILABLE = 503


class CorrelationIdMiddleware:
    """Binds a correlation ID for the duration of each request.

    An inbound ``X-Correlation-ID`` is adopted so that a trace begun elsewhere
    continues here; otherwise one is generated. Either way it is echoed on the
    response, so a caller can quote it when reporting a problem.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.decode("latin-1").lower(): value for key, value in scope.get("headers", [])}
        inbound = headers.get(CORRELATION_ID_HEADER.lower())
        incoming = inbound.decode("latin-1") if inbound is not None else None

        with correlation_id(incoming) as bound:

            async def send_with_header(message: MutableMapping[str, Any]) -> None:
                if message["type"] == "http.response.start":
                    raw = list(message.get("headers", []))
                    raw.append((CORRELATION_ID_HEADER.encode("latin-1"), bound.encode("latin-1")))
                    message = {**message, "headers": raw}
                await send(message)

            await self.app(scope, receive, send_with_header)


def build_operational_app(
    settings: ObservabilitySettings,
    *,
    readiness: HealthRegistry,
    metrics: Metrics,
    liveness: HealthRegistry | None = None,
) -> Starlette:
    """Build the operational ASGI app.

    Args:
        settings: Paths and host binding come from here, so the endpoints can be moved
            without a code change.
        readiness: Checks run by the readiness endpoint.
        metrics: Metric set rendered by the metrics endpoint.
        liveness: Optional checks for the liveness endpoint. Normally omitted:
            liveness must not depend on anything external. Supply checks only for
            genuinely internal invariants, such as a stalled event loop.
    """

    async def health(_request: Request) -> Response:
        """Liveness. Answers as long as the process can serve a request."""
        if liveness is None:
            return JSONResponse({"status": "pass", "time": utc_now().isoformat(), "checks": []})
        report = await liveness.evaluate()
        return JSONResponse(
            {**report.to_mapping(), "time": utc_now().isoformat()},
            status_code=200 if report.healthy else HTTP_SERVICE_UNAVAILABLE,
        )

    async def ready(_request: Request) -> Response:
        """Readiness. Runs every registered dependency check."""
        report = await readiness.evaluate()
        return JSONResponse(
            {**report.to_mapping(), "time": utc_now().isoformat()},
            status_code=200 if report.healthy else HTTP_SERVICE_UNAVAILABLE,
        )

    async def metrics_endpoint(_request: Request) -> Response:
        """Prometheus exposition."""
        return Response(content=metrics.render(), media_type=METRICS_CONTENT_TYPE)

    async def not_found(_request: Request, _exc: Exception) -> Response:
        return PlainTextResponse(
            "this port serves operational endpoints only: "
            f"{settings.health_path}, {settings.ready_path}, {settings.metrics_path}",
            status_code=404,
        )

    app = Starlette(
        routes=[
            Route(settings.health_path, health, methods=["GET"]),
            Route(settings.ready_path, ready, methods=["GET"]),
            Route(settings.metrics_path, metrics_endpoint, methods=["GET"]),
        ],
        exception_handlers={404: not_found},
    )
    app.add_middleware(CorrelationIdMiddleware)
    return app
