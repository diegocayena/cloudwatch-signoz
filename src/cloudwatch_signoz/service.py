from __future__ import annotations

import logging
import threading
import time
from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from opentelemetry import trace

from .aws import AwsTargetClient, Instance, Sample
from .config import Config
from .signoz import SignozClient
from .telemetry import Telemetry

LOG = logging.getLogger(__name__)


class CollectorService:
    def __init__(
        self,
        config: Config,
        client_factory: Callable = AwsTargetClient,
        signoz: SignozClient | None = None,
        telemetry: Telemetry | None = None,
    ) -> None:
        self.config = config
        self.telemetry = telemetry or Telemetry()
        self.clients = [client_factory(target) for target in config.targets]
        self.signoz = signoz or SignozClient(
            config.signoz_endpoint, config.signoz_ingestion_key, config.request_timeout_seconds
        )
        self.instances: dict[object, list[Instance]] = {}
        self.last_discovery: float | None = None
        self.last_ebs_collection: dict[object, float] = {}

    def _discover_if_due(self) -> None:
        if (
            self.last_discovery is not None
            and time.monotonic() - self.last_discovery < self.config.discovery_interval_seconds
        ):
            return
        for client in self.clients:
            attributes = self._attributes(client)
            with self.telemetry.operation("aws.discover", attributes):
                discovered = client.discover_instances()
                self.telemetry.instances.record(len(discovered), attributes)
            self.instances[client] = discovered
            LOG.info(
                "discovered_instances count=%d account=%s region=%s",
                len(discovered), client.target.account, client.target.region,
            )
        self.last_discovery = time.monotonic()

    @staticmethod
    def _attributes(client) -> dict[str, str]:
        return {"cloud.account.id": client.target.account, "cloud.region": client.target.region}

    def _collect(self, client, include_ebs: bool) -> list[Sample]:
        attributes = self._attributes(client)
        with self.telemetry.operation("aws.collect", attributes):
            metrics = ("CPUCreditBalance", "EBSIOBalance%") if include_ebs else ("CPUCreditBalance",)
            samples = client.collect(self.instances.get(client, []), self.config.lookback_seconds, metrics=metrics)
            self.telemetry.samples.add(len(samples), attributes)
            LOG.info("samples_collected count=%d account=%s region=%s", len(samples), client.target.account, client.target.region)
            return samples

    def run_once(self) -> int:
        with self.telemetry.operation("collector.cycle"):
            return self._run_once()

    def _run_once(self) -> int:
        self._discover_if_due()
        samples: list[Sample] = []
        started = time.monotonic()
        ebs_due = {
            client for client in self.clients
            if client not in self.last_ebs_collection
            or started - self.last_ebs_collection[client] >= self.config.ebs_interval_seconds
        }
        ebs_completed = set()
        with ThreadPoolExecutor(max_workers=min(10, len(self.clients))) as executor:
            jobs = {
                executor.submit(copy_context().run, self._collect, client, client in ebs_due): client
                for client in self.clients
            }
            for future in as_completed(jobs):
                client = jobs[future]
                try:
                    samples.extend(future.result())
                    if client in ebs_due:
                        ebs_completed.add(client)
                except Exception:
                    trace.get_current_span().set_status(trace.Status(
                        trace.StatusCode.ERROR, "One or more AWS targets failed"
                    ))
                    LOG.exception(
                        "collection_failed account=%s region=%s",
                        client.target.account, client.target.region,
                    )
        with self.telemetry.operation("signoz.send"):
            self.signoz.send(samples)
            self.telemetry.sent.add(len(samples))
        for client in ebs_completed:
            self.last_ebs_collection[client] = started
        LOG.info("samples_sent count=%d", len(samples))
        return len(samples)

    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            started = time.monotonic()
            try:
                self.run_once()
            except Exception:
                LOG.exception("collection_cycle_failed")
            remaining = max(0.0, self.config.interval_seconds - (time.monotonic() - started))
            stop.wait(remaining)

