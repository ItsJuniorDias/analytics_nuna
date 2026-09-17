# Nuna Analytics

Backend de analytics interno do app **Nuna: Preschool Story Books** (iOS, categoria Kids, 3 a 6 anos). Recebe eventos anônimos em lote, valida cada um contra o catálogo `catalog/events.json`, grava em SQLite e oferece estatísticas agregadas e um painel simples.

- Python 3.9+ (local) e 3.12 (Docker), FastAPI, pydantic v2, SQLite (`sqlite3` da biblioteca padrão).
- Sem SDK de terceiros, sem identificador persistente, sem texto livre.
- Plano de eventos: [`EVENTS.md`](EVENTS.md). Fonte da verdade: [`catalog/events.json`](catalog/events.json) (versão 1, 38 eventos).

## Sumário

1. [Desenho de privacidade](#desenho-de-privacidade)
2. [Estrutura](#estrutura)
3. [Contrato da API](#contrato-da-api)
4. [Rodar localmente](#rodar-localmente)
5. [Docker](#docker)
6. [Deploy no Render com disco](#deploy-no-render-com-disco)
7. [Retenção e limpeza](#retenção-e-limpeza)
8. [Como o app iOS envia eventos](#como-o-app-ios-envia-eventos)
9. [App Store Connect: App Privacy](#app-store-connect-app-privacy)
10. [Política de privacidade e compromissos](#política-de-privacidade-e-compromissos)
11. [Justificativa COPPA](#justificativa-coppa)
12. [Evoluir o catálogo](#evoluir-o-catálogo)

## Desenho de privacidade

O público é criança. A Apple (diretriz 1.3, categoria Kids, e 5.1.2) proíbe analytics de terceiros e envio de informação pessoal ou do aparelho; o COPPA (16 CFR Part 312) trata identificador persistente, inclusive IP, como informação pessoal. Por isso o desenho parte de **não ter o que proteger**:

**Sem identificadores.** Nada de IDFA, IDFV, id de instalação, de conta ou de transação da StoreKit, token de push, nome ou modelo do aparelho. O único id é o `session_id`: UUID aleatório criado a cada lançamento, só em memória, trocado depois de 30 minutos em segundo plano. Ele não reconhece a mesma criança amanhã, então não é identificador persistente. O `event_id` é outro UUID aleatório, por evento, usado só para descartar reenvios.

**Sem texto livre.** Toda string do catálogo é enum fechado ou tem padrão estrito (`book_id` é o slug público do livro, `^[a-z0-9-]{1,64}$`). A busca manda só uma faixa de tamanho, erros mandam só uma categoria. O carregador do catálogo recusa subir se alguém adicionar uma string sem enum nem padrão, ou um inteiro sem limites.

**Validação estrita.** Evento com nome desconhecido, propriedade fora do catálogo, enum inválido, tipo errado, `null`, padrão que não bate ou inteiro fora da faixa é rejeitado com motivo, nunca aceito "em parte". Assim um bug no app não consegue contrabandear texto para o banco. O motivo devolvido nunca ecoa o valor recebido.

**IP e User-Agent nunca são lidos, gravados nem logados.**
- `app/privacy.py` é o middleware mais externo: apaga `scope["client"]` e remove `User-Agent`, `X-Forwarded-For`, `X-Real-IP`, `Forwarded`, `CF-Connecting-IP`, `True-Client-IP`, `Cookie`, `Referer` e similares antes de qualquer rota ou handler de erro.
- O uvicorn roda sempre com `--no-access-log` (o access log grava o IP) e `--no-proxy-headers` (não confia em `X-Forwarded-For`). Isso está no `Dockerfile`, no `Makefile` e em `python -m app`.
- Não existe coluna de IP, User-Agent, aparelho ou usuário. `tests/test_privacy.py` falha se aparecer uma, se o código passar a ler `request.client` ou se algum comando de execução perder essas opções.

**Sem ligar sessões entre si.** Um upload pode trazer várias sessões do mesmo aparelho (a fila acumula dias). Para não deixar essa ligação gravada:
- `received_at` guarda só o **dia** UTC da chegada, não o horário.
- A tabela `events` é `WITHOUT ROWID` com chave no `event_id` aleatório: não há rowid sequencial que revele quais linhas chegaram juntas.
- As rotas de estatística só devolvem contagens; nenhuma devolve `session_id`, `event_id` ou linha individual, e `by=session_id` é recusado.
- Funis são calculados dentro de uma sessão, nunca entre sessões.

**Retenção.** Eventos individuais são apagados após 180 dias (automático, dentro do serviço). Antes de apagar, os totais por dia e nome vão para `daily_event_counts`, que não tem `session_id`. A exclusão usa `PRAGMA secure_delete` e trunca o WAL.

**Opt-out.** Parents > "Share anonymous usage data" (ligado por padrão). Desligado: nada é enfileirado nem enviado e a fila local é apagada. O próprio toggle não gera evento.

## Estrutura

```
analytics_nuna/
├── catalog/events.json        catálogo v1 (fonte da verdade)
├── EVENTS.md                  plano de eventos (espelha o JSON)
├── app/
│   ├── main.py                create_app() e app do módulo
│   ├── __main__.py            python -m app (uvicorn com opções de privacidade)
│   ├── config.py              variáveis NUNA_*
│   ├── db.py                  SQLite: WAL, busy_timeout, migrações, índices
│   ├── catalog.py             carga do catálogo e validação estrita
│   ├── schemas.py             modelos pydantic de requisição e resposta
│   ├── auth.py                X-Nuna-Key e Bearer de admin
│   ├── privacy.py             middleware que apaga IP/User-Agent; cabeçalhos de segurança
│   ├── stats.py               consultas agregadas (SQL parametrizado)
│   ├── purge.py               retenção: CLI e limpeza agendada
│   ├── routes/                ingest.py, stats.py, dashboard.py
│   └── static/                dashboard.html, dashboard.css, dashboard.js
├── examples/batch.json        lote de exemplo (validado nos testes)
├── tests/                     pytest + TestClient
├── Dockerfile, docker-compose.yml, render.yaml, Makefile
├── .env.example
└── requirements.txt
```

## Contrato da API

Base: `https://<seu-domínio>`. Todas as respostas são JSON; erros têm a forma `{"detail": "<codigo>"}`.

### `POST /v1/events`

Cabeçalhos:

| Cabeçalho | Valor |
|---|---|
| `Content-Type` | `application/json` |
| `X-Nuna-Key` | uma das chaves de `NUNA_APP_KEYS` |

Corpo: o formato que o app manda (`CorpoDoEnvio` em `Nuna/Services/Analytics.swift`), com até **100** eventos (`NUNA_MAX_BATCH`) e no máximo **256 KB** (`NUNA_MAX_BODY_BYTES`).

- `context` vale para o lote inteiro: `session_id`, `app_version`, `build`, `os_version`, `device_family`. O app só junta no mesmo lote eventos com o mesmo contexto (a fila atravessa sessões e atualizações do app).
- Cada evento leva o que muda dentro da sessão: `event_id`, `timestamp`, `layout` (omitido fora do leitor) e `subscription_state`.
- `catalog_version` é opcional; ausente vale `1`. O app atual não manda.

```json
{
  "context": {
    "session_id": "5d2c8f7a-1b3e-4c6d-8e9f-0a1b2c3d4e5f",
    "app_version": "1.0",
    "build": "12",
    "os_version": "27.0",
    "device_family": "phone"
  },
  "events": [
    {
      "event_id": "c9d8e7f6-a5b4-4c3d-b2e1-f0a9b8c7d604",
      "name": "book_opened",
      "timestamp": "2026-09-17T19:02:20.117Z",
      "properties": {
        "book_id": "o-quintal-da-nuna",
        "book_version": 3,
        "source": "week_card",
        "access_reason": "free_book",
        "resume_state": "start",
        "start_spread": 1,
        "spread_count": 12,
        "orientation": "portrait",
        "two_halves": false
      },
      "layout": "single",
      "subscription_state": "free"
    }
  ]
}
```

Regras:
- Contexto do lote: exatamente `session_id`, `app_version`, `build`, `os_version`, `device_family`. Erro nele recusa todos os eventos do lote com o mesmo motivo (`"invalid_enum: context.device_family"`); `context` ausente ou que não é objeto dá `422 invalid_envelope`.
- Evento: `event_id`, `name`, `timestamp`, `properties`, `subscription_state` obrigatórios; `layout` opcional. Qualquer outro campo (inclusive `context` ou `session_id` dentro do evento) é recusado. Os motivos desses campos vêm sem prefixo (`"invalid_enum: layout"`).
- Os valores seguem `catalog.context` (contexto e campos de evento) e `catalog.events[name].properties`.
- Opcional ausente = não se aplica. **Nunca** mandar `null` nem string vazia.
- `event_id` e `session_id`: UUID v4 **minúsculo**.
- `timestamp`: UTC com `Z`, 0 a 3 casas de fração (`2026-09-17T19:02:20Z` ou `...20.117Z`). Aceito entre 30 dias atrás (`NUNA_MAX_EVENT_AGE_DAYS`) e 24 h à frente (`NUNA_MAX_CLOCK_SKEW_HOURS`) do relógio do servidor.
- Inteiros são inteiros JSON (`3`, não `3.0` nem `"3"`); booleanos são `true`/`false` (não `1`).
- JSON com `NaN`/`Infinity` ou chave repetida é recusado inteiro.

Resposta `200`:

```json
{"accepted": 4, "duplicates": 1, "rejected": [{"index": 2, "reason": "invalid_enum: properties.source"}]}
```

- `accepted`: eventos novos gravados.
- `duplicates`: `event_id` já gravado antes (ou repetido no mesmo lote). Idempotente: o primeiro gravado vale.
- `rejected`: índice (base 0) e motivo `"codigo: caminho"`. Os demais eventos do lote são gravados normalmente.
- Sempre: `accepted + duplicates + len(rejected) == len(events)`.

Motivos de rejeição:

| Código | Quando |
|---|---|
| `event_not_object` | item do array não é objeto |
| `unknown_field` | campo do evento além de `event_id`, `name`, `timestamp`, `properties`, `layout`, `subscription_state` |
| `missing_field` | falta `name` ou `properties` |
| `unknown_event` | `name` fora do catálogo |
| `unknown_property` | propriedade (ou campo do contexto do lote) fora do catálogo |
| `missing_required` | propriedade ou campo obrigatório ausente (`event_id`, `timestamp`, `subscription_state`, campos do contexto) |
| `null_value` | valor `null` |
| `invalid_type` | tipo errado (string, integer, boolean) |
| `invalid_enum` | valor fora do enum |
| `pattern_mismatch` | string não bate com o padrão |
| `out_of_range` | inteiro fora de `minimum`/`maximum` |
| `invalid_timestamp` | formato certo, data impossível (ex. 30 de fevereiro) |
| `timestamp_out_of_window` | antigo ou adiantado demais |

Status do lote inteiro e o que o app deve fazer:

| Status | `detail` | O que fazer com os eventos enviados |
|---|---|---|
| `200` | — | tirar **todos** da fila (aceitos, duplicados e rejeitados; rejeitado não melhora com reenvio) |
| `400` | `invalid_json`, `invalid_content_length` | tirar da fila (bug do cliente) |
| `401` | `invalid_app_key` | manter e tentar mais tarde (chave em rotação ou configuração) |
| `413` | `batch_too_large`, `body_too_large` | o app descarta (qualquer 4xx fora 401/403/408/429). Não acontece na prática: o app manda no máximo 100 eventos, e o maior lote possível fica bem abaixo de 256 KB |
| `415` | `unsupported_media_type` | corrigir o cabeçalho; tirar da fila |
| `422` | `invalid_envelope`, `unsupported_catalog_version` | tirar da fila |
| `5xx`, rede | — | manter, backoff exponencial |

Exemplo com curl (timestamps precisam ser recentes):

```bash
NOW=$(date -u +%Y-%m-%dT%H:%M:%S.000Z)
curl -sS -X POST http://127.0.0.1:8000/v1/events \
  -H "Content-Type: application/json" \
  -H "X-Nuna-Key: $NUNA_APP_KEY" \
  --data-binary @- <<EOF
{"context": {"session_id": "$(uuidgen | tr A-Z a-z)", "app_version": "1.0", "build": "12",
             "os_version": "27.0", "device_family": "phone"},
 "events": [
  {"event_id": "$(uuidgen | tr A-Z a-z)", "name": "app_opened", "timestamp": "$NOW",
   "properties": {"onboarding_completed": false}, "subscription_state": "unknown"}
]}
EOF
```

### `GET /health`

Sem autenticação. `{"status": "ok", "catalog_version": 1}`, ou `503` se o banco não abrir.

### `GET /v1/catalog`

Sem autenticação. Devolve o `events.json` carregado (o mesmo contrato que já vai dentro do app).

### Estatísticas (`/v1/stats/*`)

Todas exigem `Authorization: Bearer $NUNA_ADMIN_TOKEN` (sem token configurado, respondem `401`). Datas são dias UTC `YYYY-MM-DD`, inclusivos; padrão: últimos 30 dias; máximo 366 dias. Parâmetro inválido = `422` com `detail` em texto. Respostas com `Cache-Control: no-store`.

| Rota | Parâmetros | Devolve |
|---|---|---|
| `/v1/stats/overview` | `from`, `to` | `totals` (eventos, sessões distintas), `days` (eventos e sessões por dia, dias vazios com zero), `top_events` |
| `/v1/stats/events` | `name` (obrigatório), `group=day\|total`, `by`, `from`, `to` | contagem do evento por dia ou no período; `by` quebra por uma propriedade do evento ou por `app_version`, `build`, `os_version`, `device_family`, `layout`, `subscription_state` |
| `/v1/stats/funnel` | `steps`, `from`, `to` | sessões que fizeram os passos **nesta ordem** (não precisam ser seguidos), com conversão do passo anterior e do primeiro |
| `/v1/stats/books` | `from`, `to` | por `book_id`: aberturas, sessões, conclusões e taxa de conclusão |
| `/v1/stats/paywall` | `from`, `to` | visualizações por origem, toques em assinar por plano, portão dos pais (mostrado, passou, errou, cancelou, taxa) por propósito, compras por plano e resultado, fechamentos por motivo, restaurações por resultado |

`steps` do funil: 2 a 10 passos separados por vírgula, cada um `evento` ou `evento:prop=valor[:prop=valor]`. Eventos no mesmo milissegundo contam na ordem do funil; um passo repetido exige dois eventos.

```bash
TOKEN=$NUNA_ADMIN_TOKEN
curl -sS -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8000/v1/stats/overview?from=2026-09-01&to=2026-09-17"
curl -sS -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8000/v1/stats/events?name=book_opened&group=total&by=source"
curl -sS -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8000/v1/stats/funnel?steps=paywall_viewed:source=locked_book,subscribe_tapped,parental_gate_passed:purpose=subscribe,purchase_completed"
curl -sS -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8000/v1/stats/books?from=2026-09-01"
curl -sS -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8000/v1/stats/paywall"
```

### Painel: `GET /dashboard`

HTML, CSS e JS próprios, sem CDN (a CSP proíbe qualquer origem externa). A página não tem dados: pede o token de admin, guarda em `sessionStorage` (some quando a aba fecha) e chama as rotas acima. Mostra visão geral com gráficos de barras em SVG, funis prontos (Ativação, Leitura, Monetização, Livro bloqueado), livros, paywall e um explorador por evento. Todo gráfico tem tabela equivalente.

## Rodar localmente

```bash
cd analytics_nuna
cp .env.example .env        # preencha NUNA_APP_KEYS e NUNA_ADMIN_TOKEN
make setup                  # cria .venv e instala requirements.txt
make test                   # pytest
make run                    # http://127.0.0.1:8000 (painel em /dashboard)
```

Sem `make`:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
set -a; . ./.env; set +a
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log --no-proxy-headers --no-server-header
# ou: .venv/bin/python -m app   (mesmas opções; HOST e PORT por variável)
```

Variáveis (todas em `.env.example`):

| Variável | Padrão | Uso |
|---|---|---|
| `NUNA_APP_KEYS` | vazio (tudo 401) | chaves de `X-Nuna-Key`, separadas por vírgula |
| `NUNA_ADMIN_TOKEN` | vazio (stats 401) | Bearer das estatísticas; mínimo 16 caracteres |
| `NUNA_DB_PATH` | `./data/analytics.db` | arquivo SQLite |
| `NUNA_RETENTION_DAYS` | `180` | retenção de eventos individuais |
| `NUNA_MAX_BATCH` | `100` | eventos por requisição |
| `NUNA_MAX_BODY_BYTES` | `262144` | tamanho do corpo |
| `NUNA_MAX_EVENT_AGE_DAYS` | `30` | timestamp mais antigo aceito |
| `NUNA_MAX_CLOCK_SKEW_HOURS` | `24` | timestamp mais adiantado aceito |
| `NUNA_PURGE_INTERVAL_HOURS` | `24` | limpeza automática (0 desliga) |
| `NUNA_CORS_ORIGINS` | vazio | só se o painel ficar em outro domínio |
| `NUNA_ENABLE_DOCS` | `false` | liga `/docs` e `/openapi.json` |

A chave do app vai dentro do binário, então **não é segredo**: ela barra tráfego aleatório e permite rotação (publique a versão do app com a chave nova, mantenha as duas em `NUNA_APP_KEYS` e remova a antiga quando ela sumir). O token de admin, esse sim, é segredo.

## Docker

```bash
cp .env.example .env
docker compose up -d --build
curl http://127.0.0.1:8000/health
docker compose exec analytics python -m app.purge --days 180 --dry-run
```

Imagem `python:3.12-slim`, usuário sem privilégios (`nuna`, uid 10001), banco em `/data/analytics.db` no volume nomeado `nuna_analytics_data`, um worker, `--no-access-log --no-proxy-headers`. A porta fica só em `127.0.0.1`; para expor, use um proxy HTTPS **com log de acesso sem IP**.

## Deploy no Render com disco

SQLite precisa de **disco persistente**. Sem ele, o sistema de arquivos do Render é efêmero e o banco some a cada deploy.

1. Suba este repositório no GitHub.
2. Render > **New** > **Blueprint** > escolha o repositório. O `render.yaml` cria o Web Service `nuna-analytics` (runtime Docker, plano `starter`, 1 instância, health check em `/health`) com disco `nuna-analytics-data` de 1 GB montado em `/data`.
3. Preencha `NUNA_APP_KEYS` quando o Render pedir. `NUNA_ADMIN_TOKEN` é gerado automaticamente; copie em Environment.
4. Depois do deploy: `curl https://<serviço>.onrender.com/health` e abra `/dashboard`.
5. Aponte um domínio próprio (ex. `analytics.<seu-domínio>`) e use essa URL no app.

Consequências do disco no Render: exige plano pago; só 1 instância; sem zero-downtime deploy (alguns segundos fora do ar por deploy, sem perda, porque o app mantém a fila e reenvia). Medido localmente, cada evento ocupa cerca de 700 bytes com índices: 1 GB guarda perto de 1,5 milhão de eventos, ou uns 8 mil por dia com 180 dias de retenção. Aumente `sizeGB` se o volume passar disso (o disco cresce, não diminui).

Se aparecer `unable to open database file`, o usuário `nuna` não consegue escrever no disco montado: confira `mountPath: /data` e `NUNA_DB_PATH=/data/analytics.db`.

Antes de publicar a política, confirme no painel do Render que não há log de requisições HTTP guardando IP do cliente (e desligue em qualquer CDN ou proxy à frente), e que o disco e os snapshots são criptografados em repouso. Os snapshots do disco guardam eventos por alguns dias além da exclusão; leve isso em conta na redação da retenção.

## Retenção e limpeza

- **Automática:** o serviço roda a limpeza ao subir e a cada `NUNA_PURGE_INTERVAL_HOURS` (padrão 24 h), com `NUNA_RETENTION_DAYS` (padrão 180). É o caminho no Render, onde um Cron Job separado não enxerga o disco do Web Service.
- **Manual:**

  ```bash
  python -m app.purge --days 180 --dry-run   # só conta
  python -m app.purge --days 180             # apaga
  make purge DAYS=180
  ```

- **Agendada fora do serviço** (servidor próprio, com `NUNA_PURGE_INTERVAL_HOURS=0`):

  ```cron
  # crontab: todo dia às 03:17 UTC
  17 3 * * * cd /srv/analytics_nuna && docker compose exec -T analytics python -m app.purge --days 180 >> /var/log/nuna-purge.log 2>&1
  ```

Um evento sai quando o timestamp do cliente **ou** o dia de chegada é anterior ao corte, então relógio errado no aparelho não prolonga a retenção. Antes de apagar, os totais vão para `daily_event_counts(day, name, events)`, sem `session_id`.

## Como o app iOS envia eventos

Resumo do cliente que o servidor espera (a implementação fica no app, `Analytics`):

**Fila local**
- Arquivo JSON em Application Support, marcado com `isExcludedFromBackup`. Cada item já é o evento completo (`name`, `context`, `properties`).
- Descartar itens com mais de **7 dias** (pelo `timestamp` dentro do arquivo) e limitar o tamanho (ex. 1.000 eventos, descartando os mais antigos).
- Com "Share anonymous usage data" desligado: não enfileirar nada, cancelar envio em curso e **apagar o arquivo**.

**Envio**
- Lotes de até 100 eventos, na ordem da fila, por `URLSession(configuration: .ephemeral)` (sem cookies nem cache), só HTTPS, para o domínio próprio.
- Quando: em `app_backgrounded` (dentro de uma background task curta), no lançamento (sobras da execução anterior) e quando a fila passar de ~50 eventos.
- Um envio por vez. Tratar a resposta pela tabela de status acima; em falha de rede ou `5xx`, backoff exponencial (30 s, 1 min, 2 min... até 1 h).
- O `User-Agent` padrão do URLSession (nome do app, CFNetwork, Darwin) é descartado pelo servidor; se quiser nem mandar, defina `User-Agent: Nuna`.

**Formato dos campos**

```swift
// UUID do Foundation vem em MAIÚSCULAS; o catálogo exige minúsculas.
let eventID = UUID().uuidString.lowercased()

// UTC com milissegundos e "Z" (ISO8601DateFormatter usa GMT por padrão).
let formatador = ISO8601DateFormatter()
formatador.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
let timestamp = formatador.string(from: Date())   // "2026-09-17T19:02:20.117Z"

var request = URLRequest(url: URL(string: "https://analytics.example.com/v1/events")!)
request.httpMethod = "POST"
request.setValue("application/json", forHTTPHeaderField: "Content-Type")
request.setValue(chaveDoApp, forHTTPHeaderField: "X-Nuna-Key")
```

- Propriedades opcionais como `Optional` no `Encodable` sintetizado: `nil` é omitido (`encodeIfPresent`), que é o que o servidor exige.
- `os_version`: `ProcessInfo.processInfo.operatingSystemVersion` (`27.0` ou `27.0.1`); `device_family`: `phone`, `pad` ou `other` a partir de `userInterfaceIdiom`, nunca modelo.
- Contadores limitados ao `maximum` do catálogo antes de enfileirar; durações sempre em faixas.
- `catalog_version` não é enviado (vale 1) até existir um catálogo novo.

## App Store Connect: App Privacy

**"Do you or your third-party partners collect data from this app?"** Yes.

Declarar os tipos abaixo. Em **todos**: *Linked to the user's identity* = **No**; *Used for tracking* = **No**.

| Tipo (App Store Connect) | Finalidade | Eventos |
|---|---|---|
| Usage Data > **Product Interaction** | Analytics | `app_opened/foregrounded/backgrounded`, `onboarding_*`, `screen_viewed`, `home_section_viewed`, `library_filter/sort_changed`, `book_opened`, `page_turned`, `book_completed`, `reader_*`, `paywall_viewed/closed`, `plan_selected`, `subscribe_tapped`, `parental_gate_*`, `restore_tapped`, `reading_progress_reset` |
| Purchases > **Purchase History** | Analytics | `purchase_completed/pending/cancelled/failed`, `restore_finished`, `subscription_status_changed` (tipo de plano e resultado; não é ligado a ninguém, mas é informação sobre compras, então declare) |
| **Search History** | Analytics | `library_search_performed` (que houve busca, faixa de tamanho e número de resultados, nunca o texto). Se não quiser esse rótulo num app Kids, remova esse evento do catálogo antes do lançamento; nada depende dele |
| Diagnostics > **Performance Data** | App Functionality, Analytics | `catalog_loaded.load_duration_bucket`, `store_products_loaded.duration_bucket` |
| Diagnostics > **Other Diagnostic Data** | App Functionality, Analytics | `store_products_load_failed`, origem do catálogo e contagem de assets faltando em `catalog_loaded`, `page_art_fallback_shown`, `book_opened.resume_state = reset_invalid` |

**Não declarar:** Contact Info, Health & Fitness, Financial Info, Location, Sensitive Info, Contacts, User Content, Browsing History, Identifiers (User ID ou Device ID: `session_id` é aleatório por lançamento e não persiste, então não é nenhum dos dois), Crash Data (não há crash reporter), Advertising Data, Other Usage Data, Other Data.

### Privacy manifest (`Nuna/PrivacyInfo.xcprivacy`)

O target ainda não tem esse arquivo. Ele deve espelhar as respostas acima. `NSPrivacyAccessedAPICategoryUserDefaults` (motivo `CA92.1`) é necessário mesmo sem analytics, porque o app já usa UserDefaults (`ReadingProgress`, `@AppStorage`). Medir durações com `Date` evita declarar motivo de boot time, e guardar os timestamps dentro do arquivo da fila evita motivo de file timestamp.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>NSPrivacyTracking</key>
  <false/>
  <key>NSPrivacyTrackingDomains</key>
  <array/>
  <key>NSPrivacyCollectedDataTypes</key>
  <array>
    <dict>
      <key>NSPrivacyCollectedDataType</key>
      <string>NSPrivacyCollectedDataTypeProductInteraction</string>
      <key>NSPrivacyCollectedDataTypeLinked</key>
      <false/>
      <key>NSPrivacyCollectedDataTypeTracking</key>
      <false/>
      <key>NSPrivacyCollectedDataTypePurposes</key>
      <array>
        <string>NSPrivacyCollectedDataTypePurposeAnalytics</string>
      </array>
    </dict>
    <dict>
      <key>NSPrivacyCollectedDataType</key>
      <string>NSPrivacyCollectedDataTypePurchaseHistory</string>
      <key>NSPrivacyCollectedDataTypeLinked</key>
      <false/>
      <key>NSPrivacyCollectedDataTypeTracking</key>
      <false/>
      <key>NSPrivacyCollectedDataTypePurposes</key>
      <array>
        <string>NSPrivacyCollectedDataTypePurposeAnalytics</string>
      </array>
    </dict>
    <dict>
      <key>NSPrivacyCollectedDataType</key>
      <string>NSPrivacyCollectedDataTypeSearchHistory</string>
      <key>NSPrivacyCollectedDataTypeLinked</key>
      <false/>
      <key>NSPrivacyCollectedDataTypeTracking</key>
      <false/>
      <key>NSPrivacyCollectedDataTypePurposes</key>
      <array>
        <string>NSPrivacyCollectedDataTypePurposeAnalytics</string>
      </array>
    </dict>
    <dict>
      <key>NSPrivacyCollectedDataType</key>
      <string>NSPrivacyCollectedDataTypePerformanceData</string>
      <key>NSPrivacyCollectedDataTypeLinked</key>
      <false/>
      <key>NSPrivacyCollectedDataTypeTracking</key>
      <false/>
      <key>NSPrivacyCollectedDataTypePurposes</key>
      <array>
        <string>NSPrivacyCollectedDataTypePurposeAppFunctionality</string>
        <string>NSPrivacyCollectedDataTypePurposeAnalytics</string>
      </array>
    </dict>
    <dict>
      <key>NSPrivacyCollectedDataType</key>
      <string>NSPrivacyCollectedDataTypeOtherDiagnosticData</string>
      <key>NSPrivacyCollectedDataTypeLinked</key>
      <false/>
      <key>NSPrivacyCollectedDataTypeTracking</key>
      <false/>
      <key>NSPrivacyCollectedDataTypePurposes</key>
      <array>
        <string>NSPrivacyCollectedDataTypePurposeAppFunctionality</string>
        <string>NSPrivacyCollectedDataTypePurposeAnalytics</string>
      </array>
    </dict>
  </array>
  <key>NSPrivacyAccessedAPITypes</key>
  <array>
    <dict>
      <key>NSPrivacyAccessedAPIType</key>
      <string>NSPrivacyAccessedAPICategoryUserDefaults</string>
      <key>NSPrivacyAccessedAPITypeReasons</key>
      <array>
        <string>CA92.1</string>
      </array>
    </dict>
  </array>
</dict>
</plist>
```

## Política de privacidade e compromissos

Seção sugerida para a política (em inglês, voltada aos pais; peça revisão jurídica da redação final):

> **Anonymous usage data**
>
> Nuna can send anonymous information about how the app is used, so we can see which features work, fix problems, and understand in aggregate how many families subscribe. This is on by default and a parent can turn it off at any time in the app under Parents > Share anonymous usage data. When it is turned off, Nuna stops sending this information right away and deletes any that has not been sent yet.
>
> What we collect: which screens are shown; which book (by its catalog name) is opened, how many pages are turned, whether it is finished, and roughly how long it stays open; onboarding steps; steps on the subscription screen and the outcome of a purchase or restore (plan type and success, pending, cancelled or a general error type); when a grown-ups-only check is passed, failed or closed; technical signals such as loading times, store connection errors and missing artwork; the kind of device (phone or tablet), the iOS version and the app version.
>
> What we never collect: names, email addresses, accounts, photos, voice, location, advertising identifiers or any device identifier, what anyone types (including searches), the answer to the grown-ups-only question, or anything that identifies a child or family.
>
> Each time the app opens it creates a random session code that is kept only in memory and replaced after 30 minutes in the background. It cannot be used to recognize the same child or device later.
>
> How we use it: only to operate and improve Nuna, internally and in aggregate. We do not use it for advertising, we do not build profiles of any child, we never use it to contact anyone, and we do not sell or share it. We do not use third-party analytics or advertising services. Our hosting provider stores the data for us under contract and may not use it for anything else.
>
> IP addresses: like any internet request, your device's IP address reaches our server so the server can reply. We do not store or log it, and we do not use it to work out location.
>
> Retention: unsent data waits on the device for at most 7 days. Individual events are deleted from our servers after 180 days; after that we keep only aggregate totals that contain no session code.

Compromissos que esse texto cria e onde cada um é cumprido:

| Compromisso | Situação |
|---|---|
| Não guardar nem logar IP; sem geo-IP; sem User-Agent | Servidor: middleware + `--no-access-log --no-proxy-headers`, sem colunas, testes. **Fora do código:** conferir logs do Render e de qualquer CDN/proxy |
| Validar todo evento contra `events.json` (desconhecido, propriedade extra, enum, faixa) | `app/catalog.py`, `tests/test_validation.py` |
| Não juntar sessões, não exportar por sessão, não usar para anúncio ou personalização | `received_at` só com dia, tabela sem rowid, stats só agregadas, funil dentro da sessão. **Fora do código:** regra do time |
| Apagar eventos brutos com 180 dias; agregados sem `session_id` | Limpeza automática + `daily_event_counts` |
| Programa de segurança escrito (acesso restrito, criptografia em repouso) | **Fora do código:** token de admin só com quem precisa, rotação se alguém sair, confirmar criptografia do disco e dos snapshots no provedor |

Risco de revisão fora do analytics (só sinalizado): a diretriz 1.3 exige portão parental antes de links que saem de um app Kids. Os links de Terms of Use e Privacy abrem o Safari sem portão no rodapé do paywall (`PaywallView`, `linksLegais`) e em Parents > About Nuna (`ParentsView`, `sobre`), e a aba Parents não tem portão na entrada. O `ParentalGate` existente resolve.

## Justificativa COPPA

- Pelo COPPA (16 CFR Part 312), "informação pessoal" inclui identificador persistente capaz de reconhecer um usuário ao longo do tempo e entre serviços, como IP ou id de aparelho. Os eventos do Nuna não têm nome, contato, foto, áudio, geolocalização nem texto livre. O `session_id` é aleatório por lançamento, só em memória e trocado, então não reconhece uma criança ao longo do tempo e não é identificador persistente.
- O único identificador persistente que chega ao Nuna é o IP da conexão HTTPS. Ele é usado só para realizar a comunicação de rede, que está na definição de "support for the internal operations of the website or online service" do § 312.2, junto com manter ou analisar o funcionamento do serviço, garantir segurança e integridade e cumprir a lei. O IP não é guardado, logado, usado para contatar a criança, para publicidade comportamental nem para montar perfil. Isso se encaixa na exceção do § 312.5(c)(7): não é preciso consentimento parental verificável quando um identificador persistente é coletado só para operações internas e nenhuma outra informação pessoal é coletada.
- Os agregados (confiabilidade, funis de onboarding, leitura e assinatura) servem para "maintaining or analyzing the functioning" do app. Nunca são ligados a uma criança identificável, nunca servem para contato nem para anúncio.
- A regra COPPA alterada (regra final da FTC publicada em abril de 2025, cumprimento a partir de 22 de abril de 2026) pede, de quem usa essa exceção, que o aviso online diga para quais operações internas específicas o identificador é coletado e como se garante que não é usado para outra coisa, e que exista política de retenção escrita. A seção acima faz as duas coisas (finalidades, "nunca para contatar, perfilar ou anunciar", IP não guardado, retenção de 7 dias no aparelho e 180 no servidor). Peça a um advogado para confirmar a redação final.
- O toggle dos pais (ligado por padrão) não é mecanismo de consentimento do COPPA; a exceção não exige consentimento. Ele é controle parental adicional e minimização de dados.
- Se o Nuna for distribuído no Reino Unido ou na UE, confirme com advogado o analytics ligado por padrão para crianças (UK Children's Code, padrão 7, "high privacy by default"; GDPR art. 8 e legítimo interesse). O desenho anônimo ajuda, mas lá pode ser esperado desligado por padrão.

## Evoluir o catálogo

- `catalog/events.json` e `EVENTS.md` andam juntos; `tests/test_validation.py` confere que todo evento e propriedade do JSON aparece no documento.
- O servidor aceita só `catalog_version` igual à do arquivo carregado. Mudança só aditiva e opcional (nova propriedade `required: false`, novo valor de enum, evento novo) pode manter a versão, desde que o servidor seja publicado **antes** do app que usa. Remover ou apertar algo quebra apps antigos: crie a versão 2 e faça o servidor aceitar as duas versões durante a transição.
- Campo de contexto novo exige migração em `app/db.py` (o carregador recusa um contexto que não bate com as colunas).
- Cada tipo de dado novo pode mudar as respostas de App Privacy e o privacy manifest: revise as seções acima.
