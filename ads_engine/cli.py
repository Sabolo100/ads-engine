"""Parancssor: python -m ads_engine <parancs>

Beállítás és indítás:
  check [--project pacsi] [--offline]   beállítás-ellenőrzés; kiírja a KÖVETKEZŐ HIÁNYZÓ LÉPÉST (kilépési kód: 1, ha van)
  plan [--project pacsi]                az Ads Pack letöltése és ellenőrzése, a kampányfa összegzése (nem ír)
  launch [--project pacsi] [--yes]      szüneteltetett kampány létrehozása (dry: csak validateOnly; --yes nélkül csak terv)
  go-live --weekly-budget N --yes       a valódi heti keret beállítása és a kampány bekapcsolása (csak ENGINE_MODE=live)
Üzem (ezeket az ütemező magától futtatja; kézzel is indíthatók):
  serve                                 a szolgáltatás: ütemező + /healthz (a Docker-konténer fő parancsa)
  tick                                  az esedékes feladatok egyszeri futtatása
  sync [--project pacsi]                napi szinkron és védelmek azonnal (kampány-fékek, brief-figyelő, nyitóoldal)
  weekly [--project pacsi] [--mail] [--no-ai] [--yes]   heti kiértékelés azonnal; élesben módosít (ehhez --yes kell)
  monthly [--project pacsi] [--mail] [--yes]            a havi terv azonnal (élesben új kulcsszavakat ír, ehhez --yes kell)
  report [--project pacsi]              az utolsó heti jelentés kiírása
  confirm-budget --yes [--enable]       a Google Ads-ben látható keret elfogadása fék után; --enable: a szüneteltetett kampányok visszakapcsolása
  status [--project pacsi]              üzemmód, utolsó futások, jóváhagyott keret, zár, STOP
  stop | resume                         a STOP fájl létrehozása/törlése (a motor ilyenkor semmit nem ír, csak a fékek futnak)
  healthcheck                           a helyi /healthz lekérdezése (Docker HEALTHCHECK)
  api-check [--api-version v25]         a Google API leíró frissítése, lejárat, újabb verzió
  --version                             verzió és build-azonosító

A Google Ads-be író parancsok `--yes` nélkül nem írnak (a launch és a weekly élesben megerősítést kér).
"""
import argparse
import os
import socket
import sys

from . import __version__, checks, config, guardrails, http, label, launch, log, monthly, pack as packmod, reports, review, runtime, scheduler, sync
from .google import discovery
from .google.auth import AuthError
from .google.client import GoogleAdsError
from .store import LeaseBusy


def _out(text=""):
    print(text, flush=True)


def cmd_check(args, settings):
    project = settings.project(args.project)
    try:
        client = None if args.offline else runtime.google_client(settings)
        sa = runtime.service_account(settings)
    except AuthError as e:
        _out(f" ✗ A Google szolgáltatásfiók-kulcs hibás: {e}")
        return 1
    results = checks.run_checks(settings, project, client=client, sa=sa, offline=args.offline)
    _out(f"Ads Engine {label()} · projekt: {project.name} ({project.slug})\n")
    _out(checks.render(results))
    return 1 if checks.next_step(results) else 0


def cmd_status(args, settings):
    project = settings.project(args.project)
    store = runtime.open_store(settings)
    _out(f"Ads Engine {label()} · projekt: {project.name} ({project.slug})")
    _out(f"Üzemmód: {settings.mode} · adatmappa: {settings.data_dir}")
    stop = (settings.data_dir / "STOP").exists()
    _out(f"STOP fájl: {'VAN – a motor nem ír' if stop else 'nincs'}")
    holder = store.lease_holder("engine")
    _out(f"Zár: {holder or 'szabad'}")
    slug = project.slug
    approved = store.get(f"{slug}.approved_daily_micros")
    if approved:
        _out(f"Jóváhagyott keret: {approved / 1_000_000:,.0f} {project.currency}/nap ({approved * 7 / 1_000_000:,.0f} /hét)".replace(",", " "))
        go = store.get(f"{slug}.go_live") or {}
        if go.get("date"):
            _out(f"Éles indulás: {go['date']}")
    else:
        _out("Jóváhagyott keret: nincs (a go-live előtt a kampányok szüneteltetve maradnak)")
    if store.get(f"{slug}.needs_budget_confirmation"):
        _out("⚠ A kampány fék miatt szünetel, megerősítésre vár: python -m ads_engine confirm-budget --yes --enable")
    elif store.get(f"{slug}.guard_pause"):
        _out("⚠ A kampány fék miatt szünetel (túlköltés): nézd meg a levelet; ha rendben: python -m ads_engine confirm-budget --yes --enable")
    hands = store.get(f"{slug}.hands_off", {}) or {}
    if hands:
        _out(f"Kézben lévő objektum (ember módosította, a motor nem nyúl hozzá): {len(hands)}")
    ls = store.get(f"{slug}.last_sync")
    _out(f"Utolsó napi szinkron: {ls['date'] if ls else 'még nem volt'}")
    lm = store.get(f"{slug}.last_monthly")
    _out(f"Utolsó havi terv: {lm['month'] + (' (levélben elment)' if lm.get('mailed') else ' (a levél NEM ment el)') if lm else 'még nem volt'}")
    used = store.counter(slug, "ai_images", guardrails.iso_week(sync.now_in(project).date()))
    _out(f"AI-képek ebben a hétben: {used} / {project.max_images_per_week}")
    lr = store.get(f"{slug}.last_report")
    _out(f"Utolsó heti jelentés: {lr['period_end'] + (' (levélben elment)' if lr.get('mailed') else ' (a levél NEM ment el)') if lr else 'még nem volt'}")
    runs = [r for r in store.runs(slug, limit=60) if r["kind"] != "guard"][:6]
    _out("Utolsó futások:" if runs else "Még nem volt futás.")
    for r in runs:
        _out(f"  #{r['id']} {r['started_at']} {r['kind']:<12} {r['status']:<8} ({r['mode']})")
    store.close()
    return 0


def _google_or_none(settings):
    try:
        client = runtime.google_client(settings)
    except AuthError as e:
        _out(f"✗ {e}")
        return None
    if client is None:
        _out("✗ Nincs Google szolgáltatásfiók-kulcs (GADS_SA_JSON_B64) – futtasd: python -m ads_engine check")
    return client


def _locked(settings, fn):
    """A parancs a zár alatt fut (egyszerre egy példány írhat). Visszaad: (kód, eredmény)."""
    store = runtime.open_store(settings)
    try:
        with store.lease("engine", _owner()):
            return fn(store)
    except LeaseBusy as e:
        _out(f"✗ {e} – várd meg, míg az ütemező befejezi, vagy próbáld később.")
        return 1
    finally:
        store.close()


def cmd_sync(args, settings):
    project = settings.project(args.project)
    client = _google_or_none(settings)
    if client is None:
        return 1

    def run(store):
        run_id = store.start_run(project.slug, "sync", settings.mode)
        try:
            summary = scheduler.job_sync(settings, store, project, run_id, sync.now_in(project),
                                         scheduler.Deps(client=client, fetch_kw=_fetch_kwargs(settings)))
        except (GoogleAdsError, AuthError) as e:
            store.finish_run(run_id, "failed", {"error": str(e)})
            _out(f"✗ {e}")
            return 1
        store.finish_run(run_id, "ok", summary)
        _out(f"Szinkron kész: {summary['campaigns']} kampány · fékek: {len(summary['findings'])} megállapítás, {summary['paused']} kampány szüneteltetve · "
             f"kézi módosítás: {summary['human_changes']} · elutasított hirdetés: {summary['disapproved']} · csomag: {summary['pack']}")
        for f in summary["findings"]:
            _out(f"  ! {f}")
        return 0
    return _locked(settings, run)


def cmd_weekly(args, settings):
    project = settings.project(args.project)
    if settings.live and not args.yes:
        _out("A heti kör ÉLES üzemmódban valódi módosításokat végez a Google Ads-ben (negatív kulcsszó, szüneteltetés, hirdetésszöveg-csere).")
        _out("Megerősítéshez: python -m ads_engine weekly --yes   (ENGINE_MODE=dry mellett nincs írás, csak próba)")
        return 1
    client = _google_or_none(settings)
    if client is None:
        return 1

    def run(store):
        run_id = store.start_run(project.slug, "weekly-manual", settings.mode)
        llm = None if args.no_ai else runtime.llm(settings, store)
        try:
            res = review.run_weekly(settings, project, store, client, run_id, llm=llm, umami_client=runtime.umami(settings),
                                    openai=None if args.no_ai else runtime.openai(settings), now=sync.now_in(project),
                                    **_fetch_kwargs(settings))
        except (GoogleAdsError, AuthError) as e:
            store.finish_run(run_id, "failed", {"error": str(e)})
            _out(f"✗ {e}")
            return 1
        narrative = reports.make_narrative(llm, project.slug, res.report)
        if args.mail:
            out = reports.deliver(settings, project, store, res.report, narrative)
            text = out["text"]
        else:
            text = reports.render_text(res.report, narrative)
            out = {"path": str(reports.save(settings, res.report, text, reports.render_html(res.report, narrative), narrative)), "mailed": False, "mail_error": ""}
        store.finish_run(run_id, "ok", {"actions": len(res.report["actions"]), "rejected": len(res.report["rejected"]), "report": out["path"]})
        _out(text)
        _out(f"(a jelentés elmentve: {out['path']})")
        if args.mail:
            _out("A levél elment." if out["mailed"] else f"A levél NEM ment el: {out['mail_error']}")
        return 0
    return _locked(settings, run)


def cmd_monthly(args, settings):
    project = settings.project(args.project)
    if settings.live and not args.yes:
        _out("A havi terv ÉLES üzemmódban új kulcsszavakat ír a Google Ads-be (a szabályokon átengedettet, havonta legfeljebb tízet).")
        _out("Megerősítéshez: python -m ads_engine monthly --yes   (ENGINE_MODE=dry mellett nincs írás, csak próba)")
        return 1
    client = _google_or_none(settings)
    if client is None:
        return 1

    def run(store):
        run_id = store.start_run(project.slug, "monthly-manual", settings.mode)
        llm = runtime.llm(settings, store)
        try:
            report, _actions = monthly.run_monthly(settings, project, store, client, run_id, llm=llm, now=sync.now_in(project), **_fetch_kwargs(settings))
        except (GoogleAdsError, AuthError) as e:
            store.finish_run(run_id, "failed", {"error": str(e)})
            _out(f"✗ {e}")
            return 1
        if report.get("skipped"):
            store.finish_run(run_id, "ok", {"skipped": report["skipped"]})
            _out("A havi terv nem készült el: " + " ".join(report["notes"]))
            return 1
        _out(reports.render_monthly_text(report))
        store.finish_run(run_id, "ok", {"theme": report.get("theme"), "keywords_added": len(report["keywords"]["added"]), "path": report.get("path")})
        if args.mail:
            out = reports.deliver_monthly(settings, project, store, report)
            _out("A levél elment." if out["mailed"] else f"A levél NEM ment el: {out['mail_error']}")
        return 0
    return _locked(settings, run)


def cmd_report(args, settings):
    project = settings.project(args.project)
    store = runtime.open_store(settings)
    try:
        last = store.get(f"{project.slug}.last_report")
    finally:
        store.close()
    if not last:
        _out("Még nem készült heti jelentés (python -m ads_engine weekly).")
        return 1
    try:
        report, narrative = reports.load_saved(last["path"])
    except (OSError, ValueError) as e:
        _out(f"✗ A mentett jelentés nem olvasható ({last['path']}): {e}")
        return 1
    _out(reports.render_text(report, narrative))
    return 0


def cmd_confirm_budget(args, settings):
    project = settings.project(args.project)
    if not args.yes:
        _out("A megerősítés a Google Ads-ben JELENLEG beállított keretet fogadja el jóváhagyottként (a motor a keretet soha nem emeli maga).")
        _out("Ellenőrizd a keretet a Google Ads-ben, majd: python -m ads_engine confirm-budget --yes [--enable]")
        _out("  --enable: a fék által szüneteltetett kampányokat is visszakapcsolja (ENGINE_MODE=live kell)")
        return 1
    client = _google_or_none(settings)
    if client is None:
        return 1

    def run(store):
        run_id = store.start_run(project.slug, "confirm-budget", settings.mode)
        try:
            res = launch.confirm_budget(settings, project, store, client, run_id, enable=args.enable)
        except (launch.LaunchError, GoogleAdsError) as e:
            store.finish_run(run_id, "failed", {"error": str(e)})
            _out(f"✗ {e}")
            return 1
        store.finish_run(run_id, "ok", res)
        _out(f"Megerősítve: jóváhagyott napi keret {_money(res['approved_daily_micros'], project.currency)} (heti {_money(res['approved_daily_micros'] * 7, project.currency)}).")
        if res["enabled"]:
            _out(f"Visszakapcsolva: {len(res['enabled'])} kampány.")
        elif res["held"]:
            _out(f"{len(res['held'])} kampány még szünetel (a fék tette): a visszakapcsoláshoz add meg az --enable kapcsolót (ENGINE_MODE=live).")
        return 0
    return _locked(settings, run)


def cmd_tick(args, settings):
    store = runtime.open_store(settings)
    try:
        done = scheduler.tick(settings, store)
    except LeaseBusy as e:
        _out(f"✗ {e}")
        return 1
    finally:
        store.close()
    if not done:
        _out("Nincs esedékes feladat.")
    for d in done:
        _out(f"{d['kind']:<7} {d['period']}  {d['status']}" + (f"  – {d['error']}" if d["status"] == "failed" else ""))
    return 1 if any(d["status"] == "failed" for d in done) else 0


def cmd_serve(args, settings):
    return scheduler.serve(settings)


def cmd_healthcheck(args, settings):
    port = int(settings.env.get("PORT") or 8080)
    try:
        r = http.request("GET", f"http://127.0.0.1:{port}/healthz", timeout=5, retries=0)
    except http.HttpError as e:
        _out(f"✗ /healthz: HTTP {e.status}")
        return 1
    _out(f"ok ({r.status})")
    return 0


def cmd_stop(args, settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "STOP").write_text("A motor leállítva a stop paranccsal. Törlés: python -m ads_engine resume\n", encoding="utf-8")
    _out("STOP fájl létrehozva: a motor mostantól semmit nem ír a Google Ads-be (csak a biztonsági fékek futnak).")
    return 0


def cmd_resume(args, settings):
    f = settings.data_dir / "STOP"
    if f.exists():
        f.unlink()
        _out("STOP fájl törölve: a motor újra dolgozhat.")
    else:
        _out("Nincs STOP fájl.")
    return 0


def cmd_api_check(args, settings):
    res = discovery.api_check(args.api_version or settings.api_version, refresh=True)
    _out(f"Google Ads API {res['version']} · leíró revízió: {res.get('revision', '?')} · hálózat: {'ok' if res['ok'] else 'nem elérhető'}")
    if "sunset" in res:
        _out(f"Lejárat: {res['sunset']} ({res['days_left']} nap)")
    for n in res["notes"]:
        _out(f" ! {n}")
    if not res["notes"]:
        _out("Nincs teendő.")
    return 0 if res["ok"] else 1


def _owner():
    return f"{socket.gethostname()}-{os.getpid()}"


def _money(micros_value, currency):
    return f"{micros_value / 1_000_000:,.0f} {currency}".replace(",", " ")


def _print_plan(plan, project):
    s = plan.summary
    _out(f"Kampány: {s['campaign']}  (SZÜNETELVE jön létre)")
    _out(f"  hirdetéscsoport: {s['ad_groups']} · kulcsszó: {s['keywords']} · negatív: {s['negatives']} · hirdetés: {s['ads']} · "
         f"hivatkozás: {s['sitelinks']} · kiemelés: {s['callouts']} · kép: {s['images']} · művelet: {s['operations']}")
    _out(f"  helyőrző napi keret: {_money(s['daily_budget_micros'], project.currency)} (a valódi keretet a go-live állítja) · "
         f"CPC-plafon: {_money(s['max_cpc_micros'], project.currency)}")
    _out(f"  csomag: {plan.pack.content_hash}")
    for w in plan.warnings + plan.skipped_images:
        _out(f" ! {w}")


_fetch_kwargs = runtime.fetch_kwargs


def _print_pack_error(e):
    _out(f"Az Ads Pack nem használható ({len(e.problems)} hiba):")
    for p in e.problems:
        _out(f"  ✗ {p}")


def cmd_plan(args, settings):
    project = settings.project(args.project)
    store = runtime.open_store(settings)
    try:
        client = runtime.google_client(settings) if project.customer_id else None
        plan = launch.make_plan(settings, project, store, client=client, **_fetch_kwargs(settings))
    except packmod.PackError as e:
        _print_pack_error(e)
        return 1
    except (launch.LaunchError, AuthError, GoogleAdsError) as e:
        _out(f"✗ {e}")
        return 1
    finally:
        store.close()
    _print_plan(plan, project)
    return 0


def cmd_launch(args, settings):
    project = settings.project(args.project)
    if not args.yes:
        _out("(--yes nélkül csak a terv készül; a Google-be semmi nem megy)\n")
        return cmd_plan(args, settings)
    try:
        client = runtime.google_client(settings)
    except AuthError as e:
        _out(f"✗ {e}")
        return 1
    if client is None:
        _out("✗ Nincs Google szolgáltatásfiók-kulcs (GADS_SA_JSON_B64) – futtasd: python -m ads_engine check")
        return 1
    store = runtime.open_store(settings)
    try:
        with store.lease("engine", _owner()):
            run_id = store.start_run(project.slug, "launch", settings.mode)
            try:
                res = launch.launch(settings, project, store, client, run_id, **_fetch_kwargs(settings))
            except (packmod.PackError, launch.LaunchError, GoogleAdsError) as e:
                store.finish_run(run_id, "failed", {"error": str(e)})
                if isinstance(e, packmod.PackError):
                    _print_pack_error(e)
                else:
                    _out(f"✗ {e}")
                return 1
            store.finish_run(run_id, "ok", {k: v for k, v in res.items() if k != "summary"})
    except LeaseBusy as e:
        _out(f"✗ {e}")
        return 1
    finally:
        store.close()
    if res["status"] == "exists":
        _out(res["message"])
    elif res["status"] == "validated":
        _out("Próba rendben (dry üzemmód): a Google elfogadta a teljes kampányfát, de semmi nem jött létre.")
        _out("Éles létrehozás: ENGINE_MODE=live mellett ugyanez a parancs (a kampány SZÜNETELVE jön létre).")
    else:
        _out(f"A kampány létrejött, SZÜNETELVE: {res['campaign']} (képek: {res['images_added']})")
        _out("A Google Ads-ben megnézheted. Bekapcsolás és keret: python -m ads_engine go-live --weekly-budget <összeg> --yes")
    for w in res.get("warnings", []):
        _out(f" ! {w}")
    return 0


def cmd_go_live(args, settings):
    project = settings.project(args.project)
    if not args.yes:
        _out("A go-live bekapcsolja a kampányt és beállítja a keretet: --yes kell hozzá. Példa:")
        _out(f"  python -m ads_engine go-live --weekly-budget 10000 --yes   (heti 10 000 {project.currency} = napi {10000 / 7:,.0f})")
        return 1
    try:
        client = runtime.google_client(settings)
    except AuthError as e:
        _out(f"✗ {e}")
        return 1
    if client is None:
        _out("✗ Nincs Google szolgáltatásfiók-kulcs (GADS_SA_JSON_B64).")
        return 1
    store = runtime.open_store(settings)
    try:
        with store.lease("engine", _owner()):
            run_id = store.start_run(project.slug, "go-live", settings.mode)
            try:
                res = launch.go_live(settings, project, store, client, run_id, args.weekly_budget)
            except (launch.LaunchError, GoogleAdsError) as e:
                store.finish_run(run_id, "failed", {"error": str(e)})
                _out(f"✗ {e}")
                return 1
            store.finish_run(run_id, "ok", res)
    except LeaseBusy as e:
        _out(f"✗ {e}")
        return 1
    finally:
        store.close()
    _out(f"Élesítve: a kampány BEKAPCSOLVA. Napi keret: {_money(res['daily_micros'], project.currency)} "
         f"(heti {res['weekly_budget']:,.0f} {project.currency}).".replace(",", " "))
    _out("A keretet a Google Ads-ben bármikor szerkesztheted; a motor átveszi (kétszeres vagy nagyobb emelésnél megerősítést kér).")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="ads_engine", description="Ads Engine – önjáró Google Ads kezelő")
    p.add_argument("--version", action="store_true", help="verzió és build-azonosító")
    sub = p.add_subparsers(dest="cmd")
    c = sub.add_parser("check", help="beállítás-ellenőrzés, kiírja a következő hiányzó lépést")
    c.add_argument("--project")
    c.add_argument("--offline", action="store_true", help="csak a helyi ellenőrzések (hálózat nélkül)")
    s = sub.add_parser("status", help="üzemmód, futások, keret")
    s.add_argument("--project")
    a = sub.add_parser("api-check", help="API leíró frissítése, lejárat, újabb verzió")
    a.add_argument("--api-version")
    pl = sub.add_parser("plan", help="az Ads Pack letöltése, ellenőrzése, a kampányfa összegzése (nem ír)")
    pl.add_argument("--project")
    la = sub.add_parser("launch", help="szüneteltetett kampány létrehozása (--yes nélkül csak terv)")
    la.add_argument("--project")
    la.add_argument("--yes", action="store_true")
    gl = sub.add_parser("go-live", help="a valódi heti keret beállítása és a kampány bekapcsolása")
    gl.add_argument("--project")
    gl.add_argument("--weekly-budget", type=float, required=True, help="heti keret a fiók pénznemében")
    gl.add_argument("--yes", action="store_true")
    sy = sub.add_parser("sync", help="napi szinkron és védelmek azonnal")
    sy.add_argument("--project")
    wk = sub.add_parser("weekly", help="heti kiértékelés azonnal (élesben módosít: --yes kell)")
    wk.add_argument("--project")
    wk.add_argument("--mail", action="store_true", help="a jelentést el is küldi levélben")
    wk.add_argument("--no-ai", action="store_true", help="AI nélkül (keresési kifejezések elemzése és szövegcsere nélkül)")
    wk.add_argument("--yes", action="store_true")
    mo = sub.add_parser("monthly", help="a havi terv azonnal (élesben új kulcsszavakat ír: --yes kell)")
    mo.add_argument("--project")
    mo.add_argument("--mail", action="store_true", help="a tervet el is küldi levélben")
    mo.add_argument("--yes", action="store_true")
    rp = sub.add_parser("report", help="az utolsó heti jelentés kiírása")
    rp.add_argument("--project")
    cb = sub.add_parser("confirm-budget", help="a Google Ads-ben látható keret elfogadása fék után")
    cb.add_argument("--project")
    cb.add_argument("--yes", action="store_true")
    cb.add_argument("--enable", action="store_true", help="a fék által szüneteltetett kampányok visszakapcsolása")
    sub.add_parser("tick", help="az esedékes feladatok egyszeri futtatása")
    sub.add_parser("serve", help="a szolgáltatás: ütemező + /healthz")
    sub.add_parser("healthcheck", help="a helyi /healthz lekérdezése (Docker)")
    sub.add_parser("stop", help="STOP fájl létrehozása: a motor nem ír")
    sub.add_parser("resume", help="STOP fájl törlése")
    return p


COMMANDS = {"check": cmd_check, "status": cmd_status, "api-check": cmd_api_check, "plan": cmd_plan, "launch": cmd_launch,
            "go-live": cmd_go_live, "sync": cmd_sync, "weekly": cmd_weekly, "monthly": cmd_monthly, "report": cmd_report, "confirm-budget": cmd_confirm_budget,
            "tick": cmd_tick, "serve": cmd_serve, "healthcheck": cmd_healthcheck, "stop": cmd_stop, "resume": cmd_resume}


def main(argv=None, env=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    if args.version:
        _out(f"ads-engine {__version__} ({label().split(' · ')[1]})")
        return 0
    if not args.cmd:
        build_parser().print_help()
        return 0
    try:
        settings = config.load(env)
    except config.ConfigError as e:
        _out(f"Beállítási hiba: {e}")
        return 2
    try:
        return COMMANDS[args.cmd](args, settings)
    except config.ConfigError as e:
        _out(f"Beállítási hiba: {e}")
        return 2
    except KeyboardInterrupt:
        return 130
