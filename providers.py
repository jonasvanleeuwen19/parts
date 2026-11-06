import re
import time
import requests

TIMEOUT = 10
_token = {}


def to_price(text):
    if isinstance(text, (int, float)):
        return float(text)
    cleaned = re.sub(r"[^\d.,]", "", text or "")
    if not cleaned:
        return None
    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def to_int(text):
    digits = re.sub(r"\D", "", str(text or ""))
    return int(digits) if digits else None


def mouser(cfg, query):
    response = requests.post(
        "https://api.mouser.com/api/v1/search/keyword",
        params={"apiKey": cfg["api_key"]},
        json={"SearchByKeywordRequest": {"keyword": query, "records": 20, "startingRecord": 0}},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("Errors"):
        raise RuntimeError(data["Errors"][0].get("Message", "Mouser error"))
    results = []
    for part in data["SearchResults"]["Parts"]:
        breaks = []
        for pb in part.get("PriceBreaks", []):
            price = to_price(pb.get("Price"))
            if price is not None:
                breaks.append({"qty": pb.get("Quantity"), "price": price, "currency": pb.get("Currency")})
        datasheet = part.get("DataSheetUrl")
        results.append({
            "provider": "Mouser",
            "mpn": part.get("ManufacturerPartNumber", ""),
            "manufacturer": part.get("Manufacturer", ""),
            "name": part.get("ManufacturerPartNumber", ""),
            "description": part.get("Description", ""),
            "image": part.get("ImagePath") or "",
            "category": part.get("Category", ""),
            "datasheets": [datasheet] if datasheet else [],
            "specs": [],
            "lifecycle": part.get("LifecycleStatus"),
            "offers": [{
                "distributor": "Mouser",
                "url": part.get("ProductDetailUrl", ""),
                "stock": to_int(part.get("Availability")),
                "moq": to_int(part.get("Min")),
                "mult": to_int(part.get("Mult")),
                "breaks": breaks,
            }],
        })
    return results


NEXAR_QUERY = """query($q:String!){supSearch(q:$q,limit:20){results{part{mpn name shortDescription
manufacturer{name} bestImage{url} bestDatasheet{url} category{name}
specs{attribute{name} displayValue}
sellers{company{name} offers{inventoryLevel moq orderMultiple clickUrl prices{quantity price currency}}}}}}}"""


def nexar_token(cfg):
    cached = _token.get(cfg["client_id"])
    if cached and cached[1] > time.time():
        return cached[0]
    response = requests.post(
        "https://identity.nexar.com/connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "scope": "supply.domain",
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    body = response.json()
    _token[cfg["client_id"]] = (body["access_token"], time.time() + body.get("expires_in", 3600) - 60)
    return body["access_token"]


def nexar(cfg, query):
    response = requests.post(
        "https://api.nexar.com/graphql",
        json={"query": NEXAR_QUERY, "variables": {"q": query}},
        headers={"Authorization": "Bearer " + nexar_token(cfg)},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        raise RuntimeError(body["errors"][0].get("message", "Nexar error"))
    results = []
    for item in body["data"]["supSearch"]["results"] or []:
        part = item["part"]
        offers = []
        for seller in part.get("sellers") or []:
            for offer in seller.get("offers") or []:
                breaks = [
                    {"qty": p["quantity"], "price": p["price"], "currency": p["currency"]}
                    for p in offer.get("prices") or []
                    if p.get("price") is not None
                ]
                offers.append({
                    "distributor": seller["company"]["name"],
                    "url": offer.get("clickUrl") or "",
                    "stock": offer.get("inventoryLevel"),
                    "moq": offer.get("moq"),
                    "mult": offer.get("orderMultiple"),
                    "breaks": breaks,
                })
        datasheet = (part.get("bestDatasheet") or {}).get("url")
        results.append({
            "provider": "Nexar",
            "mpn": part.get("mpn", ""),
            "manufacturer": (part.get("manufacturer") or {}).get("name", ""),
            "name": part.get("name") or part.get("mpn", ""),
            "description": part.get("shortDescription") or "",
            "image": (part.get("bestImage") or {}).get("url") or "",
            "category": (part.get("category") or {}).get("name", ""),
            "datasheets": [datasheet] if datasheet else [],
            "specs": [
                {"name": s["attribute"]["name"], "value": s["displayValue"], "unit": ""}
                for s in part.get("specs") or []
            ],
            "offers": offers,
        })
    return results


PROVIDERS = {
    "mouser": {"label": "Mouser", "search": mouser, "fields": ["api_key"]},
    "nexar": {"label": "Nexar / Octopart", "search": nexar, "fields": ["client_id", "client_secret"]},
}
