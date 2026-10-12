# Resolver RequestExpired no host EC2

`RequestExpired` significa que a AWS rejeitou o tempo da requisição. A causa
mais provável neste caso é relógio divergente, mas atraso de transporte também
é possível. Não é o mesmo erro que `ExpiredToken`: trocar a chave não corrige
o relógio. Alterar o fuso horário também não corrige a hora UTC.

O log fornecido usa uma versão antiga: `_discover_if_due` deixa a exceção
escapar e `telemetry.operation` imprime `operation_failed`. O código atual
isola falhas por alvo e registra a diferença aproximada para o cabeçalho Date
da AWS. Atualizar a imagem e corrigir a sincronização são etapas separadas.

## Atualizar somente o container, sem alterar a VM

Na cópia atualizada do repositório na VM, preservando `.env` e `config.yaml`:

```bash
bash scripts/update-collector.sh
```

Esse é o caminho inicial quando você deseja manter o relógio do host intacto.
O script constrói a imagem, verifica o código antes de substituir o container,
recria somente `collector` e confirma a correção dentro do container iniciado.
Verifica isolamento de falhas, diagnóstico de horário, renovação STS e remoção
de logs duplicados. Não instala pacotes na VM nem executa o script de NTP.
Execute fora do container, com acesso ao Docker. A construção precisa de rede.

Para instalações com `image:` em vez de `build:`, primeiro publique uma nova
tag corrigida, atualize o Compose e execute `docker compose pull collector`.
O script recusa a implantação se a imagem ainda contiver o código antigo.

Esta atualização mantém os outros alvos coletando e identifica o alvo com
`RequestExpired`; ela não compensa timestamps de assinatura nem garante que
o alvo com erro volte a funcionar. Use os novos logs para confirmar a causa.

## 1. Corrigir a VM

No host EC2 que executa o Docker, dentro da cópia atualizada deste repositório:

```bash
sudo bash scripts/fix-ec2-clock.sh
```

O script suporta Ubuntu/Debian e sistemas com dnf/yum e systemd. Instala chrony,
preserva a configuração original em `.before-cloudwatch-signoz`, configura
Amazon Time Sync (`169.254.169.123`), habilita no boot e exige sincronização
com correção inferior a 100 ms. O ajuste pode mover imediatamente o relógio
da VM. Execute na VM EC2, não no computador local nem dentro do container.
Precisa de acesso ao repositório de pacotes e UDP 123 ao servidor de tempo.
Se terminar com erro, não considere o relógio corrigido.

Confira após a execução e novamente após reiniciar a VM:

```bash
chronyc sources -n
chronyc tracking
timedatectl status
```

O servidor `169.254.169.123` deve aparecer com `^*`; `Leap status` deve ser
`Normal`. A configuração é persistente: não depende de reiniciar o coletor
periodicamente. Em sistemas diferentes, siga a
[configuração oficial AWS](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configure-ec2-ntp.html).

## 2. Atualizar a aplicação em execução

Para o `compose.yaml` deste repositório, que usa `build: .`, atualize o checkout
para o commit com as correções e execute no diretório da aplicação:

```bash
docker compose build --pull collector
docker compose up -d --force-recreate collector
docker compose exec -T collector python -c 'import inspect; from cloudwatch_signoz.service import CollectorService; s=inspect.getsource(CollectorService._discover_if_due); assert "self._target_failed" in s, "Imagem antiga"; print("Isolamento de falhas presente")'
docker compose logs --since 10m collector
```

Se o Compose foi adaptado para `image: ghcr.io/...`, publique uma tag nova com
as correções, altere `image:` para essa tag e use `docker compose pull collector`
antes de `docker compose up -d --force-recreate collector`. `restart` sozinho
não atualiza a imagem nem as variáveis de ambiente do container.

## 3. Validar todas as regiões

O container recriado inicia um ciclo imediatamente. Confirme nos logs
`discovered_instances` para cada conta/região configurada e `samples_sent`.
Observe também o próximo ciclo programado. Somente `samples_sent` não prova
que todas as regiões funcionaram, pois falhas agora são isoladas.

Se houver `discovery_failed` ou `collection_failed`, confira `account`, `region`,
`aws_error`, `request_id` e `local_minus_aws_seconds` (quando Date estiver disponível).
Valor positivo significa relógio local adiantado; negativo, atrasado. Diferenças
pequenas não comprovam problema de relógio: com NTP saudável, investigue proxy,
endpoint personalizado, latência e fila de requisições do alvo que falhou,
preservando o request ID para suporte AWS. Não exponha chaves nos diagnósticos.

Para `ExpiredToken`, credenciais temporárias fixadas no ambiente precisam de
renovação externa e recriação do container. Roles configuradas em `role_arn`
já usam renovação automática no código atual.
