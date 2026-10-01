"""Indítás: Ads Pack → kampányfa → szüneteltetett kampány a Google Ads-ben; majd a go-live (keret + bekapcsolás).

  plan     letölti és ellenőrzi a csomagot, felépíti a fát, kiírja az összegzést (a Google-be nem ír)
  launch   a fa egyetlen atomi mutate-tal: dry módban csak validateOnly, live módban SZÜNETELTETETT kampány jön létre
  go_live  a te jóváhagyásod: a valódi keret beállítása és a kampány bekapcsolása EGY műveletben
Az újrafuttatás nem duplikál: a kampányt név és `ads-engine` címke alapján ismeri fel.
"""
import dataclasses
import datetime as dt
import re
from zoneinfo import ZoneInfo

from . import builder, factory, images as imgs, net, pack as packmod, reportdata
from .executor import Executor, WriteRefused, enable_campaign_ops
from .google.client import GoogleAdsError
from .guardrails import ENGINE_LABEL, MICROS, round_budget_micros


class LaunchError(Exception):
    pass


def gaql_quote(s):
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def resolve_locale(client, cid, country, language):
    """A célország és nyelv Google-azonosítója (dinamikusan; tartalék: ismert értékek HU/hu-hoz)."""
    geo = lang = None
    try:
        rows = client.search(cid, "SELECT geo_target_constant.resource_name, geo_target_constant.country_code, geo_target_constant.target_type "
                                  f"FROM geo_target_constant WHERE geo_target_constant.country_code = {gaql_quote(country)} "
                                  "AND geo_target_constant.target_type = 'Country'")
        geo = rows[0]["geoTargetConstant"]["resourceName"] if rows else None
        rows = client.search(cid, "SELECT language_constant.resource_name, language_constant.code FROM language_constant "
                                  f"WHERE language_constant.code = {gaql_quote(language)}")
        lang = rows[0]["languageConstant"]["resourceName"] if rows else None
    except GoogleAdsError:
        pass
    fb = builder.LOCALE_FALLBACK.get((country, language))
    geo, lang = geo or (fb[0] if fb else None), lang or (fb[1] if fb else None)
    if not geo or not lang:
        raise LaunchError(f"Nem található Google-azonosító a(z) {country}/{language} célhoz.")
    return geo, lang


def existing_label(client, cid):
    rows = client.search(cid, f"SELECT label.resource_name, label.name FROM label WHERE label.name = {gaql_quote(ENGINE_LABEL)} AND label.status = 'ENABLED'")
    return rows[0]["label"]["resourceName"] if rows else None


def existing_campaign(client, cid, name):
    rows = client.search(cid, "SELECT campaign.id, campaign.name, campaign.status, campaign.resource_name, campaign.campaign_budget, "
                              "campaign.labels, campaign_budget.amount_micros FROM campaign "
                              f"WHERE campaign.name = {gaql_quote(name)} AND campaign.status != 'REMOVED'")
    return rows[0] if rows else None


@dataclasses.dataclass
class PlanResult:
    summary: dict
    warnings: list
    skipped_images: list
    tree: builder.Tree
    pack: packmod.Pack


def prepare_images(pack, project, fetch=net.fetch, **fetch_kw):
    out, problems, seen = [], [], set()
    for ref in pack.creatives.get("images", []):
        try:
            data, url = packmod.fetch_image(pack, project, ref["file"], fetch=fetch, **fetch_kw)
            im = imgs.open_image(data)
            kind = imgs.kind_for(ref.get("ratio"), im.width, im.height)
            p = imgs.prepare(data, kind)
        except (net.FetchError, imgs.ImageError) as e:
            problems.append(f"kép kihagyva ({ref['id']}): {e}")
            continue
        if p.sha256 in seen:
            continue
        seen.add(p.sha256)
        out.append(p)
    return out, problems


def make_plan(settings, project, store, *, client=None, fetch=net.fetch, **fetch_kw):
    """Csomag letöltése + ellenőrzése + fa. A Google-t csak olvasásra használja (címke, helyi azonosítók), ha van kliens."""
    pk = packmod.load(project, store, fetch=fetch, **fetch_kw)
    images, problems = prepare_images(pk, project, fetch=fetch, **fetch_kw)
    cid = project.customer_id or "0000000000"
    label_rn = geo = lang = None
    if client is not None and project.customer_id:
        label_rn = existing_label(client, cid)
        geo, lang = resolve_locale(client, cid, pk.brief["project"]["country"], pk.brief["project"]["language"])
    else:
        fb = builder.LOCALE_FALLBACK.get((pk.brief["project"]["country"], pk.brief["project"]["language"]))
        if not fb:
            raise LaunchError("Google-kapcsolat nélkül csak HU/hu célhoz van beépített azonosító; add meg a Google-fiókot a pontos tervhez.")
        geo, lang = fb
    tree = builder.build_search_tree(project, pk, customer_id=cid, label_rn=label_rn, geo_rn=geo, language_rn=lang, images=images,
                                     currency=project.currency)
    return PlanResult(tree.summary, pk.warnings, problems, tree, pk)


def error_indexes(e):
    """A Google hibájában szereplő művelet-indexek (mutate_operations[N]): megmondja, melyik művelet hibás."""
    out = set()
    for err in getattr(e, "errors", []) or []:
        out.update(int(m) for m in re.findall(r"mutate_operations\[(\d+)\]", err.get("path", "") or ""))
    return out


def existing_image_assets(client, cid, names):
    """A már feltöltött kép-eszközök (név → erőforrásnév): a tartalom-hash névből tudjuk, hogy ugyanaz a kép."""
    if not names:
        return {}
    quoted = ", ".join(gaql_quote(n) for n in names)
    rows = client.search(cid, f"SELECT asset.resource_name, asset.name FROM asset WHERE asset.type = 'IMAGE' AND asset.name IN ({quoted})")
    return {r["asset"]["name"]: r["asset"]["resourceName"] for r in rows}


def launch(settings, project, store, client, run_id, *, fetch=net.fetch, **fetch_kw):
    """Szüneteltetett kampány létrehozása (live) vagy próbája (dry). Visszatér: {status, summary, ...}.

    Dry módban a teljes fa (képekkel együtt) egy validateOnly kérésben ellenőrződik. Live módban a kampány képek nélkül jön létre,
    a képek külön kérésben követik: egy kép hibája így nem viszi magával a kampányt."""
    if not project.customer_id:
        raise LaunchError("A projekthez még nincs Google Ads ügyfélfiók (customer_id a config/projects.toml-ban).")
    name = builder.campaign_name(project)
    found = existing_campaign(client, project.customer_id, name)
    if found:
        return {"status": "exists", "campaign": found["campaign"].get("resourceName"), "campaign_status": found["campaign"].get("status"),
                "message": f"A(z) „{name}” kampány már létezik ({found['campaign'].get('status')}); a motor nem duplikál."}
    plan = make_plan(settings, project, store, client=client, fetch=fetch, **fetch_kw)
    tree, warnings = plan.tree, plan.warnings + plan.skipped_images
    ex = Executor(client, store, settings, project, run_id)
    after = {"summary": plan.summary, "pack": plan.pack.content_hash}
    if not settings.live:
        try:
            ex.apply("launch", tree.ops, target=name, reason="induló csomag (próba)", before=None, after=after)
        except GoogleAdsError as e:
            idx = error_indexes(e)
            if tree.core_len < len(tree.ops) and idx and min(idx) >= tree.core_len:      # csak a képek hibásak (pl. új fiók: kép-bővítmény még nem engedélyezett)
                ex.apply("launch", tree.core_ops, target=name, reason="induló csomag (próba, képek nélkül)", before=None, after=after)
                warnings.append(f"A képek próbája nem sikerült, a kampány képek nélkül rendben: {str(e)[:200]}. {factory.ELIGIBILITY_HINT}")
                return {"status": "validated", "summary": plan.summary, "warnings": warnings}
            raise
        return {"status": "validated", "summary": plan.summary, "warnings": warnings}
    resp = ex.apply("launch", tree.core_ops, target=name, reason="induló csomag", before=None, after=after)
    camp_rn = resp["mutateOperationResponses"][tree.campaign_index]["campaignResult"]["resourceName"]
    images_added = 0
    images = [op for op in tree.ops[tree.core_len:] if "assetOperation" in op]
    if images:
        try:
            prepared = prepare_images(plan.pack, project, fetch=fetch, **fetch_kw)[0]
            existing = existing_image_assets(client, project.customer_id, [im.name for im in prepared])
            img_ops = builder.build_image_ops(project.customer_id, camp_rn, prepared, existing)
            ex.apply("launch_images", img_ops, target=name, reason="induló képek", after={"images": len(prepared)})
            images_added = len(prepared)
            reg = factory.Registry(store, settings, project)
            for p in prepared:
                reg.add(p, "pack", {"launch": True}, "uploaded")                              # a projekt saját képe: védett, a heti kör nem tölti fel újra
        except (GoogleAdsError, net.FetchError, imgs.ImageError) as e:
            warnings.append(f"A képek feltöltése nem sikerült (a kampány létrejött, a képeket a heti kör később pótolja): {e}" +
                            (f" {factory.ELIGIBILITY_HINT}" if isinstance(e, GoogleAdsError) else ""))
    store.put(f"{project.slug}.launch", {"campaign": name, "campaign_rn": camp_rn, "pack": plan.pack.content_hash,
                                         "summary": plan.summary, "images_added": images_added})
    return {"status": "created", "campaign": camp_rn, "summary": plan.summary, "warnings": warnings, "images_added": images_added}


def go_live(settings, project, store, client, run_id, weekly_budget, *, today=None):
    """A te jóváhagyásod: valódi napi keret (heti/7) + a kampány bekapcsolása egyetlen atomi művelettel."""
    today = today or dt.datetime.now(ZoneInfo(project.timezone)).date()
    if weekly_budget <= 0:
        raise LaunchError("A heti keret legyen pozitív szám.")
    if not settings.live:
        raise LaunchError("Dry üzemmódban vagyunk (ENGINE_MODE=dry): az élesítéshez ENGINE_MODE=live kell.")
    name = builder.campaign_name(project)
    found = existing_campaign(client, project.customer_id, name)
    if not found:
        raise LaunchError(f"Nincs „{name}” kampány: előbb futtasd a launch parancsot.")
    camp = found["campaign"]
    daily = round_budget_micros(weekly_budget * MICROS / 7, project.currency)         # a heti keret 1/7-e, a pénznem legkisebb egységére kerekítve
    budget_rn = camp["campaignBudget"]
    ops = [{"campaignBudgetOperation": {"update": {"resourceName": budget_rn, "amountMicros": str(daily)}, "updateMask": "amountMicros"}},
           {"campaignOperation": {"update": {"resourceName": camp["resourceName"], "status": "ENABLED"}, "updateMask": "status"}}]
    before = {"status": camp.get("status"), "daily_budget_micros": found.get("campaignBudget", {}).get("amountMicros")}
    ex = Executor(client, store, settings, project, run_id)
    ex.apply("go_live", ops, target=name, reason=f"go-live: heti keret {weekly_budget:,.0f} {project.currency}".replace(",", " "),
             before=before, after={"status": "ENABLED", "daily_budget_micros": str(daily)})
    store.put(f"{project.slug}.approved_daily_micros", daily)
    store.put(f"{project.slug}.go_live", {"weekly_budget": weekly_budget, "daily_micros": daily, "date": today.isoformat()})
    return {"status": "enabled", "daily_micros": daily, "weekly_budget": weekly_budget, "campaign": camp["resourceName"]}


def confirm_budget(settings, project, store, client, run_id, *, enable=False):
    """A fék (gyanús keretemelés, túlköltés) utáni megerősítés: a Google Ads-ben látható keret lesz a jóváhagyott, és kérésre a fék
    által szüneteltetett kampányok újra bekapcsolódnak. A keretet itt NEM emeli a motor: csak elfogadja, amit te állítottál be."""
    cid = project.customer_id
    campaigns = reportdata.owned_campaigns(client, cid)
    if not campaigns:
        raise LaunchError("Nincs motor-kampány a fiókban.")
    live_daily = sum(c["daily_micros"] for c in campaigns)
    old = store.get(f"{project.slug}.approved_daily_micros")
    pause = store.get(f"{project.slug}.guard_pause") or {}
    held = [c["resource_name"] for c in campaigns if c["resource_name"] in pause.get("campaigns", []) and c["status"] == "PAUSED"]
    if enable and not settings.live:
        raise LaunchError("Dry üzemmódban vagyunk (ENGINE_MODE=dry): a visszakapcsoláshoz ENGINE_MODE=live kell.")
    enabled = []
    if enable and held:
        Executor(client, store, settings, project, run_id).apply(
            "confirm_enable", enable_campaign_ops(held), target=",".join(h.rsplit("/", 1)[-1] for h in held),
            reason="a keret megerősítve: a fék által szüneteltetett kampányok visszakapcsolva", before={"status": "PAUSED"}, after={"status": "ENABLED"})
        enabled = held
    store.put(f"{project.slug}.approved_daily_micros", live_daily)
    store.delete(f"{project.slug}.needs_budget_confirmation")
    store.delete(f"{project.slug}.guard_pause")
    store.put(f"{project.slug}.snapshot_stale", True)
    store.log_action(run_id, project.slug, "confirm_budget", "napi keret", {"approved_daily_micros": old}, {"approved_daily_micros": live_daily},
                     "applied", "a felhasználó megerősítette a keretet", settings.mode)
    return {"approved_daily_micros": live_daily, "previous": old, "enabled": enabled, "held": held}
