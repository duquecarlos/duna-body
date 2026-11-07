"""One log line per request to the `body.request` logger: method, the matched route's path
TEMPLATE, status code, duration in ms -- never the query string, body, headers, cookies, client
IP, user agent, or exception text (host-deploy-v1-amendment-sensitive-data rule 5). An unhandled
exception is caught here, logged by its class name only, and answered with a plain 500 page;
nothing is re-raised to uvicorn's own traceback logger.
"""
from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

logger = logging.getLogger("body.request")


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    return route.path if route is not None else "-"


class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.monotonic()
        try:
            response = await call_next(request)
        except Exception as exc:
            template = _route_template(request)
            logger.info("%s %s 500 %s", request.method, template, type(exc).__name__)
            return PlainTextResponse("Internal Server Error", status_code=500)
        duration_ms = round((time.monotonic() - start) * 1000)
        template = _route_template(request)
        logger.info("%s %s %d %dms", request.method, template, response.status_code, duration_ms)
        return response
