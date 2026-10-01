"""Közös segédek a próbákhoz: álszerver indítása, kliens, mintaadatok."""
import base64
import io

import _path  # noqa: F401
from ads_engine.google.auth import ServiceAccount, TokenProvider
from ads_engine.google.client import GoogleAdsClient
from ads_engine.google.discovery import Discovery
from mock_google_ads import MockGoogleAds

MCC = "1111111111"
CID = "2222222222"
_KEY = None


def shared_key():
    """Egy RSA-kulcs az összes próbához (a kulcsgenerálás lassú)."""
    global _KEY
    if _KEY is None:
        from cryptography.hazmat.primitives.asymmetric import rsa
        _KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return _KEY


def discovery():
    """A helyi v25 leíró (ha nincs, letölti; ha az sem megy, None – a séma-ellenőrzés kimarad)."""
    try:
        return Discovery.load_cached("v25") or Discovery.load("v25")
    except Exception:
        return None


def start_mock(grant_mcc=True):
    m = MockGoogleAds(discovery=discovery(), key=shared_key()).start()
    m.add_account(MCC, "ARworks MCC", manager=True, grant=grant_mcc)
    m.add_account(CID, "Pacsit", parent=MCC)
    return m


def make_client(mock, login=MCC, sleep=lambda s: None):
    sa = ServiceAccount(mock.service_account_info())
    tokens = TokenProvider(sa)
    return GoogleAdsClient(tokens, login_customer_id=login, base_url=mock.base_url, sleep=sleep)


def tmp(cid, kind, n):
    return f"customers/{cid}/{kind}/-{n}"


def tiny_png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 100, 50)).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def sample_tree(cid=CID, name="Pacsi | Kereső", label="ads-engine", with_label=True, eu=True, headlines=None, budget=1_000_000_000):
    """Egy teljes Search-kampányfa atomi mutate-hoz (a valódi építő ennek a mintájára készül)."""
    heads = headlines or ["Melyik kutya illik hozzád?", "Kutyafajta-választó kvíz", "Derítsd ki 1 perc alatt!"]
    ops = [{"campaignBudgetOperation": {"create": {"resourceName": tmp(cid, "campaignBudgets", 1), "name": f"{name} – keret",
                                                   "amountMicros": str(budget), "deliveryMethod": "STANDARD", "explicitlyShared": False}}}]
    if with_label:
        ops.append({"labelOperation": {"create": {"resourceName": tmp(cid, "labels", 2), "name": label,
                                                  "textLabel": {"backgroundColor": "#4285F4", "description": "Az Ads Engine kezeli"}}}})
    camp = {"resourceName": tmp(cid, "campaigns", 3), "name": name, "status": "PAUSED", "advertisingChannelType": "SEARCH",
            "campaignBudget": tmp(cid, "campaignBudgets", 1), "targetSpend": {"cpcBidCeilingMicros": "300000000"},
            "networkSettings": {"targetGoogleSearch": True, "targetSearchNetwork": False, "targetContentNetwork": False,
                                "targetPartnerSearchNetwork": False},
            "geoTargetTypeSetting": {"positiveGeoTargetType": "PRESENCE"},
            "finalUrlSuffix": "utm_source=google&utm_medium=cpc&utm_campaign=pacsi-kereso"}
    if eu:
        camp["containsEuPoliticalAdvertising"] = "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"
    ops.append({"campaignOperation": {"create": camp}})
    if with_label:
        ops.append({"campaignLabelOperation": {"create": {"campaign": tmp(cid, "campaigns", 3), "label": tmp(cid, "labels", 2)}}})
    ops += [
        {"campaignCriterionOperation": {"create": {"campaign": tmp(cid, "campaigns", 3), "location": {"geoTargetConstant": "geoTargetConstants/2348"}}}},
        {"campaignCriterionOperation": {"create": {"campaign": tmp(cid, "campaigns", 3), "language": {"languageConstant": "languageConstants/1024"}}}},
        {"adGroupOperation": {"create": {"resourceName": tmp(cid, "adGroups", 4), "name": "Kvíz", "campaign": tmp(cid, "campaigns", 3),
                                         "status": "ENABLED", "type": "SEARCH_STANDARD"}}},
        {"adGroupCriterionOperation": {"create": {"adGroup": tmp(cid, "adGroups", 4), "status": "ENABLED",
                                                  "keyword": {"text": "kutyafajta választó", "matchType": "PHRASE"}}}},
        {"adGroupAdOperation": {"create": {"adGroup": tmp(cid, "adGroups", 4), "status": "ENABLED",
                                           "ad": {"finalUrls": ["https://pacsit.hu/"],
                                                  "responsiveSearchAd": {"headlines": [{"text": h} for h in heads],
                                                                         "descriptions": [{"text": "Ingyenes kvíz, 124 fajta."},
                                                                                          {"text": "Regisztráció nélkül, magyarul."}],
                                                                         "path1": "kviz"}}}}},
        {"assetOperation": {"create": {"resourceName": tmp(cid, "assets", 5), "sitelinkAsset": {"linkText": "Kvíz", "description1": "10 kérdés", "description2": "1 perc"},
                                       "finalUrls": ["https://pacsit.hu/kviz"]}}},
        {"campaignAssetOperation": {"create": {"campaign": tmp(cid, "campaigns", 3), "asset": tmp(cid, "assets", 5), "fieldType": "SITELINK"}}},
        {"assetOperation": {"create": {"resourceName": tmp(cid, "assets", 6), "name": "kep-1", "imageAsset": {"data": tiny_png()}}}},
        {"campaignAssetOperation": {"create": {"campaign": tmp(cid, "campaigns", 3), "asset": tmp(cid, "assets", 6), "fieldType": "AD_IMAGE"}}},
    ]
    return ops
