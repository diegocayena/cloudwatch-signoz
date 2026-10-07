# CloudWatch → SigNoz

## Falhas AWS e credenciais temporárias

Falhas de descoberta e coleta são isoladas por conta/região. Uma descoberta
malsucedida é tentada novamente no próximo ciclo, sem redescobrir alvos que
ainda estão dentro do intervalo. Se houver uma descoberta anterior, suas
instâncias são usadas até a próxima atualização bem-sucedida; um alvo sem
descoberta bem-sucedida é ignorado na coleta. O trace do ciclo registra erro
mesmo quando as métricas dos outros alvos são enviadas normalmente.

Roles configuradas com `role_arn` usam credenciais renováveis pelo botocore,
inclusive durante paginação e coleta. A primeira chamada STS é adiada até o
uso do alvo. O STS usa a região do alvo; os clientes têm timeouts e até três
tentativas no modo standard para falhas transitórias suportadas pelo SDK.
Credenciais temporárias fornecidas diretamente pelo ambiente continuam
dependendo de renovação externa; prefira uma IAM Role ou um perfil renovável.

Cada falha AWS gera um log com `account`, `region`, `aws_error` e `request_id`,
sem repetir o stack trace nas camadas de telemetria. Para `RequestExpired`,
`RequestTimeTooSkewed` e `RequestInTheFuture`, o log indica verificar o relógio
e NTP. Quando a resposta contém o cabeçalho Date, `local_minus_aws_seconds`
mostra a diferença aproximada entre o relógio local e a AWS (positiva quando
o relógio local está adiantado). O coletor não altera o relógio do servidor.
Verifique no host Linux com `date -u`, `timedatectl status` e, se instalado,
`chronyc tracking`; corrija a sincronização no host que executa o container.

A aplicação também envia logs, traces e métricas operacionais ao SigNoz.
Consulte [instrumentação e validação](docs/telemetry.md).

## Publicação da imagem no GitHub

O workflow `.github/workflows/publish-image.yml` executa os testes e publica a
imagem no GitHub Container Registry (GHCR) quando uma tag é enviada ao GitHub.
O commit marcado deve conter o workflow. Não é necessário cadastrar secrets:
a autenticação usa o `GITHUB_TOKEN` automático, com `packages: write`.

Após commitar e enviar as alterações, publique uma versão:

```bash
git tag v0.2.0
git push origin v0.2.0
```

Acompanhe a execução na aba **Actions**. Ao terminar com sucesso, a imagem
estará em **Packages**, com o endereço:

```text
ghcr.io/diegoeneres/cloudwatch-signoz:v0.2.0
```

A imagem é construída para `linux/amd64`. Cada tag Git gera uma tag de imagem
(caracteres incompatíveis com tags Docker são normalizados pela metadata-action).
Não é criada uma tag `latest`; utilize a versão desejada explicitamente.

Para baixar na VM:

```bash
docker pull ghcr.io/diegoeneres/cloudwatch-signoz:v0.2.0
```

Se o pacote for privado, autentique primeiro com `docker login ghcr.io`, usando
seu usuário GitHub e um token com `read:packages` como senha. Para usar a imagem
no Compose, substitua `build: .` por
`image: ghcr.io/diegoeneres/cloudwatch-signoz:v0.2.0` e execute
`docker compose up -d`. Preserve as configurações de ambiente e volume.

Referência: https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images

Serviço Python que implementa o coletor descrito na proposta executiva: descobre instâncias EC2
T-family em múltiplas contas/regiões, consulta `AWS/EC2/CPUCreditBalance` a cada hora e
envia a métrica ao SigNoz Cloud via OTLP/HTTP JSON.

## Executar

1. Copie os exemplos: `cp config.example.yaml config.yaml` e `cp .env.example .env`.
2. No `.env`, informe `AWS_ACCOUNT_ID`, `AWS_REGIONS`, `AWS_ACCESS_KEY_ID`,
   `AWS_SECRET_ACCESS_KEY` e `SIGNOZ_INGESTION_KEY`. Use `AWS_SESSION_TOKEN` apenas para
   credenciais temporárias.
3. Em `config.yaml`, substitua `<REGION>` no endpoint do SigNoz pela região do seu ambiente
   SigNoz Cloud (ela pode ser diferente da região AWS).
4. Execute `docker compose up -d --build`.

O Docker Compose injeta as credenciais no container e o `boto3` as carrega automaticamente. Os
arquivos `.env` e `config.yaml` estão no `.gitignore` para evitar o versionamento de segredos.

Defina todas as regiões AWS em uma lista separada por vírgulas, sem espaços:

```env
AWS_REGIONS=sa-east-1,us-east-1,us-east-2,us-west-2,eu-west-1
```

O serviço cria um coletor lógico para cada região, consulta todas em paralelo e identifica cada
amostra com o atributo `cloud.region`. Alterar o `.env` e reiniciar o container adiciona ou remove
regiões da coleta.

Para validar uma única coleta localmente:

```bash
python -m venv .venv
.venv/bin/pip install -e '.[test]'
SIGNOZ_INGESTION_KEY=... .venv/bin/cloudwatch-signoz --config config.yaml --once
```

## AWS IAM

Anexe `iam-policy.json` ao usuário IAM dono da Access Key. Para consultar outra conta, a
credencial-base também precisa de `sts:AssumeRole`, e a role de destino deve confiar nesse usuário.
`external_id` é aceito por alvo quando exigido pela trust policy. Em produção, prefira uma IAM
Role associada à VM em vez de credenciais permanentes sempre que isso for possível.

## Métrica e dashboard

O coletor também consulta `AWS/EC2/EBSIOBalance%` e envia ao SigNoz como
`aws.ec2.ebs_io_balance`, gauge em percentual de 0 a 100, com os mesmos atributos
de conta, região, instância, `host.name` e `aws.ec2.tag.UserID`.
A consulta usa `Average` e período de 300 segundos. EBS é consultado na primeira
execução e depois a cada 24 horas por conta/região (`ebs_interval_seconds: 86400`).
CPU continua no intervalo atual (`interval_seconds: 3600` por padrão).
O agendamento de EBS é verificado em cada ciclo de CPU: se os intervalos não
forem múltiplos, a consulta ocorre no primeiro ciclo após completar o prazo.
O relógio fica em memória; reiniciar o serviço ou executar `--once` coleta EBS
novamente. Falhas de coleta ou envio permitem nova tentativa no próximo ciclo.
Uma consulta sem dados também conta como executada, evitando consultar a cada
hora instâncias que não suportam EBSIOBalance%.
A coleta diária envia o saldo mais recente da janela de consulta, não a média
nem o mínimo das últimas 24 horas. Use um período de pelo menos 26 horas para
visualizar EBS no SigNoz, considerando o intervalo diário e o atraso das amostras.
Essa métrica representa créditos de I/O EBS da instância e só está disponível
nos tipos compatíveis. Ausência de dados não é convertida em zero.
O escopo de descoberta continua sendo EC2 T-family; instâncias de outras
famílias não são incluídas por esta alteração. Não são necessárias permissões
IAM adicionais. O dashboard e o alerta existentes continuam dedicados a CPU.

São até 250 instâncias por lote quando as duas métricas estão previstas e 500
quando somente CPU está prevista, respeitando o limite de 500 consultas por
chamada. `samples_sent` passa a contar as amostras
das duas métricas, e não a quantidade de instâncias.

Disponibilidade: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/viewing_metrics_with_cloudwatch.html

- Nome: `aws.ec2.cpu_credit_balance`
- Unidade: crédito
- Atributos: `cloud.account.id`, `cloud.region`, `host.id`, `host.name` e
  `aws.ec2.instance.type`

Esses atributos permitem os filtros solicitados na proposta. No SigNoz, crie um dashboard com a
métrica acima e um alerta conforme o limite operacional escolhido. Um limite universal não foi
fixado: o impacto de saldo baixo depende do tipo e do modo de créditos (Standard/Unlimited).

O serviço coleta e redescobre instâncias a cada hora, agrupa até 500 métricas por chamada do
CloudWatch e consulta regiões em paralelo. Além dos saldos de créditos, não coleta utilização de CPU, memória ou disco. A janela de consulta
é de duas horas para tolerar atrasos de publicação; somente a amostra mais recente é enviada.
