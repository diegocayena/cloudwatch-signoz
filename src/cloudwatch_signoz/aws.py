from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import boto3

from .config import Target

T_FAMILY = re.compile(r"^t\d+[a-z]*\.")
METRICS = {
    "CPUCreditBalance": ("aws.ec2.cpu_credit_balance", "{credit}", "EC2 CPU burst credits available"),
    "EBSIOBalance%": ("aws.ec2.ebs_io_balance", "%", "EC2 EBS I/O burst credits remaining (percent)"),
}


@dataclass(frozen=True)
class Instance:
    instance_id: str
    instance_type: str
    name: str = ""
    user_id: str = ""


@dataclass(frozen=True)
class Sample:
    account: str
    region: str
    instance: Instance
    value: float
    timestamp: datetime
    metric: str = "CPUCreditBalance"


def _chunks(values: list[Instance], size: int) -> Iterable[list[Instance]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


class AwsTargetClient:
    def __init__(self, target: Target, base_session: Any | None = None) -> None:
        self.target = target
        session = base_session or boto3.Session()
        if target.role_arn:
            params: dict[str, str] = {
                "RoleArn": target.role_arn,
                "RoleSessionName": "cloudwatch-signoz",
            }
            if target.external_id:
                params["ExternalId"] = target.external_id
            creds = session.client("sts").assume_role(**params)["Credentials"]
            session = boto3.Session(
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
            )
        self.ec2 = session.client("ec2", region_name=target.region)
        self.cloudwatch = session.client("cloudwatch", region_name=target.region)

    def discover_instances(self) -> list[Instance]:
        paginator = self.ec2.get_paginator("describe_instances")
        instances: list[Instance] = []
        for page in paginator.paginate(
            Filters=[{"Name": "instance-state-name", "Values": ["pending", "running"]}]
        ):
            for reservation in page["Reservations"]:
                for item in reservation["Instances"]:
                    instance_type = item["InstanceType"]
                    if not T_FAMILY.match(instance_type):
                        continue
                    tags = {tag["Key"]: tag["Value"] for tag in item.get("Tags", [])}
                    instances.append(Instance(
                        item["InstanceId"], instance_type,
                        tags.get("Name", ""), tags.get("UserID", ""),
                    ))
        return instances

    def collect(
        self, instances: list[Instance], lookback_seconds: int,
        metrics: tuple[str, ...] = tuple(METRICS),
    ) -> list[Sample]:
        # CloudWatch permits up to 500 MetricDataQueries in one request.
        end = datetime.now(timezone.utc)
        start = end - timedelta(seconds=lookback_seconds)
        samples: list[Sample] = []
        if not metrics:
            return samples
        for batch in _chunks(instances, 500 // len(metrics)):
            requested = [(instance, metric) for instance in batch for metric in metrics]
            queries = [
                {
                    "Id": f"m{index}",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/EC2",
                            "MetricName": metric,
                            "Dimensions": [{"Name": "InstanceId", "Value": instance.instance_id}],
                        },
                        "Period": 300,
                        "Stat": "Average",
                    },
                    "ReturnData": True,
                }
                for index, (instance, metric) in enumerate(requested)
            ]
            response = self.cloudwatch.get_metric_data(
                MetricDataQueries=queries,
                StartTime=start,
                EndTime=end,
                ScanBy="TimestampDescending",
            )
            by_id = {f"m{i}": item for i, item in enumerate(requested)}
            for result in response.get("MetricDataResults", []):
                if result.get("Values") and result.get("Timestamps"):
                    samples.append(
                        Sample(
                            self.target.account,
                            self.target.region,
                            by_id[result["Id"]][0],
                            float(result["Values"][0]),
                            result["Timestamps"][0],
                            by_id[result["Id"]][1],
                        )
                    )
        return samples

