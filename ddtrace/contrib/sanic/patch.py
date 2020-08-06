import asyncio
import ddtrace
import sanic
from ddtrace.constants import ANALYTICS_SAMPLE_RATE_KEY
from ddtrace.ext import SpanTypes, http
from ddtrace.http import store_request_headers, store_response_headers
from ddtrace.propagation.http import HTTPPropagator
from ddtrace.utils.wrappers import unwrap as _u
from ddtrace.vendor import wrapt
from ddtrace.vendor.wrapt import wrap_function_wrapper as _w

from ...internal.logger import get_logger

log = get_logger(__name__)


def _extract_tags_from_request(request):
    tags = {}
    tags[http.METHOD] = request.method

    url = "{scheme}://{host}{path}".format(scheme=request.scheme, host=request.host, path=request.path)
    tags[http.URL] = url

    query_string = None
    if ddtrace.config.sanic.trace_query_string:
        query_string = request.query_string
        if isinstance(query_string, bytes):
            query_string = query_string.decode()
        tags[http.QUERY_STRING] = query_string

    return tags


def _wrap_response_callback(span, callback):
    # wrap response callbacks (either sync or async function) to set span tags
    # based on response

    def update_span(response):
        span.set_tag(http.STATUS_CODE, response.status)
        if 500 <= response.status < 600:
            span.error = 1
        store_response_headers(response.headers, span, ddtrace.config.sanic)

    @wrapt.function_wrapper
    def wrap_sync(wrapped, instance, args, kwargs):
        r = wrapped(*args, **kwargs)
        response = args[0]
        update_span(response)
        return r

    @wrapt.function_wrapper
    async def wrap_async(wrapped, instance, args, kwargs):
        r = await wrapped(*args, **kwargs)
        response = args[0]
        update_span(response)
        return r

    if asyncio.iscoroutinefunction(callback):
        return wrap_async(callback)

    return wrap_sync(callback)


def patch():
    """Patch the instrumented methods.
    """
    if getattr(sanic, "__datadog_patch", False):
        return
    setattr(sanic, "__datadog_patch", True)
    ddtrace.config._add("sanic", dict(service=ddtrace.config._get_service(default="sanic"), distributed_tracing=True))
    _w("sanic", "Sanic.handle_request", patch_handle_request)


def unpatch():
    """Unpatch the instrumented methods.
    """
    _u(sanic.Sanic, "handle_request")
    if not getattr(sanic, "__datadog_patch", False):
        return
    setattr(sanic, "__datadog_patch", False)


def patch_handle_request(wrapped, instance, args, kwargs):
    """Wrapper for Sanic.handle_request"""
    request = kwargs.get("request", args[0])
    write_callback = kwargs.get("write_callback", args[1])
    stream_callback = kwargs.get("stream_callback", args[2])

    resource = "{} {}".format(request.method, request.path)

    headers = request.headers.copy()

    if ddtrace.config.sanic.distributed_tracing:
        propagator = HTTPPropagator()
        context = propagator.extract(headers)
        if context.trace_id:
            ddtrace.tracer.context_provider.activate(context)

    with ddtrace.tracer.trace(
        "sanic.request", service=ddtrace.config.sanic.service, resource=resource, span_type=SpanTypes.WEB
    ) as span:
        sample_rate = ddtrace.config.sanic.get_analytics_sample_rate(use_global_config=True)
        if sample_rate is not None:
            span.set_tag(ANALYTICS_SAMPLE_RATE_KEY, sample_rate)

        tags = _extract_tags_from_request(request=request)
        span.set_tags(tags)

        store_request_headers(headers, span, ddtrace.config.sanic)

        if write_callback is not None:
            write_callback = _wrap_response_callback(span, write_callback)
        if stream_callback is not None:
            stream_callback = _wrap_response_callback(span, stream_callback)

        return wrapped(request, write_callback, stream_callback, **kwargs)
