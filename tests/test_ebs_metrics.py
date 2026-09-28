from datetime import datetime, timezone

from cloudwatch_signoz.aws import AwsTargetClient, Instance
from cloudwatch_signoz.config import Target
from cloudwatch_signoz.signoz import otlp_payload
from cloudwatch_signoz.config import Config
from cloudwatch_signoz.service import CollectorService
import pytest


def test_ebs_daily_cpu_hourly_and_failed_send_is_retried(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("cloudwatch_signoz.service.time.monotonic", lambda: now[0])
    calls = []

    class Client:
        def __init__(self, target):
            self.target = target

        def discover_instances(self):
            return [Instance("i-1", "t3.large")]

        def collect(self, instances, lookback, metrics):
            calls.append(metrics)
            return []

    class Sink:
        fail = False

        def send(self, samples):
            if self.fail:
                raise RuntimeError("send failed")

    sink = Sink()
    cfg = Config((Target("123", "sa-east-1"),), "https://example.com", "secret")
    service = CollectorService(cfg, Client, sink)
    both = ("CPUCreditBalance", "EBSIOBalance%")
    service.run_once()
    assert calls[-1] == both
    for hour in range(1, 24):
        now[0] = hour * 3600
        service.run_once()
        assert calls[-1] == ("CPUCreditBalance",)
    now[0] = 86400
    sink.fail = True
    with pytest.raises(RuntimeError):
        service.run_once()
    assert calls[-1] == both
    now[0] += 3600
    sink.fail = False
    service.run_once()
    assert calls[-1] == both
    now[0] += 3600
    service.run_once()
    assert calls[-1] == ("CPUCreditBalance",)


def test_batches_both_metrics_and_preserves_zero_without_inventing_missing_data():
    calls = []
    timestamp = datetime(2026, 9, 28, tzinfo=timezone.utc)

    class CloudWatch:
        def get_metric_data(self, **kwargs):
            queries = kwargs["MetricDataQueries"]
            calls.append(queries)
            assert len(queries) <= 500
            results = []
            for query in queries:
                metric = query["MetricStat"]["Metric"]
                assert metric["Namespace"] == "AWS/EC2"
                assert query["MetricStat"]["Period"] == 300
                assert query["MetricStat"]["Stat"] == "Average"
                missing = metric["MetricName"] == "EBSIOBalance%" and metric["Dimensions"][0]["Value"] == "i-0"
                results.append({
                    "Id": query["Id"],
                    "Values": [] if missing else [0.0 if metric["MetricName"] == "EBSIOBalance%" else 42.0],
                    "Timestamps": [] if missing else [timestamp],
                })
            return {"MetricDataResults": list(reversed(results))}

    class Session:
        def client(self, service, **kwargs):
            return CloudWatch() if service == "cloudwatch" else object()

    client = AwsTargetClient(Target("123", "sa-east-1"), Session())
    instances = [Instance(f"i-{i}", "t3.large", "host", "user-1") for i in range(251)]
    samples = client.collect(instances, 7200)
    assert [len(batch) for batch in calls] == [500, 2]
    cpu = [s for s in samples if s.metric == "CPUCreditBalance"]
    ebs = [s for s in samples if s.metric == "EBSIOBalance%"]
    assert len(cpu) == 251
    assert len(ebs) == 250
    assert all(s.value == 0 and s.instance.instance_id != "i-0" for s in ebs)
    metrics = otlp_payload(samples)["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    by_name = {metric["name"]: metric for metric in metrics}
    assert by_name["aws.ec2.cpu_credit_balance"]["unit"] == "{credit}"
    assert by_name["aws.ec2.ebs_io_balance"]["unit"] == "%"
    points = by_name["aws.ec2.ebs_io_balance"]["gauge"]["dataPoints"]
    assert len(points) == 250
    attrs = {attr["key"]: attr["value"]["stringValue"] for attr in points[0]["attributes"]}
    assert attrs["host.name"] == "host"
    assert attrs["aws.ec2.tag.UserID"] == "user-1"
