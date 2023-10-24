from __future__ import annotations

import functools
import inspect
import logging
import sys
import typing as t

from starlette.middleware import Middleware

from bentoml._internal.server.base_app import BaseAppFactory
from bentoml._internal.server.http_app import log_exception

from .service import Service

if t.TYPE_CHECKING:
    from opentelemetry.sdk.trace import Span
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import BaseRoute

    from bentoml._internal.types import LifecycleHook


class ServiceAppFactory(BaseAppFactory):
    def __init__(
        self,
        service: Service,
        *,
        timeout: int | None = None,
        max_concurrency: int | None = None,
        enable_metrics: bool = False,
    ) -> None:
        self.service = service
        self.enable_metrics = enable_metrics
        super().__init__(timeout=timeout, max_concurrency=max_concurrency)

    def __call__(self, is_main: bool = False) -> Starlette:
        app = super().__call__()
        app.add_route("/schema.json", self.schema_view, name="schema")
        if is_main:
            # TODO: enable static content
            pass
        for mount_app, path, name in self.service.mount_apps:
            app.mount(app=mount_app, path=path, name=name)
        return app

    @property
    def name(self) -> str:
        return self.service.name

    @property
    def middlewares(self) -> list[Middleware]:
        from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

        from bentoml._internal.container import BentoMLContainer

        middlewares = super().middlewares

        for middleware_cls, options in self.service.middlewares:
            middlewares.append(Middleware(middleware_cls, **options))

        def client_request_hook(span: Span | None, _scope: dict[str, t.Any]) -> None:
            from bentoml._internal.context import trace_context

            if span is not None:
                trace_context.request_id = span.context.span_id

        middlewares.append(
            Middleware(
                OpenTelemetryMiddleware,
                excluded_urls=BentoMLContainer.tracing_excluded_urls.get(),
                default_span_details=None,
                server_request_hook=None,
                client_request_hook=client_request_hook,
                tracer_provider=BentoMLContainer.tracer_provider.get(),
            )
        )

        if self.enable_metrics:
            from bentoml._internal.server.http.instruments import (
                RunnerTrafficMetricsMiddleware,
            )

            middlewares.append(Middleware(RunnerTrafficMetricsMiddleware))

        access_log_config = BentoMLContainer.runners_config.logging.access
        if access_log_config.enabled.get():
            from bentoml._internal.server.http.access import AccessLogMiddleware

            access_logger = logging.getLogger("bentoml.access")
            if access_logger.getEffectiveLevel() <= logging.INFO:
                middlewares.append(
                    Middleware(
                        AccessLogMiddleware,
                        has_request_content_length=access_log_config.request_content_length.get(),
                        has_request_content_type=access_log_config.request_content_type.get(),
                        has_response_content_length=access_log_config.response_content_length.get(),
                        has_response_content_type=access_log_config.response_content_type.get(),
                    )
                )

        return middlewares

    @property
    def on_startup(self) -> list[LifecycleHook]:
        return [*super().on_startup, *self.service.startup_hooks]

    @property
    def on_shutdown(self) -> list[LifecycleHook]:
        return [*super().on_shutdown, *self.service.shutdown_hooks]

    async def schema_view(self, request: Request) -> Response:
        from starlette.responses import JSONResponse

        schema = self.service.servable.schema()
        return JSONResponse(schema)

    @property
    def routes(self) -> list[BaseRoute]:
        from starlette.routing import Route

        routes = super().routes

        for (
            name,
            method,
        ) in self.service.servable_cls.__servable_methods__.items():
            api_endpoint = functools.partial(self.api_endpoint, name)
            route_path = method.route
            if not route_path.startswith("/"):
                route_path = "/" + route_path
            routes.append(Route(route_path, api_endpoint, methods=["POST"], name=name))
        return routes

    async def api_endpoint(self, name: str, request: Request) -> Response:
        from starlette.concurrency import run_in_threadpool
        from starlette.responses import JSONResponse

        from bentoml._internal.container import BentoMLContainer
        from bentoml._internal.context import trace_context
        from bentoml._internal.utils import is_async_callable
        from bentoml._internal.utils.http import set_cookies
        from bentoml.exceptions import BentoMLException

        from ..serde import ALL_SERDE

        media_type = request.headers.get("Content-Type", "application/json")
        serde = ALL_SERDE[media_type]()
        try:
            servable = self.service.servable
        except Exception as e:
            return JSONResponse(str(e), status_code=500)
        method = servable.__servable_methods__[name]
        func = getattr(servable, name)

        with self.service.context.in_request(request) as ctx:
            try:
                input_data = await method.input_spec.from_http_request(request, serde)
                input_params = {
                    k: getattr(input_data, k) for k in input_data.model_fields
                }
                if method.ctx_param is not None:
                    input_params[method.ctx_param] = ctx
                if is_async_callable(func):
                    output = await func(**input_params)
                elif inspect.isasyncgenfunction(func):
                    output = func(**input_params)
                else:
                    output = await run_in_threadpool(func, **input_params)

                response = await method.output_spec.to_http_response(output, serde)
                response.headers.update(
                    {"Server": f"BentoML Service/{self.service.name}"}
                )

                if method.ctx_param is not None:
                    response.status_code = ctx.response.status_code
                    response.headers.update(ctx.response.metadata)
                    set_cookies(response, ctx.response.cookies)
                if trace_context.request_id is not None:
                    response.headers["X-BentoML-Request-ID"] = str(
                        trace_context.request_id
                    )
                if (
                    BentoMLContainer.http.response.trace_id.get()
                    and trace_context.trace_id is not None
                ):
                    response.headers["X-BentoML-Trace-ID"] = str(trace_context.trace_id)
            except BentoMLException as e:
                log_exception(request, sys.exc_info())

                status = e.error_code.value
                if 400 <= status < 500 and status not in (401, 403):
                    response = JSONResponse(
                        content="BentoService error handling API request: %s" % str(e),
                        status_code=status,
                    )
                else:
                    response = JSONResponse("", status_code=status)
            except Exception:  # pylint: disable=broad-except
                # For all unexpected error, return 500 by default. For example,
                # if users' model raises an error of division by zero.
                log_exception(request, sys.exc_info())

                response = JSONResponse(
                    "An error has occurred in BentoML user code when handling this request, find the error details in server logs",
                    status_code=500,
                )
            return response