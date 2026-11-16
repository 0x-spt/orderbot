"""Read-only reconciliation: python -m orderbot.recon ozon <posting_number>...

`python -m orderbot.recon warehouses [main|second]` prints the Ozon warehouses and Yandex
shops (campaigns) of a company, for its sections of warehouses.yaml. A leading `second`
before ozon/yandex checks orders with the second company's keys."""
import asyncio
import json
import sys

import httpx

from orderbot.config import Config, load_config
from orderbot.domain import MAIN, SECOND
from orderbot.marketplaces.ozon import AWAITING, OzonMarketplace
from orderbot.marketplaces.wildberries import WildberriesMarketplace
from orderbot.marketplaces.yandex import BUYER_STOP_FLAG, YandexMarket


def _money_fields(d, prefix=""):
    """Flatten numeric/string leaves so every money-like field is printed."""
    for k, v in d.items():
        if isinstance(v, dict):
            yield from _money_fields(v, f"{prefix}{k}.")
        elif isinstance(v, (int, float, str)) and not isinstance(v, bool):
            yield f"{prefix}{k}", v


async def recon_ozon(m: OzonMarketplace, ids) -> str:
    out = []
    for pid in ids:
        raw = await m._fetch(pid)
        await m._fill_brands([p["offer_id"] for p in raw.get("products", [])])
        wh = (raw.get("delivery_method") or {}).get("warehouse")
        out.append(f"== {pid} status={raw.get('status')} warehouse={wh!r} "
                   f"mapped={m.warehouses.get(wh)!r}")
        fin = (raw.get("financial_data") or {}).get("products", [])
        for i, p in enumerate(raw.get("products", [])):
            out.append(f"-- line {i}: offer_id={p['offer_id']} qty={p['quantity']} "
                       f"price={p.get('price')} brand={m._brands.get(p['offer_id'])!r}")
            if i < len(fin):
                for k, v in _money_fields(fin[i]):
                    out.append(f"   financial_data.products[{i}].{k} = {v}")
        extra = {k: v for k, v in (raw.get("financial_data") or {}).items() if k != "products"}
        if extra:
            out.append("   financial_data (other): " + json.dumps(extra, ensure_ascii=False))
    return "\n".join(out)


async def recon_yandex(m: YandexMarket, ids) -> str:
    out = []
    for oid in ids:
        try:
            campaign, raw = await m._find(oid)
        except KeyError as e:
            out.append(f"== {oid} not found: {e}")
            continue
        await m._fill_brands([x["offerId"] for x in raw.get("items", [])])
        out.append(f"== {oid} status={raw.get('status')}/{raw.get('substatus')} "
                   f"campaign={campaign} warehouse={m._warehouse(campaign)!r} "
                   f"buyer_stop_flag={raw.get(BUYER_STOP_FLAG)!r}")
        for i, x in enumerate(raw.get("items", [])):
            out.append(f"-- line {i}: offerId={x['offerId']} count={x.get('count')} "
                       f"brand={m._brands.get(x['offerId'])!r}")
            for k, v in _money_fields(x):
                out.append(f"   items[{i}].{k} = {v}")
            for j, sub in enumerate(x.get("subsidies") or []):
                out.append(f"   items[{i}].subsidies[{j}] = {json.dumps(sub, ensure_ascii=False)}")
    return "\n".join(out)


async def recon_wb(m: WildberriesMarketplace, ids) -> str:
    """With no ids: the new assembly tasks and the warehouses (for warehouses.yaml)."""
    out = []
    if not ids:
        for w in await m._request(m._http, "GET", "/api/v3/warehouses"):
            out.append(f"warehouse id={w.get('id')} name={w.get('name')!r} "
                       f"mapped={m.warehouses.get(str(w.get('id')))!r}")
        raw = await m._new_raw()
    else:
        want = {int(i) for i in ids}
        raw = [o for o in await m._new_raw() + await m._recent_raw() if int(o["id"]) in want]
    st = await m._statuses([o["id"] for o in raw]) if raw else {}
    await m._fill_brands([o.get("nmId") for o in raw])
    for o in raw:
        s = st.get(int(o["id"]), (None, None))
        out.append(f"== {o['id']} status={s[0]}/{s[1]} article={o.get('article')} "
                   f"nmId={o.get('nmId')} warehouseId={o.get('warehouseId')} "
                   f"mapped={m._warehouse(o)!r} brand={m._brands.get(o.get('nmId'))!r}")
        for k, v in _money_fields(o):
            if any(w in k.lower() for w in ("price", "currency", "cost", "sum")):
                out.append(f"   {k} = {v}")
    return "\n".join(out)


def _keys(c: Config, account: str):
    """(ozon client id, ozon key, ym token, ym business id, env prefix) of the company."""
    if account == SECOND:
        return (c.second_ozon_client_id, c.second_ozon_api_key, c.second_ym_token,
                c.second_ym_business_id, "SECOND_")
    return c.ozon_client_id, c.ozon_api_key, c.ym_token, c.ym_business_id, ""


def _section(mp: str, account: str) -> str:
    return mp if account == MAIN else f"{mp}_{account}"


def _ozon(c: Config, account: str) -> OzonMarketplace:
    cid, key, *_ = _keys(c, account)
    return OzonMarketplace(cid, key, c.warehouses.get(_section("ozon", account), {}),
                           c.ozon_paid_field, c.ozon_paid_is_unit, account=account)


def _yandex(c: Config, account: str) -> YandexMarket:
    _, _, token, business, _ = _keys(c, account)
    if account == MAIN:
        return YandexMarket(token, c.ym_campaign_id or None, c.warehouses.get("yandex", {}),
                            c.ym_paid_field, business or None)
    return YandexMarket(token, None, c.warehouses.get(_section("yandex", account), {}),
                        c.ym_paid_field, business or None, account=account)


async def warehouses_text(c: Config, account: str) -> str:
    """Read-only: the company's Ozon warehouse names and Yandex campaign ids, each with what
    its warehouses.yaml section maps it to now."""
    cid, key, token, _, prefix = _keys(c, account)
    name = c.company_names.get(account, account)
    out = []
    oz_sec, ym_sec = _section("ozon", account), _section("yandex", account)
    if cid and key:
        m = _ozon(c, account)
        out.append(f"== {name}: Озон, раздел {oz_sec} файла складов "
                   "(склад площадки: склад 1С)")
        try:
            data = await m._post("/v1/warehouse/list", {}, retry=False)
            for w in data.get("result") or []:
                out.append(f"warehouse id={w.get('warehouse_id')} name={w.get('name')!r} "
                           f"mapped={m.warehouses.get(w.get('name'))!r}")
        except httpx.HTTPStatusError as e:
            # the key may lack the role for this method (seen live: HTTP 403)
            out.append(f"список складов недоступен (HTTP {e.response.status_code}), "
                       "склады из текущих заказов:")
            names = {}
            for status in (AWAITING, "awaiting_deliver"):
                for p in await m._list(status):
                    wh = (p.get("delivery_method") or {}).get("warehouse")
                    if wh:
                        names[wh] = None
            for wh in names:
                out.append(f"warehouse name={wh!r} mapped={m.warehouses.get(wh)!r}")
    else:
        out.append(f"== {name}: Озон не подключён: задайте {prefix}OZON_CLIENT_ID и "
                   f"{prefix}OZON_API_KEY в .env")
    if token:
        y = _yandex(c, account)
        out.append(f"== {name}: Маркет, раздел {ym_sec} файла складов (id кампании: склад 1С)")
        page, pages = 1, 1
        while page <= pages:
            data = (await y._request("GET", "/v2/campaigns", params={"page": page})).json()
            for x in data.get("campaigns") or []:
                out.append(f"campaign id={x.get('id')} domain={x.get('domain')!r} "
                           f"placement={x.get('placementType')} "
                           f"business={(x.get('business') or {}).get('id')} "
                           f"mapped={y.warehouses.get(str(x.get('id')))!r}")
            pages = int((data.get("pager") or {}).get("pagesCount") or 1)
            page += 1
    else:
        out.append(f"== {name}: Маркет не подключён: задайте {prefix}YM_TOKEN в .env")
    return "\n".join(out)


async def main(argv):
    account = MAIN
    if argv and argv[0] == "warehouses":
        acc = argv[1] if len(argv) > 1 else MAIN
        if acc not in (MAIN, SECOND):
            print("usage: python -m orderbot.recon warehouses [main|second]")
            return 2
        print(await warehouses_text(load_config(), acc))
        return 0
    if argv and argv[0] in (MAIN, SECOND):
        account, argv = argv[0], argv[1:]
    if argv and argv[0] == "wb":
        c = load_config()
        m = WildberriesMarketplace(c.wb_token, c.warehouses.get("wb", {}), c.wb_paid_field)
        print(await recon_wb(m, argv[1:]))
        return 0
    if len(argv) >= 2 and argv[0] == "yandex":
        print(await recon_yandex(_yandex(load_config(), account), argv[1:]))
        return 0
    if len(argv) < 2 or argv[0] != "ozon":
        print("usage: python -m orderbot.recon [second] ozon|yandex <order_id>... | "
              "wb [order_id...] | warehouses [main|second]")
        return 2
    print(await recon_ozon(_ozon(load_config(), account), argv[1:]))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
