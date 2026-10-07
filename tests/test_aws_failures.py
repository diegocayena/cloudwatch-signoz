import logging
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from botocore.exceptions import ClientError
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from cloudwatch_signoz.aws import AwsTargetClient, Instance, Sample
from cloudwatch_signoz.config import Config, Target
from cloudwatch_signoz.service import CollectorService
from cloudwatch_signoz.telemetry import Telemetry


def test_role_credentials_are_lazy_and_refresh_for_both_clients(monkeypatch):
    now = [datetime.now(timezone.utc)]
    calls = []

    class STS:
        def assume_role(self, **kwargs):
            calls.append(kwargs)
            return {"Credentials": {
                "AccessKeyId": f"key-{len(calls)}", "SecretAccessKey": "secret",
                "SessionToken": "token", "Expiration": now[0] + timedelta(hours=1),
            }}

    class Session:
        def client(self, service, **kwargs):
            assert service == "sts"
            assert kwargs["region_name"] == "sa-east-1"
            return STS()

    client = AwsTargetClient(Target("123", "sa-east-1", "arn:aws:iam::123:role/test", "external"), Session())
    assert calls == []
    credentials = client.ec2._request_signer._credentials
    assert credentials is client.cloudwatch._request_signer._credentials
    monkeypatch.setattr(credentials, "_time_fetcher", lambda: now[0])
    assert credentials.get_frozen_credentials().access_key == "key-1"
    now[0] += timedelta(minutes=50)
    assert credentials.get_frozen_credentials().access_key == "key-2"
    assert calls == [{"RoleArn": "arn:aws:iam::123:role/test",
                      "RoleSessionName": "cloudwatch-signoz", "ExternalId": "external"}] * 2
    client.ec2.close()
    client.cloudwatch.close()


def test_failed_discovery_is_isolated_retried_and_keeps_cached_instances(monkeypatch, caplog):
    now = [0.0]
    monkeypatch.setattr("cloudwatch_signoz.service.time.monotonic", lambda: now[0])
    discoveries = []
    collected = []
    failure = [True]
    targets = (Target("123", "sa-east-1"), Target("456", "us-east-1"))
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    class Client:
        def __init__(self, target):
            self.target = target

        def discover_instances(self):
            discoveries.append(self.target.account)
            if self.target.account == "456" and failure[0]:
                raise ClientError({"Error": {"Code": "RequestExpired", "Message": "Request has expired"},
                                   "ResponseMetadata": {"RequestId": "request-1", "HTTPHeaders": {
                                       "date": format_datetime(datetime.now(timezone.utc) - timedelta(minutes=20))}}},
                                  "DescribeInstances")
            return [Instance("i-1", "t3.micro")]

        def collect(self, instances, lookback, metrics):
            collected.append(self.target.account)
            return [Sample(self.target.account, self.target.region, instances[0], 42, datetime.now(timezone.utc))]

    class Sink:
        def send(self, samples):
            pass

    try:
        service = CollectorService(Config(targets, "https://example.com", "secret"), Client, Sink(),
                                   Telemetry(provider.get_tracer("test")))
        with caplog.at_level(logging.ERROR):
            assert service.run_once() == 1
        assert collected == ["123"]
        errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert errors[0].exc_info is None
        assert "account=456 region=us-east-1 aws_error=RequestExpired" in errors[0].message
        assert "local_minus_aws_seconds=" in errors[0].message
        assert next(s for s in exporter.get_finished_spans() if s.name == "collector.cycle").status.status_code == StatusCode.ERROR
        now[0] = 60
        failure[0] = False
        assert service.run_once() == 2
        assert discoveries == ["123", "456", "456"]
        now[0] = 3600
        failure[0] = True
        assert service.run_once() == 2
        now[0] = 3660
        assert service.run_once() == 2
        assert discoveries[-1] == "456"
        assert len(service.instances) == 2
    finally:
        provider.shutdown()


def test_nested_telemetry_does_not_duplicate_exception_logs(caplog):
    import pytest

    telemetry = Telemetry()
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        with telemetry.operation("collector.cycle"), telemetry.operation("aws.discover"):
            raise RuntimeError("failure")
    assert not caplog.records
