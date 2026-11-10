from datetime import datetime, timezone

from cloudwatch_signoz.aws import AwsTargetClient, Instance, Sample
from cloudwatch_signoz.config import Config, Target, load_config
from cloudwatch_signoz.service import CollectorService
from cloudwatch_signoz.signoz import otlp_payload


class Paginator:
    def paginate(self, **kwargs):
        return [{"Reservations": [{"Instances": [
            {"InstanceId": "i-t", "InstanceType": "t3.micro", "Tags": [
                {"Key": "Name", "Value": "api"}, {"Key": "UserID", "Value": "user-123"},
            ]},
            {"InstanceId": "i-m", "InstanceType": "m6i.large"},
        ]}]}]


class EC2:
    def describe_instance_credit_specifications(self, **kwargs):
        assert kwargs == {"InstanceIds": ["i-t"]}
        return {"InstanceCreditSpecifications": [{"InstanceId": "i-t", "CpuCredits": "unlimited"}]}

    def get_paginator(self, name):
        assert name == "describe_instances"
        return Paginator()


class CloudWatch:
    def get_metric_data(self, **kwargs):
        assert kwargs["MetricDataQueries"][0]["MetricStat"]["Period"] == 300
        return {"MetricDataResults": [{
            "Id": "m0", "Values": [42.0], "Timestamps": [datetime(2026, 1, 1, tzinfo=timezone.utc)]
        }]}


class Session:
    def client(self, service, **kwargs):
        return EC2() if service == "ec2" else CloudWatch()


def test_first_cycle_discovers_before_interval_and_refreshes_when_due(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("cloudwatch_signoz.service.time.monotonic", lambda: now[0])
    target = Target("123", "sa-east-1")
    client = AwsTargetClient(target, Session())
    discoveries = []
    discover = client.discover_instances

    def tracked_discover():
        discoveries.append(now[0])
        return discover()

    monkeypatch.setattr(client, "discover_instances", tracked_discover)
    sent = []

    class Sink:
        def send(self, samples):
            sent.extend(samples)

    service = CollectorService(
        Config((target,), "https://example.com", "test"),
        client_factory=lambda _: client,
        signoz=Sink(),
    )
    assert service.run_once() == 1
    now[0] = 60.0
    assert service.run_once() == 1
    assert discoveries == [0.0]
    now[0] = 3600.0
    assert service.run_once() == 1
    assert discoveries == [0.0, 3600.0]
    assert len(sent) == 3


def test_discovers_only_t_family_and_collects_latest_value():
    client = AwsTargetClient(Target("123", "sa-east-1"), Session())
    instances = client.discover_instances()
    assert instances == [Instance("i-t", "t3.micro", "api", "user-123", "unlimited")]
    sample = client.collect(instances, 600)[0]
    assert sample.value == 42.0
    assert sample.account == "123"
    attrs = otlp_payload([sample])["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["gauge"]["dataPoints"][0]["attributes"]
    assert {a["key"]: a["value"]["stringValue"] for a in attrs}["aws.ec2.tag.UserID"] == "user-123"
    assert {a["key"]: a["value"]["stringValue"] for a in attrs}["aws.ec2.cpu_credit_mode"] == "unlimited"


def test_otlp_payload_has_filters_required_by_proposal():
    sample = Sample(
        "123", "sa-east-1", Instance("i-1", "t4g.small", "worker"), 10.5,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    payload = otlp_payload([sample])
    metric = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]
    assert metric["name"] == "aws.ec2.cpu_credit_balance"
    attrs = metric["gauge"]["dataPoints"][0]["attributes"]
    assert {a["key"] for a in attrs} >= {"cloud.account.id", "cloud.region", "host.id", "host.name"}
    assert "aws.ec2.tag.UserID" not in {a["key"] for a in attrs}
    assert "aws.ec2.cpu_credit_mode" not in {a["key"] for a in attrs}


def test_credit_modes_are_batched_paginated_and_missing_modes_are_not_guessed():
    calls = []

    class ManyInstances:
        def paginate(self, **kwargs):
            return [{"Reservations": [{"Instances": [
                {"InstanceId": f"i-{index}", "InstanceType": "t3.micro"}
                for index in range(1001)
            ]}]}]

    class CreditEC2:
        def get_paginator(self, name):
            return ManyInstances()

        def describe_instance_credit_specifications(self, **kwargs):
            calls.append(kwargs.copy())
            if kwargs["InstanceIds"] == ["i-1000"]:
                return {"InstanceCreditSpecifications": []}
            if "NextToken" in kwargs:
                return {"InstanceCreditSpecifications": [
                    {"InstanceId": "i-1", "CpuCredits": "unlimited"},
                    {"InstanceId": "i-2", "CpuCredits": "unexpected"},
                ]}
            return {"InstanceCreditSpecifications": [
                {"InstanceId": "i-0", "CpuCredits": "standard"},
            ], "NextToken": "page-2"}

    client = AwsTargetClient(Target("123", "sa-east-1"), Session())
    client.ec2 = CreditEC2()
    instances = client.discover_instances()
    assert [len(call["InstanceIds"]) for call in calls] == [1000, 1000, 1]
    assert calls[1]["NextToken"] == "page-2"
    assert "NextToken" not in calls[2]
    assert [item.cpu_credit_mode for item in instances[:3]] == ["standard", "unlimited", ""]
    assert instances[-1].cpu_credit_mode == ""


def test_empty_discovery_does_not_query_credit_specifications():
    class EmptyEC2:
        def get_paginator(self, name):
            return self

        def paginate(self, **kwargs):
            return [{"Reservations": []}]

        def describe_instance_credit_specifications(self, **kwargs):
            raise AssertionError("An empty inventory must not query the entire account")

    client = AwsTargetClient(Target("123", "sa-east-1"), Session())
    client.ec2 = EmptyEC2()
    assert client.discover_instances() == []


def test_config_expands_account_regions_and_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    monkeypatch.setenv("AWS_REGIONS", "sa-east-1, us-east-1,sa-east-1")
    monkeypatch.setenv("SIGNOZ_INGESTION_KEY", "secret")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
signoz:
  endpoint: https://ingest.example.com
  ingestion_key: ${SIGNOZ_INGESTION_KEY}
targets:
  - account: ${AWS_ACCOUNT_ID}
    regions: ${AWS_REGIONS}
""",
        encoding="utf-8",
    )
    config = load_config(config_file)
    assert config.targets == (
        Target("123456789012", "sa-east-1"),
        Target("123456789012", "us-east-1"),
    )
    assert config.signoz_ingestion_key == "secret"
