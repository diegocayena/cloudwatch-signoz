# Atualizar o coletor sem alterar o relógio da VM

Uma falha AWS na descoberta de uma conta/região interrompia todo o ciclo.
A correção isola falhas por alvo, mantém o inventário anterior quando disponível
e tenta novamente no próximo ciclo. Alvos saudáveis continuam coletando.
Credenciais de `role_arn` agora são renovadas automaticamente pelo botocore,
com carregamento adiado para que uma role indisponível não impeça a inicialização.

## Implantar após o merge na main

No host Docker, dentro do repositório, preservando `.env` e `config.yaml`:

```bash
cd /opt/cloudwatch-signoz
git switch main
git pull --ff-only
bash scripts/update-collector.sh
```

O script constrói a imagem do checkout atual, verifica as correções antes de
substituir o container, recria somente `collector` e verifica novamente o código
em execução. Não altera o horário nem instala pacotes na VM. Não precisa de
`docker compose down`. Execute com acesso ao Docker; a construção precisa de rede.
Se houver alterações locais que impeçam o pull, preserve-as antes de atualizar.

Se seu Compose usa `image:` em vez de `build:`, publique uma nova tag corrigida,
atualize `image:` e execute `docker compose pull collector` antes do script.
O script recusa a implantação se detectar a imagem antiga. Reiniciar um container
sozinho não atualiza a imagem nem suas variáveis de ambiente.

## Confirmar o resultado

```bash
docker compose logs -f --since 2m collector
```

Confirme `discovered_instances` por conta/região configurada e `samples_sent`.
Observe também o próximo ciclo. `samples_sent` sozinho não confirma sucesso de
todos os alvos, pois as falhas são isoladas.

Erros passam a aparecer uma vez, com `discovery_failed` ou `collection_failed`,
`account`, `region`, `aws_error` e `request_id`. Para `RequestExpired`,
`RequestTimeTooSkewed` e `RequestInTheFuture`, aparece também
`check_host_clock_and_ntp=true`. Quando a resposta contém Date,
`local_minus_aws_seconds` informa a diferença aproximada: positiva significa
relógio local adiantado; negativa, atrasado.

## Limites e próximos diagnósticos

Esta correção mantém as outras regiões funcionando e permite identificar a
origem da falha. Ela não compensa timestamps de assinatura nem garante recuperar
um alvo que continua recebendo `RequestExpired`. O relógio do container é
compartilhado com o host; alterar `TZ` não ajusta a hora UTC.

Com diferença de horário significativa, confira no host `date -u`,
`timedatectl status` e `chronyc tracking`, quando disponível. A AWS documenta a
[sincronização de tempo EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configure-ec2-ntp.html).
Com NTP saudável e diferença pequena, investigue proxy, endpoint personalizado,
latência e fila do alvo que falhou, preservando o request ID para suporte AWS.
Sucesso em quatro regiões segundos antes da falha não comprova relógio divergente.

`ExpiredToken` é outro erro: credenciais temporárias fixadas no ambiente ainda
exigem renovação externa e recriação do container. Prefira IAM Role ou perfil
renovável; a renovação automática implementada cobre as roles em `role_arn`.

## Validação de desenvolvimento

```bash
python -m pip install -e '.[test]'
python -m pytest -q
bash -n scripts/update-collector.sh
```

Os testes cobrem renovação STS, isolamento de descoberta, nova tentativa,
preservação do inventário anterior e ausência de logs duplicados.
