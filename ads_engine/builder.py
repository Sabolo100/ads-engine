"""A Search-kampányfa felépítése: a csomagból (brief + kész hirdetések) EGY atomi Google Ads mutate.

A fa: napi keret · `ads-engine` címke · kampány (SZÜNETELVE) · hely és nyelv · negatív kulcsszavak · hirdetéscsoportok
(kulcsszavak, reszponzív keresési hirdetés) · hivatkozások · kiemelések · képek. A kampány a „kapu”: szüneteltetve jön létre,
a gyerek-objektumok bekapcsolva; a go-live egyszerre állítja be a valódi keretet és kapcsolja be a kampányt.

A mezőneveket a próbák a hivatalos v25 leíró-dokumentum ellen ellenőrzik. Minden név determinisztikus (újrafuttatás nem
duplikál), a képek tartalom-hash névvel készülnek.
"""
import base64
import dataclasses
import itertools

from .guardrails import ENGINE_LABEL

PLACEHOLDER_DAILY = {"HUF": 100}              # a szüneteltetett kampány helyőrző kerete (valódi keretet a go-live állít)
DEFAULT_MAX_CPC = {"HUF": 150}
UTM_SUFFIX = "utm_source=google&utm_medium=cpc&utm_campaign={slug}&utm_content={{creative}}&utm_term={{keyword}}"
LABEL_COLOR = "#4285F4"
LOCALE_FALLBACK = {("HU", "hu"): ("geoTargetConstants/2348", "languageConstants/1024")}


def campaign_name(project):
    return f"{project.name} | Kereső"


def budget_name(project):
    return f"{project.name} | Kereső – napi keret"


def placeholder_daily_micros(currency):
    return int(PLACEHOLDER_DAILY.get(currency, 1)) * 1_000_000


def max_cpc_micros(brief, currency):
    cpc = brief.get("ads", {}).get("max_cpc") or DEFAULT_MAX_CPC.get(currency, 1)
    return int(round(float(cpc) * 1_000_000))


def utm_suffix(project):
    return UTM_SUFFIX.format(slug=f"{project.slug}-kereso")


@dataclasses.dataclass
class Tree:
    ops: list              # a teljes fa (próbafuttatáshoz: kampány + képek egyben)
    summary: dict
    names: dict            # kulcs → ideiglenes erőforrásnév (a naplózáshoz és a visszakereséshez)
    core_len: int = 0      # az első core_len művelet a kampány képek NÉLKÜL; éles üzemben a képek külön kérésben mennek
    campaign_index: int = 0

    @property
    def core_ops(self):
        return self.ops[:self.core_len]


class _Temp:
    def __init__(self, cid):
        self.cid, self._n = cid, itertools.count(1)

    def __call__(self, collection):
        return f"customers/{self.cid}/{collection}/-{next(self._n)}"


def build_search_tree(project, pack, *, customer_id, daily_budget_micros=None, label_rn=None, geo_rn, language_rn, images=(), currency="HUF"):
    """images: kész (images.Prepared) kép-eszközök a Google méreteire. Visszaad egy Tree-t (műveletek + összegzés)."""
    brief, creatives = pack.brief, pack.creatives
    tmp = _Temp(customer_id)
    ops, names = [], {}
    landing = {p["id"]: p["url"] for p in brief["landing_pages"]}
    budget = tmp("campaignBudgets")
    names["budget"] = budget
    ops.append({"campaignBudgetOperation": {"create": {
        "resourceName": budget, "name": budget_name(project), "amountMicros": str(daily_budget_micros or placeholder_daily_micros(currency)),
        "deliveryMethod": "STANDARD", "explicitlyShared": False}}})
    label = label_rn
    if not label:
        label = tmp("labels")
        names["label"] = label
        ops.append({"labelOperation": {"create": {"resourceName": label, "name": ENGINE_LABEL,
                                                  "textLabel": {"backgroundColor": LABEL_COLOR, "description": "Az Ads Engine kezeli"}}}})
    campaign = tmp("campaigns")
    names["campaign"] = campaign
    campaign_index = len(ops)
    ops.append({"campaignOperation": {"create": {
        "resourceName": campaign, "name": campaign_name(project), "status": "PAUSED", "advertisingChannelType": "SEARCH",
        "campaignBudget": budget,
        "targetSpend": {"cpcBidCeilingMicros": str(max_cpc_micros(brief, currency))},
        "networkSettings": {"targetGoogleSearch": True, "targetSearchNetwork": False, "targetContentNetwork": False,
                            "targetPartnerSearchNetwork": False},
        "geoTargetTypeSetting": {"positiveGeoTargetType": "PRESENCE"},
        "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
        "aiMaxSetting": {"enableAiMax": False},
        "finalUrlSuffix": utm_suffix(project)}}})
    ops.append({"campaignLabelOperation": {"create": {"campaign": campaign, "label": label}}})
    ops.append({"campaignCriterionOperation": {"create": {"campaign": campaign, "location": {"geoTargetConstant": geo_rn}}}})
    ops.append({"campaignCriterionOperation": {"create": {"campaign": campaign, "language": {"languageConstant": language_rn}}}})
    n_neg = 0
    for neg in brief["keywords"].get("negatives", []):
        ops.append({"campaignCriterionOperation": {"create": {"campaign": campaign, "negative": True,
                                                              "keyword": {"text": neg, "matchType": "PHRASE"}}}})
        n_neg += 1
    n_kw = n_ads = 0
    for a in creatives["adsets"]:
        ag = tmp("adGroups")
        names[f"adgroup:{a['id']}"] = ag
        ops.append({"adGroupOperation": {"create": {"resourceName": ag, "name": a["theme"], "campaign": campaign,
                                                    "status": "ENABLED", "type": "SEARCH_STANDARD"}}})
        for k in a["keywords"]:
            ops.append({"adGroupCriterionOperation": {"create": {"adGroup": ag, "status": "ENABLED",
                                                                 "keyword": {"text": k["text"], "matchType": k["match"]}}}})
            n_kw += 1
        for neg in a.get("negatives", []):
            ops.append({"adGroupCriterionOperation": {"create": {"adGroup": ag, "negative": True,
                                                                 "keyword": {"text": neg, "matchType": "PHRASE"}}}})
            n_neg += 1
        rsa = {"headlines": [{"text": h} for h in a["headlines"]], "descriptions": [{"text": d} for d in a["descriptions"]]}
        for p in ("path1", "path2"):
            if a.get(p):
                rsa[p] = a[p]
        ops.append({"adGroupAdOperation": {"create": {"adGroup": ag, "status": "ENABLED",
                                                      "ad": {"finalUrls": [landing[a["landing"]]], "responsiveSearchAd": rsa}}}})
        n_ads += 1
    n_sl = n_co = 0
    for s in creatives.get("sitelinks", []):
        asset = tmp("assets")
        sl = {"linkText": s["text"]}
        for d in ("description1", "description2"):
            if s.get(d):
                sl[d] = s[d]
        ops.append({"assetOperation": {"create": {"resourceName": asset, "sitelinkAsset": sl, "finalUrls": [landing[s["landing"]]]}}})
        ops.append({"campaignAssetOperation": {"create": {"campaign": campaign, "asset": asset, "fieldType": "SITELINK"}}})
        n_sl += 1
    for c in creatives.get("callouts", []):
        asset = tmp("assets")
        ops.append({"assetOperation": {"create": {"resourceName": asset, "calloutAsset": {"calloutText": c}}}})
        ops.append({"campaignAssetOperation": {"create": {"campaign": campaign, "asset": asset, "fieldType": "CALLOUT"}}})
        n_co += 1
    core_len = len(ops)
    ops += image_ops(tmp, campaign, images)
    n_img = len(images)
    summary = {"campaign": campaign_name(project), "ad_groups": len(creatives["adsets"]), "keywords": n_kw, "negatives": n_neg,
               "ads": n_ads, "sitelinks": n_sl, "callouts": n_co, "images": n_img, "operations": len(ops),
               "daily_budget_micros": daily_budget_micros or placeholder_daily_micros(currency),
               "max_cpc_micros": max_cpc_micros(brief, currency)}
    return Tree(ops, summary, names, core_len=core_len, campaign_index=campaign_index)


def image_ops(tmp, campaign_rn, images, existing=None):
    """Kép-eszközök létrehozása és a kampányhoz kötése. existing: {név: erőforrásnév} – a már feltöltött képet nem töltjük fel újra."""
    existing = existing or {}
    ops = []
    for im in images:
        asset = existing.get(im.name)
        if not asset:
            asset = tmp("assets")
            ops.append({"assetOperation": {"create": {"resourceName": asset, "name": im.name,
                                                      "imageAsset": {"data": base64.b64encode(im.data).decode("ascii")}}}})
        ops.append({"campaignAssetOperation": {"create": {"campaign": campaign_rn, "asset": asset, "fieldType": "AD_IMAGE"}}})
    return ops


def build_image_ops(customer_id, campaign_rn, images, existing=None):
    """Éles üzemben a kampány létrehozása UTÁN, külön kérésben: így egy kép hibája nem viszi magával a kampányt."""
    return image_ops(_Temp(customer_id), campaign_rn, images, existing)
