"""Application telemetry, separate from the EC2 business metrics."""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from time import perf_counter

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from . import __version__
from .config import Config

LOG = logging.getLogger(__name__)


class Telemetry:
    def __init__(self, tracer=None, meter=None):
        self.tracer = tracer or trace.NoOpTracerProvider().get_tracer(__name__)
        meter = meter or metrics.NoOpMeterProvider().get_meter(__name__)
        self.operations = meter.create_counter("collector.operations", unit="{operation}")
        self.duration = meter.create_histogram("collector.operation.duration", unit="s")
        self.samples = meter.create_counter("collector.samples.collected", unit="{sample}")
        self.sent = meter.create_counter("collector.samples.sent", unit="{sample}")
        self.instances = meter.create_histogram("collector.discovery.instances", unit="{instance}")
        self.providers = []
        self.handler = None

    @contextmanager
    def operation(self, name, attributes=None):
        attributes = dict(attributes or {})
        started = perf_counter()
        status = "success"
        with self.tracer.start_as_current_span(name, attributes=attributes) as span:
            try:
                yield span
            except Exception:
                status = "error"
                raise
            finally:
                if span.is_recording() and span.status.status_code == trace.StatusCode.ERROR:
                    status = "error"
                labels = {**attributes, "operation": name, "outcome": status}
                self.operations.add(1, labels)
                self.duration.record(perf_counter() - started, labels)

    def shutdown(self):
        if self.handler:
            logging.getLogger("cloudwatch_signoz").removeHandler(self.handler)
            self.handler.close()
        for provider in self.providers:
            try:
                provider.shutdown()
            except Exception:
                LOG.exception("telemetry_shutdown_failed")


def configure_telemetry(config: Config) -> Telemetry:
    if os.getenv("OTEL_SDK_DISABLED", "false").lower() == "true":
        return Telemetry()
    resource = Resource.create({
        "service.name": os.getenv("OTEL_SERVICE_NAME") or "cloudwatch-signoz",
        "service.version": __version__,
    })
    endpoint = (os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or config.signoz_endpoint).rstrip("/")
    options = {"headers": {"signoz-ingestion-key": config.signoz_ingestion_key}, "timeout": 10}
    traces = TracerProvider(resource=resource)
    traces.add_span_processor(BatchSpanProcessor(
        OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", **options)
    ))
    meters = MeterProvider(resource=resource, metric_readers=[PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics", **options),
        export_interval_millis=60000,
    )])
    logs = LoggerProvider(resource=resource)
    logs.add_log_record_processor(BatchLogRecordProcessor(
        OTLPLogExporter(endpoint=f"{endpoint}/v1/logs", **options)
    ))
    telemetry = Telemetry(traces.get_tracer(__name__), meters.get_meter(__name__))
    telemetry.providers = [traces, meters, logs]
    telemetry.handler = LoggingHandler(logger_provider=logs)
    # Export only our application logs: exporter errors remain on stderr and
    # cannot recursively enter the log exporter. Console logging stays enabled.
    logging.getLogger("cloudwatch_signoz").addHandler(telemetry.handler)
    return telemetry
