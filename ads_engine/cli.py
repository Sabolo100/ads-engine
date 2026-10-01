"""Parancssor: python -m ads_engine <parancs>

  check [--project pacsi] [--offline]   beállítás-ellenőrzés; kiírja a KÖVETKEZŐ HIÁNYZÓ LÉPÉST (kilépési kód: 1, ha van)
  status [--project pacsi]              üzemmód, utolsó futások, jóváhagyott keret, zár, STOP
  api-check [--api-version v25]         a Google API leíró frissítése, lejárat, újabb verzió
  --version                             verzió és build-azonosító

A parancsok `--yes` nélkül semmit nem írnak a Google Ads-be (a következő mérföldkövekben jönnek a launch/sync/review/report).
"""
import argparse
import sys

from . import __version__, checks, config, label, log, runtime
from .google import discovery
from .google.auth import AuthError


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
    return p


COMMANDS = {"check": cmd_check, "status": cmd_status, "api-check": cmd_api_check}


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
