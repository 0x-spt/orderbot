# Marketplace APIs: what the adapters rely on

Each fact below is marked **verified** (checked against the live API with read-only calls,
or against the official OpenAPI spec) or **assumed** (taken from documentation that could
not be checked). Calls that change state (confirm, cancel, stock write) were never made
during development; each is a single request with no retries.

## Ozon Seller API

| Call | Use | Status |
|---|---|---|
| `POST /v4/posting/fbs/unfulfilled/list` | new orders (`awaiting_packaging`), assembled stock (`awaiting_deliver`) | verified |
| `POST /v3/posting/fbs/get` | re-read one posting before confirming | verified |
| `POST /v4/product/info/attributes` | brand (attribute 85) | verified call, attribute id assumed |
| `POST /v2/product/info/stocks-by-warehouse/fbs` | stock read for the sync | verified |
| `POST /v4/posting/fbs/ship` | confirm | assumed |
| `POST /v2/products/stocks` | stock write | assumed |

Findings that shaped the adapter:
- `v3 …/unfulfilled/list` returns 404. The v4 response has no `result` wrapper.
- `filter.status` is silently ignored; only `filter.statuses: [...]` filters. The adapter
  also filters by status on the client.
- `customer_price` matches «Оплачено покупателем» in the seller cabinet (verified for
  quantity 1). Its type depends on the endpoint: an object `{amount, currency}` in the v4
  list, a plain number in v3 `get`. The parser accepts both.
- Whether `customer_price` is per unit or per line for quantity > 1 is unknown, so it is
  a setting (`OZON_PAID_IS_UNIT`). While unset, such lines go to a person.
- Pagination: `has_next` with an empty or repeated `cursor` raises instead of looping.

## Yandex Market Partner API

Auth header `Api-Key: <token>`, base `https://api.partner.market.yandex.ru`.

| Call | Use | Status |
|---|---|---|
| `GET /v2/campaigns` | list of shops (campaigns) | verified |
| `GET /v2/campaigns/{id}/orders?status=PROCESSING` | new and ready orders | verified (marked deprecated in docs, still answers) |
| `PUT /v2/campaigns/{id}/orders/{orderId}/status` | confirm: `PROCESSING/READY_TO_SHIP` | verified against docs |
| `GET …/orders/{orderId}/delivery/labels` | labels PDF, fetched before confirming | assumed |
| `POST /v2/businesses/{id}/offer-mappings` | brand (`offer.vendor`) | assumed |
| `POST /v2/campaigns/{id}/offers/stocks` | stock read (FIT / FREEZE / AVAILABLE) | verified |
| `GET /v2/businesses/{id}/warehouses` | warehouse groups with a shared stock | verified |
| `PUT /v2/campaigns/{id}/offers/stocks` | stock write | assumed; off by default |

Findings:
- `buyerPrice` is per unit (verified with count 2), so `paid = buyerPrice × count`.
- Several campaigns can share one stock (a warehouse group). The sync writes such a group
  once, through its main campaign.
- Whether the write's `count` means FIT or AVAILABLE is not confirmed. The sync writes
  AVAILABLE − sold, which can never overstate the stock, and the switch is off by default.

## Wildberries API

Header `Authorization: <token>`, base `https://marketplace-api.wildberries.ru`.

| Call | Use | Status |
|---|---|---|
| `GET /api/v3/orders/new` | new assembly tasks | verified |
| `GET /api/v3/orders` + `POST /api/v3/orders/status` | statuses, assembled stock | verified |
| `GET /api/v3/warehouses` | warehouse ids | verified |
| `POST /api/v3/supplies`, `PATCH /api/marketplace/v3/supplies/{id}/orders` | confirm: a supply named after the order, then add the order | verified against OpenAPI |
| `POST/PUT /api/v3/stocks/{warehouseId}` | stock read / write by `chrtId` | read verified, write assumed |
| `POST content-api…/content/v2/get/cards/list` | brand, `chrtId` of an article | verified |

Findings:
- One assembly task is one unit of one article: every order has one line with qty 1.
- Money fields are in kopecks.
- Stock is keyed by `chrtId` (size id), not by barcode. A card with several sizes is refused.
- Awaiting means `supplierStatus=new` and `wbStatus=waiting`. Statuses are matched against
  explicit positive lists; anything else is neither awaiting nor ready.
- A stock write with a wrong field name still returns 204 without changing anything, so the
  response code alone proves nothing.
