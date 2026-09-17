# Nuna: plano de eventos de analytics (catálogo v1)

Fonte da verdade: `events.json` (versão 1). Este documento espelha o JSON; se divergirem, vale o JSON. Analytics interno, próprio (sem SDK de terceiros), sem identificador persistente, sem texto livre. Tudo o que é string é enum fechado, exceto `book_id` (slug do catálogo, `^[a-z0-9-]{1,64}$`) e os campos de sistema do contexto.

## Regras gerais

- Nomes em `snake_case`, formato objeto + verbo no passado.
- Nada é enfileirado nem enviado com "Share anonymous usage data" desligado em Parents; desligar apaga a fila local.
- Propriedade opcional ausente = não se aplica (nunca mandar `null` nem string vazia).
- Contadores inteiros são limitados no cliente ao `maximum` do catálogo.
- Durações são sempre faixas (buckets), medidas com `Date`, nunca valores exatos.
- O backend rejeita evento com nome desconhecido, propriedade fora do catálogo, enum inválido ou inteiro fora da faixa.
- Envelope do upload: `{"context": {session_id, app_version, build, os_version, device_family, storefront?}, "events": [{event_id, name, timestamp, properties, layout?, subscription_state}]}`. O contexto vale para o lote; `event_id`, `timestamp`, `layout` e `subscription_state` vão em cada evento.

## Contexto comum (do lote ou de cada evento, ver envelope acima)

| Propriedade | Tipo | Obrigatória | Valores | Origem |
|---|---|---|---|---|
| `event_id` | string | sim | `^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$` | UUID v4 minúsculo, novo a cada evento (dedup no backend) |
| `session_id` | string | sim | `^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$` | UUID v4 minúsculo, gerado no lançamento e trocado após ≥ 30 min em segundo plano; só em memória |
| `timestamp` | string | sim | `^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,3})?Z$` | hora do cliente em UTC, ISO 8601 com `Z` (sem fuso local) |
| `app_version` | string | sim | `^[0-9]{1,4}(\.[0-9]{1,4}){0,2}$` | `CFBundleShortVersionString` |
| `build` | string | sim | `^[0-9]{1,8}(\.[0-9]{1,4}){0,2}$` | `CFBundleVersion` |
| `os_version` | string | sim | `^[0-9]{1,3}(\.[0-9]{1,3}){0,2}$` | `ProcessInfo.processInfo.operatingSystemVersion` |
| `device_family` | string | sim | `phone` \| `pad` \| `other` | `userInterfaceIdiom`: phone (inclui o Duo), pad, other; nunca modelo ou nome |
| `storefront` | string | não | `^[A-Z]{3}$` | país da conta da App Store (`Storefront.countryCode`, ISO 3166-1 alfa-3, ex. `BRA`); a loja da conta Apple, não a localização do aparelho; omitido quando o StoreKit não informa |
| `layout` | string | não | `single` \| `spread` \| `unknown` | só enquanto um leitor está aberto: single/spread; `unknown` antes da primeira medida; ausente fora do leitor |
| `subscription_state` | string | sim | `premium` \| `free` \| `unknown` | `unknown` até a primeira `atualizarAssinatura` do processo terminar; depois premium/free |

## Faixas usadas

| Faixa | Valores |
|---|---|
| curta (onboarding, paywall, portão) | `lt_5s`, `5_15s`, `15_30s`, `30_60s`, `1_3m`, `gte_3m` |
| leitura | `lt_10s`, `10_30s`, `30_60s`, `1_2m`, `2_5m`, `5_10m`, `10_20m`, `gte_20m` |
| permanência na página | `lt_2s`, `2_5s`, `5_15s`, `15_60s`, `gte_60s` |
| segundo plano | `lt_1m`, `1_5m`, `5_30m`, `gte_30m` |
| primeiro plano | `lt_1m`, `1_5m`, `5_15m`, `15_30m`, `30_60m`, `gte_60m` |

## Funis principais

- **Ativação:** `app_opened` → `catalog_loaded` → `onboarding_started(first_run)` → `onboarding_page_viewed` → `onboarding_completed` ou `onboarding_skipped` → `paywall_viewed(post_onboarding)` → `paywall_closed` → `screen_viewed(home, initial)` → `book_opened` → `page_turned` → `book_completed`.
- **Leitura:** `book_opened` → `page_turned` (várias) → `book_completed` (se chegar ao fim) → `reader_closed` (ou `reader_backgrounded` se o app morrer em segundo plano).
- **Monetização:** `paywall_viewed` → `plan_selected` (opcional) → `subscribe_tapped` → `parental_gate_shown(subscribe)` → `parental_gate_passed` → `purchase_completed` \| `purchase_pending` \| `purchase_cancelled` \| `purchase_failed` → `paywall_closed`; aprovação posterior do Pedir Compra chega como `subscription_status_changed(premium, transaction_update)`.
- **Livro bloqueado:** `paywall_viewed(locked_book, book_id, book_source)` → `paywall_closed(reason)` → `book_opened` (o livro não abre sozinho depois da compra).
- **Confiabilidade:** `store_products_loaded` vs `store_products_load_failed` por `fetch_context`; `catalog_loaded.catalog_source`; `page_art_fallback_shown`; `book_opened.resume_state = reset_invalid`.

## App e sessão

### `app_opened`

| | |
|---|---|
| **Quando dispara** | Na primeira vez que a cena fica `.active` neste processo (não no prewarm). Uma vez por processo. |
| **Onde no app** | NunaApp.swift: `.onChange(of: fase, initial: true)` no nível da Scene → `Analytics.cenaAtiva()`; `Analytics.start()` no `init()` do `NunaApp` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `onboarding_completed` | boolean | sim | true \| false | `@AppStorage("onboardingConcluido")` no momento do evento |

### `app_foregrounded`

| | |
|---|---|
| **Quando dispara** | Transição `.background → .active` (não conta `.inactive ↔ .active` de folha da App Store, Face ID, Central de Controle). |
| **Onde no app** | NunaApp.swift: `.onChange(of: fase)` no nível da Scene → `Analytics.cenaAtiva()` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `background_duration_bucket` | string | sim | `lt_1m` \| `1_5m` \| `5_30m` \| `gte_30m` | tempo em segundo plano |
| `session_rotated` | boolean | sim | true \| false | true quando ficou ≥ 30 min em segundo plano e um session_id novo foi gerado |

### `app_backgrounded`

| | |
|---|---|
| **Quando dispara** | Transição para `.background` vinda de qualquer outra fase; uma vez por transição do app (não por janela). |
| **Onde no app** | NunaApp.swift: `.onChange(of: fase)` no nível da Scene → `Analytics.foiParaSegundoPlano()` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `foreground_duration_bucket` | string | sim | `lt_1m` \| `1_5m` \| `5_15m` \| `15_30m` \| `30_60m` \| `gte_60m` | tempo desde `app_opened` ou o último `app_foregrounded` |

## Confiabilidade

### `catalog_loaded`

| | |
|---|---|
| **Quando dispara** | Fim da primeira carga do catálogo e do onboarding, antes de sair do ProgressView. |
| **Onde no app** | ContentView.swift: `.task` do `Group`, depois de `carregando = false`, protegido por `guard carregando` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `catalog_source` | string | sim | `manifest` \| `bundle_fallback` | `bundle_fallback` quando `catalog.json` faltou ou não decodificou (`Catalog.usouReserva`) |
| `books_total` | integer | sim | 0…500 | `books.count` |
| `books_available` | integer | sim | 0…500 | livros com spreads (`isAvailable`) |
| `missing_cover_count` | integer | sim | 0…500 | livros disponíveis sem asset `cover_<id>` |
| `onboarding_content_loaded` | boolean | sim | true \| false | `onboarding != nil`; false = o app pula onboarding e paywall de boas-vindas |
| `load_duration_bucket` | string | sim | `lt_50ms` \| `50_200ms` \| `200_1000ms` \| `gte_1s` | tempo de `Catalog.loadAll` + `OnboardingContent.load` |

### `store_products_loaded`

| | |
|---|---|
| **Quando dispara** | Fim de `carregarProdutos` com os dois planos carregados. |
| **Onde no app** | Services/Store.swift: `carregarProdutos(contexto:)`, depois de `await atualizarElegibilidade()`, quando `erroProdutos == nil` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `fetch_context` | string | sim | `launch` \| `paywall_open` \| `paywall_retry` | quem pediu a busca |
| `duration_bucket` | string | sim | `lt_1s` \| `1_3s` \| `3_10s` \| `gte_10s` | tempo desde o início da busca |
| `trial_eligible` | boolean | sim | true \| false | `Store.trialEligible` depois da busca |

### `store_products_load_failed`

| | |
|---|---|
| **Quando dispara** | Fim de `carregarProdutos` sem um dos dois planos (`mensal` ou `anual` nil); se o paywall estiver aberto, aparece a tela de erro com Try again. |
| **Onde no app** | Services/Store.swift: `carregarProdutos(contexto:)`, entre `await atualizarElegibilidade()` e `carregandoProdutos = false`, quando `erroProdutos != nil` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `fetch_context` | string | sim | `launch` \| `paywall_open` \| `paywall_retry` | quem pediu a busca |
| `failure_kind` | string | sim | `request_error` \| `incomplete_response` | `request_error` se `Product.products` lançou erro |
| `returned_count` | integer | sim | 0…2 | planos que a loja tem em memória depois da busca |
| `error_category` | string | não | `network` \| `not_available_in_storefront` \| `purchase_not_allowed` \| `unverified` \| `storekit_other` \| `purchase_error_other` \| `unknown` | só com `request_error`; `Store.categoriaDoErro(_:)`, nunca `localizedDescription` |

### `page_art_fallback_shown`

| | |
|---|---|
| **Quando dispara** | Uma página pousa na tela (abertura, virada, troca de layout) e o asset dela não existe. |
| **Onde no app** | Views/ReaderView.swift: `verificarArte(_:)`, chamada de `registrarAbertura`, `registrarVirada` e da `.task(id: ladoALado)` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `book_id` | string | sim | `^[a-z0-9-]{1,64}$` | slug do catálogo |
| `spread_number` | integer | sim | 1…200 | `state.spreadIndex + 1` |
| `asset_variant` | string | sim | `spread` \| `left` \| `right` | `spread` lado a lado (`imageName`); `left`/`right` em página única |

## Onboarding

### `onboarding_started`

| | |
|---|---|
| **Quando dispara** | Primeiro `onAppear` de cada `OnboardingView` (não repete ao dobrar o Duo ou girar o iPad). |
| **Onde no app** | Views/Onboarding/OnboardingView.swift: `.onAppear` no fim do body, protegido por `@State registrouInicio` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `trigger` | string | sim | `first_run` \| `replay` | `replay` quando a intro já tinha sido concluída (ContentView passa `gatilho`) |
| `page_count` | integer | sim | 1…20 | `content.pages.count` |
| `missing_image_count` | integer | sim | 0…20 | páginas com `UIImage(named: p.image) == nil` |

### `onboarding_page_viewed`

| | |
|---|---|
| **Quando dispara** | Cada mudança real de `index` (swipe no TabView ou botão Next). |
| **Onde no app** | Views/Onboarding/OnboardingView.swift: `.onChange(of: index)` ao lado de `.animation(..., value: index)` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `page_index` | integer | sim | 0…19 | novo índice, base 0 |
| `page_count` | integer | sim | 1…20 | `content.pages.count` |
| `direction` | string | sim | `forward` \| `back` | `novo > antigo` → forward |
| `method` | string | sim | `swipe` \| `next_button` | `next_button` quando `@State avancouPeloBotao` estava ligado |

### `onboarding_completed`

| | |
|---|---|
| **Quando dispara** | Toque no CTA da última página; um evento por conclusão (duplo toque ignorado). |
| **Onde no app** | ContentView.swift: `concluirOnboarding(_:)` caso `.concluiu`, depois de `guard !onboardingDone` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `trigger` | string | sim | `first_run` \| `replay` | — |
| `page_count` | integer | sim | 1…20 | — |
| `will_show_paywall` | boolean | sim | true \| false | `!Store.shared.isPremium` agora (pode ser true antes da assinatura resolver) |
| `duration_bucket` | string | sim | `lt_5s` \| `5_15s` \| `15_30s` \| `30_60s` \| `1_3m` \| `gte_3m` | desde `onboarding_started` |

### `onboarding_skipped`

| | |
|---|---|
| **Quando dispara** | Toque em Skip (só existe nas páginas 0…n-2). |
| **Onde no app** | ContentView.swift: `concluirOnboarding(_:)` caso `.pulou(pagina:iniciadoEm:)`, depois de `guard !onboardingDone` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `trigger` | string | sim | `first_run` \| `replay` | — |
| `skipped_at_page_index` | integer | sim | 0…19 | `index` no toque |
| `page_count` | integer | sim | 1…20 | — |
| `will_show_paywall` | boolean | sim | true \| false | `!Store.shared.isPremium` |
| `duration_bucket` | string | sim | `lt_5s` \| `5_15s` \| `15_30s` \| `30_60s` \| `1_3m` \| `gte_3m` | desde `onboarding_started` |

## Navegação e Home

### `screen_viewed`

| | |
|---|---|
| **Quando dispara** | Primeira aparição do `RootView` (Home) e cada troca real de aba. |
| **Onde no app** | Views/RootView.swift: `.onAppear` no TabView com `@State registrouPrimeiraTela`; `.onChange(of: tab)` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `screen` | string | sim | `home` \| `library` \| `parents` | — |
| `previous_screen` | string | não | `home` \| `library` \| `parents` | ausente com `trigger = initial` |
| `trigger` | string | sim | `initial` \| `tab_tap` | — |

### `home_section_viewed`

| | |
|---|---|
| **Quando dispara** | Seção entra 50% na tela pela primeira vez na sessão. |
| **Onde no app** | Views/Home/HomeView.swift: `.onScrollVisibilityChange(threshold: 0.5)` na raiz de `secao(...)` e `rail(...)`; dedup em `Analytics.registrarUmaVezPorSessao` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `section` | string | sim | `continue_strip` \| `week_card` \| `just_arrived` \| `collection` \| `all_books` | — |
| `collection_id` | string | não | `fora-de-casa` \| `quintal` \| `antes-de-dormir` \| `juntos` | só com `section = collection` (`Featured.Collection.id`) |
| `two_halves` | boolean | sim | true \| false | `postura.duasMetades` (Duo aberto na horizontal, iPad deitado) |

## Biblioteca

### `library_search_performed`

| | |
|---|---|
| **Quando dispara** | Busca não vazia estável por 1 s e diferente da última registrada. |
| **Onde no app** | Views/Library/LibraryView.swift: `.task(id: busca)` depois de `.scrollEdgeEffectStyle` (:127) |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `query_length_bucket` | string | sim | `1_2` \| `3_5` \| `6_10` \| `gte_11` | `busca.count`; NUNCA o texto |
| `result_count` | integer | sim | 0…500 | `resultado.count`; 0 = estado "No books found" |
| `filter` | string | sim | `all` \| `available` \| `started` | — |
| `sort` | string | sim | `catalog` \| `title` | — |

### `library_filter_changed`

| | |
|---|---|
| **Quando dispara** | Mudança real de `filtro` (tocar o chip ativo não conta). |
| **Onde no app** | Views/Library/LibraryView.swift: `.onChange(of: filtro)` na cadeia do ScrollView |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `filter` | string | sim | `all` \| `available` \| `started` | todos→all, disponiveis→available, comecados→started |
| `previous_filter` | string | sim | `all` \| `available` \| `started` | — |
| `result_count` | integer | sim | 0…500 | `resultado.count` depois da troca; 0 = "Nothing here" |
| `sort` | string | sim | `catalog` \| `title` | — |
| `has_query` | boolean | sim | true \| false | `!busca.isEmpty` |

### `library_sort_changed`

| | |
|---|---|
| **Quando dispara** | Mudança real de `ordem` no Menu. |
| **Onde no app** | Views/Library/LibraryView.swift: `.onChange(of: ordem)` na cadeia do ScrollView |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `sort` | string | sim | `catalog` \| `title` | catalogo→catalog, titulo→title |
| `previous_sort` | string | sim | `catalog` \| `title` | — |
| `filter` | string | sim | `all` \| `available` \| `started` | — |
| `has_query` | boolean | sim | true \| false | — |

## Leitura

### `book_opened`

| | |
|---|---|
| **Quando dispara** | Primeira medida de geometria do `ReaderView` (o layout já é conhecido). Livro bloqueado não chega aqui: vira `paywall_viewed(source = locked_book)`. |
| **Onde no app** | Views/ReaderView.swift: `medir(_:)`, ramo da primeira medida → `registrarAbertura(tamanho:)`, protegido por `state.aberturaRegistrada` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `book_id` | string | sim | `^[a-z0-9-]{1,64}$` | slug do catálogo, nunca o título |
| `book_version` | integer | sim | 0…10000 | `book.version` |
| `source` | string | sim | `week_card` \| `continue_strip` \| `just_arrived` \| `collection` \| `all_books` \| `library_grid` | `OrigemDaLeitura.fonte` |
| `collection_id` | string | não | `fora-de-casa` \| `quintal` \| `antes-de-dormir` \| `juntos` | só com `source = collection` |
| `position` | integer | não | 0…499 | índice base 0 no trilho/grade; ausente em week_card e continue_strip |
| `library_filter` | string | não | `all` \| `available` \| `started` | só com `source = library_grid` |
| `has_query` | boolean | não | true \| false | só com `source = library_grid` |
| `access_reason` | string | sim | `premium` \| `free_book` \| `story_of_week` | ordem de `Store.podeLer`: premium, free_book, story_of_week |
| `resume_state` | string | sim | `start` \| `resumed` \| `resumed_at_end` \| `reset_invalid` | `reset_invalid` = progresso salvo ≥ spreads.count, recomeçou do 1 |
| `start_spread` | integer | sim | 1…200 | `state.spreadIndex + 1` na abertura |
| `spread_count` | integer | sim | 1…200 | `book.spreads.count` |
| `orientation` | string | sim | `portrait` \| `landscape` | largura > altura do tamanho medido (sem pixels) |
| `two_halves` | boolean | sim | true \| false | `Postura(tamanho:horizontal:).duasMetades` |

### `page_turned`

| | |
|---|---|
| **Quando dispara** | Sucesso de `advance(via:)` ou `goBack(via:)`; toque que só empurra na borda não conta. |
| **Onde no app** | Views/ReaderView.swift: `registrarVirada(_:via:)`, chamada no ramo de sucesso de `advance(via:)` e `goBack(via:)` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `book_id` | string | sim | `^[a-z0-9-]{1,64}$` | — |
| `direction` | string | sim | `forward` \| `back` | — |
| `input` | string | sim | `tap_zone` \| `button` \| `swipe` | zona de toque, seta da barra ou arrasto horizontal |
| `spread_number` | integer | sim | 1…200 | spread onde pousou |
| `page_side` | string | sim | `left` \| `right` \| `both` | `both` lado a lado; `left`/`right` em página única |
| `spread_count` | integer | sim | 1…200 | — |
| `dwell_bucket` | string | sim | `lt_2s` \| `2_5s` \| `5_15s` \| `15_60s` \| `gte_60s` | tempo ativo na página anterior (segundo plano não conta) |

### `book_completed`

| | |
|---|---|
| **Quando dispara** | Primeira vez que `isAtEnd` fica true depois de uma virada para frente, ou de uma troca de layout que revela o fim (com ao menos uma virada). Nunca na abertura. |
| **Onde no app** | Views/ReaderView.swift: `registrarVirada` (trigger page_turn) e `.task(id: ladoALado)` (trigger layout_change), protegido por `state.fimRegistrado` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `book_id` | string | sim | `^[a-z0-9-]{1,64}$` | — |
| `source` | string | sim | `week_card` \| `continue_strip` \| `just_arrived` \| `collection` \| `all_books` \| `library_grid` | — |
| `access_reason` | string | sim | `premium` \| `free_book` \| `story_of_week` | — |
| `trigger` | string | sim | `page_turn` \| `layout_change` | — |
| `read_from_start` | boolean | sim | true \| false | a leitura começou na primeira página |
| `pages_viewed` | integer | sim | 0…400 | posições de página distintas vistas (lado a lado conta as duas) |
| `back_turns` | integer | sim | 0…99 | `state.recuos`, limitado a 99 |
| `spread_count` | integer | sim | 1…200 | — |
| `duration_bucket` | string | sim | `lt_10s` \| `10_30s` \| `30_60s` \| `1_2m` \| `2_5m` \| `5_10m` \| `10_20m` \| `gte_20m` | tempo ativo desde a abertura |

### `reader_layout_changed`

| | |
|---|---|
| **Quando dispara** | `ladoALado` mudou e ficou estável por 1 s, em relação ao layout registrado na abertura. |
| **Onde no app** | Views/ReaderView.swift: `.task(id: ladoALado)` ao lado de `.animation(..., value: ladoALado)` (:171) |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `book_id` | string | sim | `^[a-z0-9-]{1,64}$` | — |
| `from_layout` | string | sim | `single` \| `spread` | — |
| `to_layout` | string | sim | `single` \| `spread` | — |
| `orientation` | string | sim | `portrait` \| `landscape` | — |
| `two_halves` | boolean | sim | true \| false | `Postura.duasMetades`; dobra vs. rotação não se distinguem no SDK 27.0 |
| `spread_number` | integer | sim | 1…200 | — |

### `reader_backgrounded`

| | |
|---|---|
| **Quando dispara** | Fase da cena do leitor vira `.background` com o livro aberto. |
| **Onde no app** | Views/ReaderView.swift: `.onChange(of: fase)` (`@Environment(\.scenePhase)`) ao lado do `.onChange(of: state.spreadIndex)` (:172) |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `book_id` | string | sim | `^[a-z0-9-]{1,64}$` | — |
| `source` | string | sim | `week_card` \| `continue_strip` \| `just_arrived` \| `collection` \| `all_books` \| `library_grid` | — |
| `spread_number` | integer | sim | 1…200 | — |
| `furthest_spread` | integer | sim | 1…200 | — |
| `spread_count` | integer | sim | 1…200 | — |
| `pages_viewed` | integer | sim | 0…400 | — |
| `completed` | boolean | sim | true \| false | `state.fimRegistrado` |
| `duration_bucket` | string | sim | `lt_10s` \| `10_30s` \| `30_60s` \| `1_2m` \| `2_5m` \| `5_10m` \| `10_20m` \| `gte_20m` | tempo ativo até agora |

### `reader_closed`

| | |
|---|---|
| **Quando dispara** | Toque no X ("Close book"); duplo toque conta uma vez. É a única saída do leitor (`interactiveDismissDisabled`). |
| **Onde no app** | Views/ReaderView.swift: novo `fechar()` usado pelo `iconButton("xmark", ...)` (:221), antes de `onClose()` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `book_id` | string | sim | `^[a-z0-9-]{1,64}$` | — |
| `source` | string | sim | `week_card` \| `continue_strip` \| `just_arrived` \| `collection` \| `all_books` \| `library_grid` | — |
| `access_reason` | string | sim | `premium` \| `free_book` \| `story_of_week` | — |
| `start_spread` | integer | sim | 1…200 | — |
| `end_spread` | integer | sim | 1…200 | `state.spreadIndex + 1` (= progresso salvo) |
| `furthest_spread` | integer | sim | 1…200 | — |
| `spread_count` | integer | sim | 1…200 | — |
| `pages_viewed` | integer | sim | 0…400 | — |
| `forward_turns` | integer | sim | 0…99 | `state.avancos`, limitado a 99 |
| `back_turns` | integer | sim | 0…99 | `state.recuos`, limitado a 99 |
| `edge_nudges` | integer | sim | 0…99 | toques/arrastos que bateram na primeira ou última página |
| `ignored_swipes` | integer | sim | 0…99 | arrastos verticais/diagonais ignorados (tentativa de puxar para fechar) |
| `layout_changes` | integer | sim | 0…99 | quantos `reader_layout_changed` nesta leitura |
| `completed` | boolean | sim | true \| false | `state.fimRegistrado` |
| `duration_bucket` | string | sim | `lt_10s` \| `10_30s` \| `30_60s` \| `1_2m` \| `2_5m` \| `5_10m` \| `10_20m` \| `gte_20m` | tempo ativo total, sem segundo plano |

## Monetização

### `paywall_viewed`

| | |
|---|---|
| **Quando dispara** | Primeiro `onAppear` de cada `PaywallView`. |
| **Onde no app** | Views/Paywall/PaywallView.swift: `.onAppear` ao lado do `.task` (:110), protegido por `@State registrouVisita` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `source` | string | sim | `post_onboarding` \| `home_header` \| `locked_book` \| `parents` | `OrigemDoPaywall.fonte` |
| `book_id` | string | não | `^[a-z0-9-]{1,64}$` | só com `source = locked_book` |
| `book_source` | string | não | `week_card` \| `continue_strip` \| `just_arrived` \| `collection` \| `all_books` \| `library_grid` | só com `source = locked_book`: onde o livro bloqueado foi tocado |
| `has_saved_progress` | boolean | não | true \| false | só com `source = locked_book`: `ReadingProgress.started` (assinante que expirou) |
| `onboarding_trigger` | string | não | `first_run` \| `replay` | só com `source = post_onboarding` |
| `products_ready` | boolean | sim | true \| false | `prontos` (os dois planos em memória) |
| `trial_eligible` | boolean | sim | true \| false | `store.trialEligible` |

### `plan_selected`

| | |
|---|---|
| **Quando dispara** | Toque num cartão de plano diferente do selecionado. |
| **Onde no app** | Views/Paywall/PaywallView.swift: ação do Button em `cartao(_:produto:)` (:307), depois de `guard p != plano` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `plan` | string | sim | `annual` \| `monthly` | — |
| `trial_eligible` | boolean | sim | true \| false | — |
| `source` | string | sim | `post_onboarding` \| `home_header` \| `locked_book` \| `parents` | — |

### `subscribe_tapped`

| | |
|---|---|
| **Quando dispara** | Toque em `botaoAssinar` que passou pelo guard e vai abrir o portão. |
| **Onde no app** | Views/Paywall/PaywallView.swift: `pedirPortao()`, depois do `guard` (:638), antes de `mostrandoPortao = true` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `plan` | string | sim | `annual` \| `monthly` | — |
| `cta_variant` | string | sim | `free_trial` \| `subscribe` | `comTeste` → free_trial |
| `source` | string | sim | `post_onboarding` \| `home_header` \| `locked_book` \| `parents` | — |

### `parental_gate_shown`

| | |
|---|---|
| **Quando dispara** | Primeiro `onAppear` de cada `ParentalGate`. |
| **Onde no app** | Views/Paywall/ParentalGate.swift: `.onAppear` no body, protegido por `@State registrouVisita` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `purpose` | string | sim | `subscribe` \| `manage_subscription` | subscribe (PaywallView) ou manage_subscription (ParentsView) |

### `parental_gate_passed`

| | |
|---|---|
| **Quando dispara** | Resposta certa em `conferir()`. |
| **Onde no app** | Views/Paywall/ParentalGate.swift: `conferir()`, dentro de `if valor == pergunta.resposta`, antes de `onPass()` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `purpose` | string | sim | `subscribe` \| `manage_subscription` | — |
| `failed_attempts` | integer | sim | 0…99 | `erros` nesta apresentação |
| `was_paused` | boolean | sim | true \| false | houve ao menos uma pausa de 10 s |
| `duration_bucket` | string | sim | `lt_5s` \| `5_15s` \| `15_30s` \| `30_60s` \| `1_3m` \| `gte_3m` | desde `parental_gate_shown` |

### `parental_gate_failed`

| | |
|---|---|
| **Quando dispara** | Resposta errada em `conferir()`. |
| **Onde no app** | Views/Paywall/ParentalGate.swift: `conferir()`, depois do `if/else` da pausa (:317-322), antes de `mensagem = aviso` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `purpose` | string | sim | `subscribe` \| `manage_subscription` | — |
| `consecutive_errors` | integer | sim | 1…3 | `errosSeguidos` |
| `caused_pause` | boolean | sim | true \| false | `pausado` depois da decisão |

### `parental_gate_cancelled`

| | |
|---|---|
| **Quando dispara** | Toque no X do portão ou gesto de escape; uma vez por apresentação. |
| **Onde no app** | Views/Paywall/ParentalGate.swift: novo `cancelar()` usado pelo Button da `barra` (:110) e por `.accessibilityAction(.escape)` (:56) |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `purpose` | string | sim | `subscribe` \| `manage_subscription` | — |
| `failed_attempts` | integer | sim | 0…99 | `erros` |
| `while_paused` | boolean | sim | true \| false | `pausado` |
| `had_partial_answer` | boolean | sim | true \| false | `!resposta.isEmpty`; nunca os dígitos |

### `purchase_completed`

| | |
|---|---|
| **Quando dispara** | `store.comprar` devolveu `.sucesso`. |
| **Onde no app** | Views/Paywall/PaywallView.swift: `assinar()`, `case .sucesso`, antes de `fechar(.purchaseCompleted)` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `plan` | string | sim | `annual` \| `monthly` | capturado antes do `Task` |
| `with_trial` | boolean | sim | true \| false | `comTeste` capturado antes do `Task` |
| `source` | string | sim | `post_onboarding` \| `home_header` \| `locked_book` \| `parents` | — |

### `purchase_pending`

| | |
|---|---|
| **Quando dispara** | `store.comprar` devolveu `.pendente`. |
| **Onde no app** | Views/Paywall/PaywallView.swift: `assinar()`, `case .pendente`, ao lado de `pendente = true` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `plan` | string | sim | `annual` \| `monthly` | — |
| `with_trial` | boolean | sim | true \| false | — |
| `source` | string | sim | `post_onboarding` \| `home_header` \| `locked_book` \| `parents` | — |

### `purchase_cancelled`

| | |
|---|---|
| **Quando dispara** | `store.comprar` devolveu `.cancelado`. |
| **Onde no app** | Views/Paywall/PaywallView.swift: `assinar()`, `case .cancelado` (no lugar do `break`) |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `plan` | string | sim | `annual` \| `monthly` | — |
| `with_trial` | boolean | sim | true \| false | — |
| `source` | string | sim | `post_onboarding` \| `home_header` \| `locked_book` \| `parents` | — |

### `purchase_failed`

| | |
|---|---|
| **Quando dispara** | `store.comprar` lançou erro. |
| **Onde no app** | Views/Paywall/PaywallView.swift: `assinar()`, primeira linha do `catch`, antes de `avisar` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `plan` | string | sim | `annual` \| `monthly` | — |
| `with_trial` | boolean | sim | true \| false | — |
| `source` | string | sim | `post_onboarding` \| `home_header` \| `locked_book` \| `parents` | — |
| `error_category` | string | sim | `network` \| `not_available_in_storefront` \| `purchase_not_allowed` \| `unverified` \| `storekit_other` \| `purchase_error_other` \| `unknown` | `Store.categoriaDoErro(_:)` |

### `restore_tapped`

| | |
|---|---|
| **Quando dispara** | Toque em Restore que passou pelo `guard !restaurando`. |
| **Onde no app** | PaywallView.swift `restaurar()` (:676) e ParentsView.swift `restaurar()` (:380), depois do guard |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `screen` | string | sim | `paywall` \| `parents` | — |
| `source` | string | não | `post_onboarding` \| `home_header` \| `locked_book` \| `parents` | só com `screen = paywall` |

### `restore_finished`

| | |
|---|---|
| **Quando dispara** | Fim da tarefa de restauração (sucesso, nada encontrado, cancelado ou erro). |
| **Onde no app** | PaywallView.swift `restaurar()` (:682 e `catch` :692) e ParentsView.swift `restaurar()` (:386 e `catch` :397) |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `screen` | string | sim | `paywall` \| `parents` | — |
| `result` | string | sim | `subscription_found` \| `no_subscription_found` \| `cancelled` \| `failed` | `cancelled` = desistiu do login da Apple |
| `error_category` | string | não | `network` \| `not_available_in_storefront` \| `purchase_not_allowed` \| `unverified` \| `storekit_other` \| `purchase_error_other` \| `unknown` | só com `result = failed` |
| `source` | string | não | `post_onboarding` \| `home_header` \| `locked_book` \| `parents` | só com `screen = paywall` |

### `paywall_closed`

| | |
|---|---|
| **Quando dispara** | Primeira chamada de `fechar(_:)`: X, escape, compra concluída ou premium ativado. |
| **Onde no app** | Views/Paywall/PaywallView.swift: `fechar(_ motivo:)`, depois de `guard !fechou; fechou = true`, antes de `onClose()` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `source` | string | sim | `post_onboarding` \| `home_header` \| `locked_book` \| `parents` | — |
| `reason` | string | sim | `dismissed` \| `purchase_completed` \| `restored` \| `premium_activated_elsewhere` | X e escape viram `dismissed` (sem revelar VoiceOver) |
| `plans_ready` | boolean | sim | true \| false | `prontos` no fechamento |
| `selected_plan` | string | sim | `annual` \| `monthly` | `plano` |
| `purchase_pending` | boolean | sim | true \| false | `pendente` |
| `duration_bucket` | string | sim | `lt_5s` \| `5_15s` \| `15_30s` \| `30_60s` \| `1_3m` \| `gte_3m` | desde `paywall_viewed` |

### `subscription_status_changed`

| | |
|---|---|
| **Quando dispara** | `isPremium` muda depois que a primeira `atualizarAssinatura` do processo terminou. |
| **Onde no app** | Services/Store.swift: `atualizarAssinatura(motivo:)`, no lugar da linha 175 |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `new_state` | string | sim | `premium` \| `free` | valor novo de `isPremium` |
| `trigger` | string | sim | `launch` \| `transaction_update` \| `purchase` \| `restore` \| `foreground` \| `paywall_open` | `motivo` passado por quem chamou |
| `plan` | string | não | `annual` \| `monthly` | premium: plano da transação ativa; free: último plano ativo conhecido |
| `is_family_shared` | boolean | não | true \| false | só premium: `ownershipType == .familyShared` |
| `in_free_trial` | boolean | não | true \| false | só premium: `offer?.type == .introductory` |

## Área dos pais

### `reading_progress_reset`

| | |
|---|---|
| **Quando dispara** | Toque em Restart no diálogo "Restart all books?" (Cancel não conta). |
| **Onde no app** | Views/Parents/ParentsView.swift: ação do `Button("Restart", role: .destructive)` (:90), antes de `progress.resetAll()` |

| Propriedade | Tipo | Obrigatória | Valores | Como calcular |
|---|---|---|---|---|
| `books_in_progress_bucket` | string | sim | `1` \| `2_5` \| `6_plus` | `emAndamento` antes do reset |

## Fora do catálogo (de propósito)

- `reading_progress_saved`, `book_started`: duplicam `page_turned` / `reader_closed.end_spread`.
- `book_open_blocked`, `locked_book_tapped`, `home_subscribe_pill_tapped`, `parents_see_plans_tapped`: são `paywall_viewed.source`.
- `purchase_started`, `manage_subscription_tapped/opened`: equivalem a `parental_gate_passed` / `parental_gate_shown` com o `purpose`.
- `paywall_plans_shown`, `paywall_plans_load_failed`, `paywall_plans_retry_tapped`: cobertos por `store_products_*` com `fetch_context` e por `paywall_closed.plans_ready`.
- `parental_gate_paused`, `parental_gate_pause_ended`: `parental_gate_failed.caused_pause`.
- `restart_books_tapped/cancelled`, `manage_subscription_closed`, `intro_replay_tapped`, `legal_link_opened`, `library_search_cleared`, `library_empty_state_viewed`, `home_rail_scrolled`, `reader_edge_nudged`, `reader_swipe_ignored`, `reading_progress_invalid`: baixo valor ou já derivável (contadores em `reader_closed`, `result_count = 0`, `resume_state`).
- `coming_soon_book_tapped`: só existiria removendo `.disabled(!book.isAvailable)`, decisão de produto pendente.
- O próprio toggle "Share anonymous usage data" não gera evento, nem ao ligar nem ao desligar.
- Nunca: texto da busca, título ou texto de livro, pergunta/resposta do portão, `localizedDescription` de erro, URL, tamanho em pixels, Reduzir Movimento, VoiceOver, fuso horário, modelo do aparelho.
