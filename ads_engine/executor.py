"""Végrehajtó: az EGYETLEN kapu a Google Ads felé írásra.

Minden írás: (1) validateOnly próba, (2) éles végrehajtás csak `live` üzemmódban (vagy biztonsági fékként), (3) napló
előtte/utána értékkel. Dry üzemmódban csak a próba fut, így a hibák akkor is kiderülnek, amikor semmi nem íródik.
A fékek (kampány szüneteltetése) mindig élesek: csak csökkenthetnek. A `STOP` fájl minden más írást megállít.
"""
from . import log
from .google.client import GoogleAdsError


class WriteRefused(Exception):
    pass


def pause_campaign_ops(resource_names):
    return [{"campaignOperation": {"update": {"resourceName": rn, "status": "PAUSED"}, "updateMask": "status"}} for rn in resource_names]


def enable_campaign_ops(resource_names):
    return [{"campaignOperation": {"update": {"resourceName": rn, "status": "ENABLED"}, "updateMask": "status"}} for rn in resource_names]


class Executor:
    def __init__(self, client, store, settings, project, run_id):
        self.client, self.store, self.settings, self.project, self.run_id = client, store, settings, project, run_id

    @property
    def stop_requested(self):
        return (self.settings.data_dir / "STOP").exists()

    def apply(self, kind, ops, *, target="", reason="", before=None, after=None, safety=False):
        """Műveletek (egy atomi mutate) végrehajtása a szabályok szerint. Visszatér a Google válaszával (vagy dry módban {})."""
        if not ops:
            return {}
        live = self.settings.live or safety
        log_kw = dict(run_id=self.run_id, project=self.project.slug, kind=kind, target=target, before=before, after=after, reason=reason)
        if self.stop_requested and not safety:
            self.store.log_action(status="blocked_stop", mode="stop", **log_kw)
            raise WriteRefused("A STOP fájl miatt a motor most nem ír (csak biztonsági fékek futnak).")
        cid = self.project.customer_id
        if not cid:
            raise WriteRefused("A projekthez nincs Google Ads ügyfélfiók (customer_id) megadva.")
        try:
            self.client.mutate(cid, ops, validate_only=True)
        except GoogleAdsError as e:
            self.store.log_action(status="rejected", mode="validate", request_id=e.request_id, **{**log_kw, "reason": f"{reason} | {e}"[:900]})
            raise
        if not live:
            self.store.log_action(status="validated", mode="dry", request_id=self.client.last_request_id, **log_kw)
            log.info("executor.validated", kind=kind, target=target)
            return {}
        try:
            resp = self.client.mutate(cid, ops)
        except GoogleAdsError as e:
            self.store.log_action(status="failed", mode="live", request_id=e.request_id, **{**log_kw, "reason": f"{reason} | {e}"[:900]})
            raise
        self.store.put(f"{self.project.slug}.snapshot_stale", True)       # a saját írásunkat a következő szinkron ne vegye emberi módosításnak
        self.store.log_action(status="applied", mode="live" if not safety else "safety", request_id=self.client.last_request_id, **log_kw)
        log.info("executor.applied", kind=kind, target=target, safety=safety)
        return resp

    def pause_campaigns(self, resource_names, reason):
        """Biztonsági fék: a megadott kampányok szüneteltetése (dry módban is éles, mert csak csökkent)."""
        names = list(resource_names)
        return self.apply("pause_campaigns", pause_campaign_ops(names), target=",".join(n.rsplit("/", 1)[-1] for n in names),
                          reason=reason, before={"status": "ENABLED"}, after={"status": "PAUSED"}, safety=True)
