# Sigenergy API — contrato implementado

A integração executa leituras de descoberta, estado live e histórico diário. Não
implementa controlo de bateria, alterações de modo nem outros comandos remotos.
Existe uma única escrita remota opcional, o pedido de acesso
`POST /openapi/board/onboard`; só é executada por uma ação explícita e confirmada
na página de Integrações e fica auditada localmente.

## Matriz de capacidades

| Capacidade | Contrato oficial / canal | Uso local | Estado |
| --- | --- | --- | --- |
| Autenticação | `POST /openapi/auth/login/key` | Token Bearer sanitizado e renovado quando necessário | Implementado |
| Descoberta | `GET /openapi/system` | Inventário de todos os sistemas autorizados; nunca usa uma allowlist manual | Implementado |
| Estado atual | `GET /openapi/systems/{systemId}/energyFlow` | Snapshots de potência, SOC e estado; não produz kWh | Implementado |
| Histórico diário | `GET /openapi/systems/{systemId}/history`, corpo `{"level":"Day","date":"AAAA-MM-DD"}` | Backfill limitado, fila `production`, factos diários e materialização mensal | Implementado, mas condicionado por permissão e unidade confirmada |
| Data Subscription | MQTT, com pedidos de subscrição num tópico próprio | Não existe webhook HTTP porque esse não é o transporte oficial documentado | A aguardar parâmetros MQTT entregues pela Sigenergy |
| Telemetria periódica | Tópico MQTT; período normal de 5 minutos, alterável com suporte Sigenergy | Não ativa polling adicional | Não ligado |
| Alarmes e alterações | Tópicos MQTT separados | Sem consumidor local enquanto faltarem broker e tópicos reais | Não ligado |
| Pedido de acesso | `POST /openapi/board/onboard` | Ação explícita, confirmada e auditável; nunca automática | Implementado |
| Comandos remotos | Fora do âmbito | Nenhum | Não suportado |

Documentação oficial consultada:

- [System history](https://developer.sigencloud.com/user/api/document/32)
- [Data Subscription](https://developer.sigencloud.com/user/api/document/45)
- [System telemetry](https://developer.sigencloud.com/user/api/document/51)
- [Telemetry signal list](https://developer.sigencloud.com/user/api/document/63)
- [MQTT service alignment](https://developer.sigencloud.com/user/api/document/39)

## Regras energéticas

`energyFlow` contém potência instantânea. Os campos `pvPower`, `gridPower`,
`batteryPower` e equivalentes nunca são integrados nem convertidos em energia.

O histórico oficial devolve totais como `powerGeneration`, `powerUse`,
`powerSelfConsumption`, `powerOneself`, `powerToGrid`, `powerFromGrid`,
`esCharging` e `esDischarging`. A página distingue
`powerSelfConsumption` (“Self-consumed green power”) de `powerOneself`
(“Load green power consumption”). O segundo é materializado como
`self_use_kwh`, porque representa a energia verde realmente fornecida à carga e
mantém o balanço consumo = energia local + importação. O primeiro permanece no
payload sanitizado, sem ser confundido com energia faturável.

A página oficial descreve estes totais como energia, mas não identifica
inequivocamente a unidade. Por isso:

1. O payload é recusado enquanto a unidade não estiver confirmada como kWh.
2. A confirmação operacional é `SIGENERGY_HISTORY_ENERGY_UNIT=kWh`.
3. Só os campos presentes e numéricos são persistidos.
4. Nenhum valor ausente é substituído por zero.
5. O payload sanitizado, período, timezone `Europe/Lisbon`, granularidade,
   proveniência e qualidade ficam em `energy_interval_facts`.

Cada dia é materializado de forma idempotente em `production_records`. Um mês
anterior só é `complete` quando existem todos os dias e todos os campos
energéticos centrais — produção, consumo, autoconsumo, exportação e importação —
estão completos. Caso contrário fica `partial`, `missing`, `conflict` ou
`in_progress`. Os relatórios leem exclusivamente estes registos locais e nunca
chamam a Sigenergy durante a geração.

Uma fonte energética primária é única por asset. A Sigenergy não pode ser
selecionada sem pelo menos um mês completo. Se o mesmo asset também tiver
FusionSolar, a troca exige confirmação explícita.

## Fila, quotas e limites

Descoberta, estado, produção, telemetria e alarmes têm áreas distintas na fila
persistente, mas partilham a lease da conta. Importação, preview, onboarding e
histórico passam pela mesma fila.

A documentação do histórico limita uma conta a uma leitura de uma estação a
cada cinco minutos. A área `production` usa por defeito:

```dotenv
SIGENERGY_PRODUCTION_MIN_INTERVAL_SECONDS=300
SIGENERGY_PRODUCTION_DAILY_BUDGET=
```

O orçamento diário permanece vazio até existir um limite contratual confirmado.
A UI mostra `Por validar`. Podem ser definidos limites por área:

```dotenv
SIGENERGY_API_MIN_INTERVAL_SECONDS=
SIGENERGY_API_DAILY_BUDGET=
SIGENERGY_DISCOVERY_MIN_INTERVAL_SECONDS=
SIGENERGY_DISCOVERY_DAILY_BUDGET=
SIGENERGY_STATE_MIN_INTERVAL_SECONDS=
SIGENERGY_STATE_DAILY_BUDGET=
SIGENERGY_PRODUCTION_MIN_INTERVAL_SECONDS=300
SIGENERGY_PRODUCTION_DAILY_BUDGET=
SIGENERGY_TELEMETRY_MIN_INTERVAL_SECONDS=
SIGENERGY_TELEMETRY_DAILY_BUDGET=
SIGENERGY_ALARMS_MIN_INTERVAL_SECONDS=
SIGENERGY_ALARMS_DAILY_BUDGET=
```

HTTP 429 mantém a área correta, respeita `Retry-After`, aplica cooldown
persistente e reagenda jobs. HTTP 403 de histórico é isolado à instalação e não
é repetido agressivamente.

## Estado validado da Expertcom

Com as credenciais locais atuais, a descoberta devolve `Expertcom` com System ID
`TZXRS1780315946`, e `energyFlow` devolve estado live. O histórico diário para
essa instalação devolveu HTTP 403. Consequentemente:

- a monitorização live está operacional;
- não existem kWh históricos Sigenergy ingeridos;
- a Sigenergy não está pronta para ser fonte primária dos relatórios;
- FusionSolar deve continuar como fonte primária caso esteja mapeada no mesmo
  asset.

## Passos manuais ainda necessários

No portal e com o contacto Sigenergy responsável pela aplicação:

1. Abrir a aplicação associada à App Key usada pela plataforma.
2. Pedir/ativar a permissão do endpoint **System history** para a Expertcom
   (`TZXRS1780315946`) e confirmar que uma leitura diária deixa de devolver
   HTTP 403.
3. Obter confirmação escrita de que os totais do endpoint de histórico diário
   são expressos em **kWh**. Só depois definir
   `SIGENERGY_HISTORY_ENERGY_UNIT=kWh` e reiniciar a aplicação.
4. Definir `SIGENERGY_PRODUCTION_DAILY_BUDGET` de acordo com a quota contratada
   antes de lançar backfills extensos.
5. Para Data Subscription, pedir à Sigenergy os valores específicos da
   aplicação: broker, porta, client ID, username, password, TLS, QoS e os três
   tópicos (telemetria periódica, alterações e alarmes). Sem estes dados não é
   possível ligar um consumidor MQTT real e seguro.

Não há URL de webhook para configurar: a documentação oficial disponível define
MQTT. Um consumidor MQTT só deve ser implementado quando os parâmetros reais e
payloads anonimizados da aplicação estiverem disponíveis.
