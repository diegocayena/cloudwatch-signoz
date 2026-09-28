from __future__ import annotations

import json
import urllib.request
from typing import Iterable

from .aws import METRICS, Sample


def _attr(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}


def otlp_payload(samples: Iterable[Sample]) -> dict:
    grouped: dict[str, list] = {}
    for sample in samples:
        attributes = [
            _attr("cloud.account.id", sample.account),
            _attr("cloud.region", sample.region),
            _attr("host.id", sample.instance.instance_id),
            _attr("aws.ec2.instance.type", sample.instance.instance_type),
        ]
        if sample.instance.name:
            attributes.append(_attr("host.name", sample.instance.name))
        if sample.instance.user_id:
            attributes.append(_attr("aws.ec2.tag.UserID", sample.instance.user_id))
        grouped.setdefault(sample.metric, []).append(
            {
                "attributes": attributes,
                "timeUnixNano": str(int(sample.timestamp.timestamp() * 1_000_000_000)),
                "asDouble": sample.value,
            }
        )
    return {
        "resourceMetrics": [{
            "resource": {"attributes": [_attr("service.name", "cloudwatch-signoz")]},
            "scopeMetrics": [{
                "scope": {"name": "cloudwatch-signoz", "version": "0.1.0"},
                "metrics": [{
                    "name": METRICS[metric][0],
                    "description": METRICS[metric][2],
                    "unit": METRICS[metric][1],
                    "gauge": {"dataPoints": points},
                } for metric, points in grouped.items()],
            }],
        }]
    }


class SignozClient:
    def __init__(self, endpoint: str, ingestion_key: str, timeout: int = 30) -> None:
        self.url = f"{endpoint.rstrip('/')}/v1/metrics"
        self.ingestion_key = ingestion_key
        self.timeout = timeout

    def send(self, samples: list[Sample]) -> None:
        if not samples:
            return
        body = json.dumps(otlp_payload(samples), separators=(",", ":")).encode()
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "signoz-ingestion-key": self.ingestion_key,
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            if response.status >= 300:
                raise RuntimeError(f"SigNoz returned HTTP {response.status}")

