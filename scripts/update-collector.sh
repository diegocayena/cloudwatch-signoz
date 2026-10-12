#!/usr/bin/env bash
# Run from the updated repository on the Docker host. Does not change host time.
set -euo pipefail

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_dir"
if [[ ! -f config.yaml || ! -f .env ]]; then
  echo 'Preserve config.yaml e .env da instalacao neste diretorio antes de atualizar.' >&2
  exit 1
fi
docker compose version >/dev/null

echo 'Construindo coletor a partir do checkout atual...'
docker compose build --pull collector

# Check the actual Compose image before replacing the running collector.
# With an external image, pull the corrected tag first as described in the docs.
check_code='import inspect; from cloudwatch_signoz.service import CollectorService; from cloudwatch_signoz.aws import AwsTargetClient; from cloudwatch_signoz.telemetry import Telemetry; assert "self._target_failed" in inspect.getsource(CollectorService._discover_if_due), "Imagem antiga: falta isolamento de descoberta"; assert "local_minus_aws_seconds" in inspect.getsource(CollectorService._target_failed), "Imagem antiga: falta diagnostico de horario"; assert "DeferredRefreshableCredentials" in inspect.getsource(AwsTargetClient.__init__), "Imagem antiga: falta renovacao STS"; assert "LOG.exception" not in inspect.getsource(Telemetry.operation), "Imagem antiga: logs duplicados"; print("Correcao confirmada: isolamento, diagnostico e renovacao STS")'
docker compose run --rm --no-deps --entrypoint python collector -c "$check_code"

echo 'Recriando somente o servico collector...'
docker compose up -d --no-deps --force-recreate collector
docker compose exec -T collector python -c "$check_code"
docker compose ps collector
docker compose logs --since 2m --tail 100 collector
echo 'Atualizacao concluida. Acompanhe todas as contas/regioes com:'
echo 'docker compose logs -f --since 2m collector'
echo 'Confirme discovered_instances por alvo e samples_sent; investigue qualquer *_failed.'
