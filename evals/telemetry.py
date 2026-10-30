"""The runner's spans: each ends as a line on the console, and goes to the collector when one is set.

The console is piloop's rich console processor. The collector is ``OTEL_EXPORTER_OTLP_ENDPOINT``, with the headers of
``OTEL_EXPORTER_OTLP_HEADERS``.

A piloop arm gets the same variables, so its daemon exports to the same collector, with ``evals.run`` in its resource.
"""

from collections.abc import Generator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from evals.model import Settings
from piloop.otel import ConsoleProcessor

SERVICE = 'piloop-evals'
tracer = trace.get_tracer('evals')


@contextmanager
def traced(settings: Settings) -> Generator[None]:
    """Send this process's spans to the console, and to the collector when one is set, until the block ends."""
    provider = TracerProvider(resource=Resource.create({'service.name': SERVICE}))
    provider.add_span_processor(ConsoleProcessor())
    if settings.otlp_endpoint:
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f'{settings.otlp_endpoint}/v1/traces'))
        )
    trace.set_tracer_provider(provider)
    try:
        yield
    finally:
        provider.shutdown()


def exported(settings: Settings, run: str) -> dict[str, str]:
    """The variables that send a piloop arm's spans to the same collector, tagged with ``run``. None without one."""
    if not settings.otlp_endpoint:
        return {}
    attrs = ','.join(a for a in (settings.otel_resource_attributes, f'evals.run={run}') if a)
    headers = settings.otlp_headers.get_secret_value() if settings.otlp_headers else ''
    found = {'OTEL_EXPORTER_OTLP_ENDPOINT': settings.otlp_endpoint, 'OTEL_RESOURCE_ATTRIBUTES': attrs}
    return found | ({'OTEL_EXPORTER_OTLP_HEADERS': headers} if headers else {})
