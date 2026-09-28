import logging
from datetime import datetime, timezone

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from cloudwatch_signoz.aws import Instance, Sample
from cloudwatch_signoz.config import Config, Target
from cloudwatch_signoz.service import CollectorService
from cloudwatch_signoz.telemetry import Telemetry, configure_telemetry


@pytest.mark.parametrize("failure", [False, True])
def test_parallel_targets_keep_trace_context_and_report_failures(failure):
    exporter = InMemorySpanExporter()
    traces = TracerProvider()
    traces.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    meters = MeterProvider(metric_readers=[reader])
    telemetry = Telemetry(traces.get_tracer("test"), meters.get_meter("test"))
    targets = (Target("123", "sa-east-1"), Target("123", "us-east-1"))
    sent = []

    class Client:
        def __init__(self, target):
            self.target = target

        def discover_instances(self):
            return [Instance("i-1", "t3.micro")]

        def collect(self, instances, lookback, metrics=None):
            if failure and self.target.region == "us-east-1":
                raise RuntimeError("AWS unavailable")
            return [Sample(self.target.account, self.target.region, instances[0], 42, datetime.now(timezone.utc))]

    class Sink:
        def send(self, samples):
            sent.extend(samples)

    try:
        service = CollectorService(Config(targets, "https://example.com", "secret"), Client, Sink(), telemetry)
        assert service.run_once() == (1 if failure else 2)
        spans = exporter.get_finished_spans()
        cycle = next(s for s in spans if s.name == "collector.cycle")
        children = [s for s in spans if s.name != "collector.cycle"]
        assert len(children) == 5
        assert all(s.parent.span_id == cycle.context.span_id for s in children)
        assert all(s.context.trace_id == cycle.context.trace_id for s in children)
        assert cycle.status.status_code == (StatusCode.ERROR if failure else StatusCode.UNSET)
        data = reader.get_metrics_data()
        exported = {m.name: m for r in data.resource_metrics for scope in r.scope_metrics for m in scope.metrics}
        assert exported["collector.samples.sent"].data.data_points[0].value == len(sent)
        errors = [p for p in exported["collector.operations"].data.data_points if p.attributes["outcome"] == "error"]
        assert len(errors) == (2 if failure else 0)
    finally:
        traces.shutdown()
        meters.shutdown()


def test_disabled_telemetry_does_not_install_log_exporter(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    logger = logging.getLogger("cloudwatch_signoz")
    previous = list(logger.handlers)
    telemetry = configure_telemetry(Config((Target("123", "sa-east-1"),), "https://example.com", "secret"))
    with telemetry.operation("test"):
        pass
    telemetry.shutdown()
    assert logger.handlers == previous
