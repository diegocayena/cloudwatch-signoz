from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Target:
    account: str
    region: str
    role_arn: str | None = None
    external_id: str | None = None


@dataclass(frozen=True)
class Config:
    targets: tuple[Target, ...]
    signoz_endpoint: str
    signoz_ingestion_key: str
    interval_seconds: int = 3600
    discovery_interval_seconds: int = 3600
    lookback_seconds: int = 7200
    request_timeout_seconds: int = 30
    log_level: str = "INFO"
    ebs_interval_seconds: int = 86400


def _env(value: Any) -> Any:
    """Expand ${NAME} values while failing early for missing secrets."""
    if not isinstance(value, str):
        return value
    expanded = os.path.expandvars(value)
    if "${" in expanded:
        raise ValueError(f"environment variable is not set: {value}")
    return expanded


def load_config(path: str | Path) -> Config:
    with Path(path).open(encoding="utf-8") as stream:
        raw = yaml.safe_load(stream) or {}
    signoz = raw.get("signoz", {})
    targets_list: list[Target] = []
    for item in raw.get("targets", []):
        account = str(_env(item["account"])).strip()
        role_arn = _env(item.get("role_arn"))
        external_id = _env(item.get("external_id"))
        configured_regions = item.get("regions", item.get("region"))
        if configured_regions is None:
            raise ValueError(f"target {account} requires region or regions")
        expanded_regions = _env(configured_regions)
        if isinstance(expanded_regions, str):
            regions = expanded_regions.split(",")
        elif isinstance(expanded_regions, list):
            regions = expanded_regions
        else:
            raise ValueError(f"invalid regions for target {account}")
        for region in regions:
            region = str(_env(region)).strip()
            if region:
                targets_list.append(Target(account, region, role_arn, external_id))
    # Preserve order while preventing duplicate CloudWatch queries.
    targets = tuple(dict.fromkeys(targets_list))
    if not targets:
        raise ValueError("at least one AWS target is required")
    endpoint = _env(signoz.get("endpoint", "" )).rstrip("/")
    key = _env(signoz.get("ingestion_key", ""))
    if not endpoint or not key:
        raise ValueError("signoz.endpoint and signoz.ingestion_key are required")
    cfg = Config(
        targets=targets,
        signoz_endpoint=endpoint,
        signoz_ingestion_key=key,
        interval_seconds=int(raw.get("interval_seconds", 3600)),
        discovery_interval_seconds=int(raw.get("discovery_interval_seconds", 3600)),
        lookback_seconds=int(raw.get("lookback_seconds", 7200)),
        request_timeout_seconds=int(raw.get("request_timeout_seconds", 30)),
        log_level=str(raw.get("log_level", "INFO")),
        ebs_interval_seconds=int(raw.get("ebs_interval_seconds", 86400)),
    )
    if cfg.interval_seconds < 60:
        raise ValueError("interval_seconds must be at least 60")
    if cfg.ebs_interval_seconds < cfg.interval_seconds:
        raise ValueError("ebs_interval_seconds must be at least interval_seconds")
    return cfg
