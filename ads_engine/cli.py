"""Parancssor: python -m ads_engine <parancs>

  check [--project pacsi] [--offline]   beállítás-ellenőrzés; kiírja a KÖVETKEZŐ HIÁNYZÓ LÉPÉST (kilépési kód: 1, ha van)
  status [--project pacsi]              üzemmód, utolsó futások, jóváhagyott keret, zár, STOP
  api-check [--api-version v25]         a Google API leíró frissítése, lejárat, újabb verzió
  plan [--project pacsi]                az Ads Pack letöltése és ellenőrzése, a kampányfa összegzése (nem ír)
  launch [--project pacsi] [--yes]      szüneteltetett kampány létrehozása (dry: csak validateOnly; --yes nélkül csak terv)
  go-live --weekly-budget N --yes       a valódi heti keret beállítása és a kampány bekapcsolása (csak ENGINE_MODE=live)
  --version                             verzió és build-azonosító

A parancsok `--yes` nélkül semmit nem írnak a Google Ads-be (a következő mérföldkövekben jönnek a sync/review/report).
"""
import argparse
import os
import socket
import sys

from . import __version__, checks, config, label, launch, log, pack as packmod, runtime
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
    approved = store.get(f"{project.slug}.approved_daily_micros")
    if approved:
        _out(f"Jóváhagyott keret: {approved / 1_000_000:,.0f} {project.currency}/nap ({approved * 7 / 1_000_000:,.0f} /hét)".replace(",", " "))
    else:
        _out("Jóváhagyott keret: nincs (a go-live előtt a kampányok szüneteltetve maradnak)")
    runs = store.runs(project.slug, limit=5)
    _out("Utolsó futások:" if runs else "Még nem volt futás.")
    for r in runs:
        _out(f"  #{r['id']} {r['started_at']} {r['kind']:<8} {r['status']:<8} ({r['mode']})")
    store.close()
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


def _fetch_kwargs(settings):
    """Csak próbákhoz: a helyi álszerverről is letölthessen (a valódi üzemben tiltott)."""
    if settings.env.get("ADS_TEST_ALLOW_PRIVATE") == "1":
        return {"allow_private": True, "allow_http": True}
    return {}


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
    return p


COMMANDS = {"check": cmd_check, "status": cmd_status, "api-check": cmd_api_check, "plan": cmd_plan, "launch": cmd_launch,
            "go-live": cmd_go_live}


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
