"""Google Ads riportadatok: a GAQL-sorokat egyszerű, számokká alakított szótárakká normalizálja (a Google int64-et szövegként ad)."""
from . import gaql
from .guardrails import ENGINE_LABEL


def _i(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _m(row):
    m = row.get("metrics", {}) or {}
    return {"impressions": _i(m.get("impressions")), "clicks": _i(m.get("clicks")), "cost_micros": _i(m.get("costMicros"))}


def label_resource(client, cid):
    rows = client.search(cid, f"SELECT label.resource_name FROM label WHERE label.name = {gaql.quote(ENGINE_LABEL)} AND label.status = 'ENABLED'")
    return rows[0]["label"]["resourceName"] if rows else None


def owned_campaigns(client, cid):
    """A motor kampányai (az `ads-engine` címkével): azonosító, név, állapot, napi keret."""
    label = label_resource(client, cid)
    if not label:
        return []
    out = []
    for r in client.search(cid, gaql.OWNED_CAMPAIGNS):
        c = r.get("campaign", {})
        if label not in (c.get("labels") or []):
            continue
        out.append({"id": str(c.get("id") or c.get("resourceName", "").rsplit("/", 1)[-1]), "resource_name": c.get("resourceName"),
                    "name": c.get("name"), "status": c.get("status"), "primary_status": c.get("primaryStatus"),
                    "budget_rn": c.get("campaignBudget"), "daily_micros": _i(r.get("campaignBudget", {}).get("amountMicros"))})
    return out


def daily_cost(client, cid, ids, since, until):
    """{dátum: {impressions, clicks, cost_micros}} – a kampányok összege naponta."""
    out = {}
    if not ids:
        return out
    for r in client.search(cid, gaql.campaign_daily(ids, since, until)):
        d = r.get("segments", {}).get("date")
        slot = out.setdefault(d, {"impressions": 0, "clicks": 0, "cost_micros": 0})
        for k, v in _m(r).items():
            slot[k] += v
    return out


def totals(daily):
    t = {"impressions": 0, "clicks": 0, "cost_micros": 0}
    for v in daily.values():
        for k in t:
            t[k] += v.get(k, 0)
    t["ctr"] = (t["clicks"] / t["impressions"]) if t["impressions"] else 0.0
    t["avg_cpc_micros"] = (t["cost_micros"] / t["clicks"]) if t["clicks"] else 0.0
    return t


def keyword_rows(client, cid, ids, since, until):
    out = []
    if not ids:
        return out
    for r in client.search(cid, gaql.keywords(ids, since, until)):
        crit, ag = r.get("adGroupCriterion", {}), r.get("adGroup", {})
        kw = crit.get("keyword", {})
        out.append({"campaign_id": str(r.get("campaign", {}).get("id", "")), "ad_group_id": str(ag.get("id", "")), "ad_group": ag.get("name", ""),
                    "criterion_id": str(crit.get("criterionId", "")), "text": kw.get("text", ""), "match": kw.get("matchType", ""),
                    "status": crit.get("status", ""), "quality_score": _i(crit.get("qualityInfo", {}).get("qualityScore")), **_m(r),
                    "resource_name": crit.get("resourceName", "")})
    return out


def keyword_status_rows(client, cid, ids):
    """A pozitív kulcsszavak (nem eltávolított) állapota metrikák nélkül: [{campaign_id, ad_group_id, ad_group, text, match, status, resource_name}]."""
    out = []
    if not ids:
        return out
    for r in client.search(cid, gaql.keyword_status(ids)):
        crit, ag = r.get("adGroupCriterion", {}), r.get("adGroup", {})
        kw = crit.get("keyword", {})
        if not kw.get("text"):
            continue
        out.append({"campaign_id": str(r.get("campaign", {}).get("id", "")), "ad_group_id": str(ag.get("id", "")), "ad_group": ag.get("name", ""),
                    "criterion_id": str(crit.get("criterionId", "")), "text": kw.get("text", ""), "match": kw.get("matchType", ""),
                    "status": crit.get("status", ""), "resource_name": crit.get("resourceName", "")})
    return out


def disapproval_details(client, cid, ids):
    """{hirdetés-azonosító: [{topic, type, texts[]}]}: az elutasítás szabályzati okai és a kifogásolt szövegek (ha van)."""
    out = {}
    if not ids:
        return out
    for r in client.search(cid, gaql.disapproved_details(ids)):
        ad = r.get("adGroupAd", {})
        entries = (ad.get("policySummary", {}) or {}).get("policyTopicEntries", []) or []
        out[str(ad.get("ad", {}).get("id", ""))] = [
            {"topic": e.get("topic", ""), "type": e.get("type", ""),
             "texts": [t for ev in (e.get("evidences") or []) for t in (ev.get("textList", {}) or {}).get("texts", [])]} for e in entries]
    return out


def negative_rows(client, cid, ids):
    """A meglévő negatív kulcsszavak (kampány- és hirdetéscsoport-szinten): [(szöveg, egyezés)] – a duplikálás elkerülésére."""
    out = []
    if not ids:
        return out
    for r in client.search(cid, gaql.campaign_negatives(ids)):
        k = r.get("campaignCriterion", {}).get("keyword", {})
        out.append({"scope": "campaign", "text": k.get("text", ""), "match": k.get("matchType", "")})
    for r in client.search(cid, gaql.negatives(ids)):
        k = r.get("adGroupCriterion", {}).get("keyword", {})
        out.append({"scope": "adgroup", "ad_group_id": str(r.get("adGroup", {}).get("id", "")), "text": k.get("text", ""), "match": k.get("matchType", "")})
    return out


def term_rows(client, cid, ids, since, until):
    out = []
    if not ids:
        return out
    for r in client.search(cid, gaql.search_terms(ids, since, until)):
        v = r.get("searchTermView", {})
        out.append({"term": v.get("searchTerm", ""), "status": v.get("status", ""), "campaign_id": str(r.get("campaign", {}).get("id", "")),
                    "ad_group_id": str(r.get("adGroup", {}).get("id", "")), **_m(r)})
    return out


def ad_rows(client, cid, ids, since, until):
    out = []
    if not ids:
        return out
    for r in client.search(cid, gaql.ads(ids, since, until)):
        a = r.get("adGroupAd", {})
        ps = a.get("policySummary", {}) or {}
        out.append({"campaign_id": str(r.get("campaign", {}).get("id", "")), "ad_group_id": str(r.get("adGroup", {}).get("id", "")),
                    "ad_id": str(a.get("ad", {}).get("id", "")), "status": a.get("status", ""), "approval": ps.get("approvalStatus", ""),
                    "review": ps.get("reviewStatus", ""), "added_by_google": bool(a.get("ad", {}).get("addedByGoogleAds")), **_m(r),
                    "resource_name": a.get("resourceName", "")})
    return out


def asset_label_rows(client, cid, ids):
    """Az RSA-szövegek Google-címkéi (BEST/GOOD/LOW/LEARNING/PENDING) – a szövegcsere alapja."""
    out = []
    if not ids:
        return out
    for r in client.search(cid, gaql.asset_labels(ids)):
        v, a = r.get("adGroupAdAssetView", {}), r.get("asset", {})
        out.append({"ad_group_id": str(r.get("adGroup", {}).get("id", "")), "ad_id": str(r.get("adGroupAd", {}).get("ad", {}).get("id", "")),
                    "field_type": v.get("fieldType", ""), "label": v.get("performanceLabel", ""), "asset_id": str(a.get("id", "")),
                    "text": a.get("textAsset", {}).get("text", "")})
    return out


def change_event_rows(client, cid, since_iso, until_iso):
    return client.search(cid, gaql.change_events(since_iso, until_iso))


def snapshot(client, cid, campaigns):
    """Az utolsó ismert élő állapot a módosításészleléshez: {erőforrásnév: {mező: érték}} (kampány + keret)."""
    snap = {}
    for c in campaigns:
        snap[c["resource_name"]] = {"status": c["status"], "name": c["name"]}
        if c.get("budget_rn"):
            snap[c["budget_rn"]] = {"amountMicros": str(c["daily_micros"])}
    return snap


def ad_content_rows(client, cid, ids):
    """Az RSA-k jelenlegi szövegei (a heti szövegcseréhez): [{ad_group_id, ad_id, status, headlines[], descriptions[], path1, path2, final_urls}]."""
    out = []
    if not ids:
        return out
    for r in client.search(cid, gaql.ad_content(ids)):
        a = r.get("adGroupAd", {})
        rsa = a.get("ad", {}).get("responsiveSearchAd", {}) or {}
        out.append({"ad_group_id": str(r.get("adGroup", {}).get("id", "")), "ad_id": str(a.get("ad", {}).get("id", "")), "status": a.get("status", ""),
                    "headlines": [h.get("text", "") for h in rsa.get("headlines", [])], "descriptions": [d.get("text", "") for d in rsa.get("descriptions", [])],
                    "pinned": {h["text"]: h["pinnedField"] for h in rsa.get("headlines", []) if h.get("pinnedField")},
                    "path1": rsa.get("path1", ""), "path2": rsa.get("path2", ""), "final_urls": a.get("ad", {}).get("finalUrls", []),
                    "resource_name": a.get("resourceName", "")})
    return out


def image_rows(client, cid, ids, since, until):
    """A kampányokhoz kötött kép-eszközök: [{campaign_id, resource_name (a kötésé), asset_name, status, impressions, clicks}]."""
    out = []
    if not ids:
        return out
    for r in client.search(cid, gaql.image_assets(ids, since, until)):
        ca, a = r.get("campaignAsset", {}), r.get("asset", {})
        out.append({"campaign_id": str(r.get("campaign", {}).get("id", "")), "resource_name": ca.get("resourceName", ""), "status": ca.get("status", ""),
                    "asset_id": str(a.get("id", "")), "asset_name": a.get("name", ""), **{k: v for k, v in _m(r).items() if k != "cost_micros"}})
    return out
