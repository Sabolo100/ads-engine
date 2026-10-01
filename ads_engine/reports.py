"""A heti jelentés: a review.run_weekly adataiból magyar szöveges és HTML levél, mentés a /data/reports-ba, küldés.

Elv: a SZÁMOK mindig kódból, táblázatban jönnek; az AI csak rövid, számjegyek nélküli értelmezést ad (Narrative). Ha az AI kimarad,
hibázik vagy számjegyet írna, a determinisztikus összefoglaló lép a helyére – a jelentés AI nélkül is teljes. Minden külső szöveg
(keresési kifejezés, kulcsszó) HTML-escape-elve kerül a levélbe.
"""
import datetime as dt
import html
import json
import pathlib
import re
from typing import List

from pydantic import BaseModel, Field

from . import llm as llmmod, log, mailer, version
from .guardrails import MICROS

KIND_LABEL = {"add_negative": "Új negatív kulcsszó", "pause_keyword": "Kulcsszó szüneteltetve", "rotate_rsa": "Hirdetésszöveg-csere",
              "fix_disapproved": "Elutasított hirdetés javítva", "pause_ad": "Hirdetés szüneteltetve", "add_keyword": "Új kulcsszó",
              "add_images": "Új képek feltöltve", "pause_image": "Kép szüneteltetve"}
STATUS_LABEL = {"applied": "kész", "validated": "próba: a Google elfogadta, de nem írtam", "failed": "NEM sikerült"}
MAX_REJECTED_SHOWN = 8


class Narrative(BaseModel):
    headline: str = Field(max_length=160)
    paragraphs: List[str] = Field(max_length=5)
    next_steps: List[str] = Field(default_factory=list, max_length=3)


# ------------------------------------------------------------------ formázás
def num(n):
    return f"{int(round(n)):,}".replace(",", " ")


def money(micros, currency="HUF"):
    if micros is None:
        return "–"
    v = micros / MICROS
    if currency == "HUF":
        return f"{num(v)} Ft"
    return f"{v:,.2f}".replace(",", " ").replace(".", ",") + f" {currency}"


def pct(x, digits=1):
    return "–" if x is None else f"{x * 100:.{digits}f}".replace(".", ",") + " %"


def delta(cur, prev):
    if not prev:
        return "új" if cur else "–"
    d = (cur - prev) / prev * 100
    if abs(d) < 0.5:
        return "±0 %"
    return f"{'+' if d > 0 else '−'}{abs(d):.0f} %"


def short_date(iso):
    return dt.date.fromisoformat(iso).strftime("%m.%d.")


def period_label(r):
    return f"{short_date(r['period']['start'])}–{short_date(r['period']['end'])}"


def subject(r):
    t = r["google"]["this"] if "google" in r else {"cost_micros": 0, "clicks": 0}
    prefix = "" if r["mode"] == "live" else "[PRÓBA] "
    return f"{prefix}[{r['name']}] Heti Google Ads jelentés · {period_label(r)} · {money(t['cost_micros'], r['currency'])}, {num(t['clicks'])} kattintás"


# ------------------------------------------------------------------ összefoglaló
def deterministic_summary(r):
    """Számokkal teli, kódból írt összefoglaló – az AI-értelmezés tartaléka."""
    cur = r["currency"]
    if "google" not in r:
        return [r["notes"][0] if r["notes"] else "Ezen a héten nincs mit jelenteni."]
    t, p = r["google"]["this"], r["google"]["prev"]
    out = []
    if not t["impressions"] and not t["clicks"]:
        out.append("Ezen a héten a hirdetések nem kaptak megjelenést (vagy a kampány szünetelt).")
    else:
        out.append(f"A hét költése {money(t['cost_micros'], cur)}, ez {num(t['clicks'])} kattintást hozott ({delta(t['clicks'], p['clicks'])} az előző héthez képest). "
                   f"Egy kattintás átlagosan {money(t['avg_cpc_micros'], cur)} volt.")
    w = r.get("web", {})
    if w.get("available") and w.get("total", {}).get("visits"):
        tot = w["total"]
        s = f"A weboldalon {num(tot['visits'])} hirdetésből érkező látogatást mértünk, ebből {num(tot['engaged'])} ({pct(w['engaged_rate'], 0)}) volt bevont"
        if w.get("cost_per_engaged_micros"):
            s += f"; egy bevont látogatás {money(w['cost_per_engaged_micros'], cur)}-ba került"
        out.append(s + ".")
    done = r.get("actions", [])
    if done:
        out.append(f"A motor {len(done)} módosítást {'hajtott végre' if r['mode'] == 'live' else 'ellenőrzött (próba)'}.")
    elif r.get("observation", {}).get("active"):
        out.append(f"Megfigyelési időszak van még {r['observation']['days_left']} napig: ilyenkor a motor nem módosít, csak mér.")
    else:
        out.append("A motor ezen a héten nem módosított semmit.")
    return out


def make_narrative(llm, slug, report):
    """AI-értelmezés SZÁMJEGYEK NÉLKÜL. Hiba, visszautasítás vagy számjegy esetén None (a determinisztikus összefoglaló lép a helyére)."""
    if llm is None or "google" not in report:
        return None
    data = {k: report[k] for k in ("name", "period", "google", "web", "top_keywords", "top_terms", "actions", "rejected", "notes", "todo", "observation")
            if k in report}
    system = ("Te egy kis hirdetési kampány heti elemzője vagy, aki egy nem szakértő tulajdonosnak ír magyarul, barátságosan, tömören. A kapott adatok "
              "alapján értelmezd a hetet: mi működik, mi nem, mit tanultunk. SZABÁLYOK: ne írj egyetlen számjegyet sem (a számok a levélben táblázatban "
              "vannak; szavakkal jelezd az irányt: nőtt, csökkent, stabil); ne ígérj eredményt; ne találj ki adatot, csak azt magyarázd, ami az "
              "adatokban benne van; ha kevés az adat, mondd ki. A teendők csak a megadott teendőlistából és jegyzetekből jöhetnek. A headline egy "
              "mondat, a paragraphs 2–4 rövid bekezdés, a next_steps legfeljebb 3 rövid pont.\n\n" + llmmod.DATA_RULE + "\nCsak a kért JSON-t add vissza.")
    try:
        n = llm.ask(project=slug, purpose="narrative", system=system, user=llmmod.wrap_data("heti_adatok", data), schema=Narrative, effort="low", max_tokens=3000)
    except llmmod.LLMError as e:
        log.warn("report.narrative_skipped", error=str(e))
        return None
    if any(re.search(r"\d", t) for t in [n.headline] + n.paragraphs + n.next_steps):
        log.warn("report.narrative_dropped", reason="számjegy a szövegben")
        return None
    return n


# ------------------------------------------------------------------ szöveges levél
def creative_line(cr):
    """A képlépés heti összegzése egy sorban (a kampányban lévő képek, új vágásváltozatok, AI-képek, keret)."""
    return (f"a kampányban {cr.get('live_images', 0)} kép · ezen a héten {cr.get('crops', 0)} új vágásváltozat, {cr.get('ai_generated', 0)} AI-kép "
            f"({cr.get('approved', 0)} elfogadva, {cr.get('rejected', 0)} elutasítva) · feltöltve: {cr.get('uploaded', 0)} · AI-képkeret: "
            f"{cr.get('ai_used_week', 0)}/{cr.get('ai_cap', 0)}")


def _action_line(a, cur):
    label = KIND_LABEL.get(a["kind"], a["kind"])
    st = STATUS_LABEL.get(a["status"], a["status"])
    line = f"{label}: {a['target']} – {a['reason']} [{st}]"
    d = a.get("detail", {})
    if a["kind"] in ("rotate_rsa", "fix_disapproved"):
        if d.get("removed"):
            line += "\n      kihullott: " + "; ".join(d["removed"][:3])
        if d.get("added"):
            line += "\n      új: " + "; ".join(d["added"][:3])
    if a["status"] == "failed" and a.get("because"):
        line += f"\n      hiba: {a['because'][0]}"
    return line


def render_text(r, narrative=None):
    cur = r["currency"]
    title = f"{r['name']} – heti Google Ads jelentés ({period_label(r)})"
    L = [title, "=" * len(title)]
    if r["mode"] != "live":
        L += ["", "PRÓBAÜZEM (dry): ebben az üzemmódban a motor semmit nem módosít a Google Ads-ben, a módosításokat csak ellenőrzi."]
    L += [""]
    if narrative:
        L += [narrative.headline, ""] + [p + "\n" for p in narrative.paragraphs]
        if narrative.next_steps:
            L += ["Javasolt következő lépések:"] + [f"  • {s}" for s in narrative.next_steps] + [""]
    else:
        L += deterministic_summary(r) + [""]
    if "google" in r:
        t, p = r["google"]["this"], r["google"]["prev"]
        L += ["SZÁMOK (ez a hét · előző hét · változás)",
              f"  Költés:            {money(t['cost_micros'], cur):>14} · {money(p['cost_micros'], cur):>14} · {delta(t['cost_micros'], p['cost_micros'])}",
              f"  Kattintás:         {num(t['clicks']):>14} · {num(p['clicks']):>14} · {delta(t['clicks'], p['clicks'])}",
              f"  Megjelenés:        {num(t['impressions']):>14} · {num(p['impressions']):>14} · {delta(t['impressions'], p['impressions'])}",
              f"  Kattintási arány:  {pct(t['ctr']):>14} · {pct(p['ctr']):>14}",
              f"  Átl. kattintási díj: {money(t['avg_cpc_micros'], cur):>12} · {money(p['avg_cpc_micros'], cur):>14} · {delta(t['avg_cpc_micros'], p['avg_cpc_micros'])}"]
        w = r.get("web", {})
        if w.get("available"):
            tot = w["total"]
            L += ["  A weboldalon (Umami):",
                  f"    látogatás a hirdetésekből: {num(tot['visits'])} · bevont: {num(tot['engaged'])}" + (f" ({pct(w['engaged_rate'], 0)})" if w.get("engaged_rate") is not None else ""),
                  f"    kulcscselekvés (kvíz kész, kártya…): {num(tot['key_actions'])}"]
            if w.get("cost_per_engaged_micros"):
                L += [f"    költség / bevont látogatás: {money(w['cost_per_engaged_micros'], cur)}"]
        if r.get("budget"):
            b = r["budget"]
            L += ["", f"KERET: heti {money(b['weekly_micros'], cur)} (napi {money(r['approved_daily_micros'], cur)}) · elköltve {money(b['spent_micros'], cur)} ({pct(b['used'], 0)})"]
    if r.get("todo"):
        L += ["", "A TE TEENDŐD"] + [f"  → {t}" for t in r["todo"]]
    L += ["", "MIT CSINÁLTAM A HÉTEN"]
    L += [f"  • {_action_line(a, cur)}" for a in r["actions"]] if r["actions"] else ["  Nem volt módosítás."]
    if r.get("creative"):
        L += ["", "KÉPEK", f"  {creative_line(r['creative'])}"]
    if r["rejected"]:
        L += ["", "AMIT JAVASOLTAM, DE A SZABÁLYOK NEM ENGEDTEK (nem hajtottam végre)"]
        for a in r["rejected"][:MAX_REJECTED_SHOWN]:
            L.append(f"  • {KIND_LABEL.get(a['kind'], a['kind'])}: {a['target']} – {a['because'][0] if a['because'] else a['reason']}")
        if len(r["rejected"]) > MAX_REJECTED_SHOWN:
            L.append(f"  … és még {len(r['rejected']) - MAX_REJECTED_SHOWN} (a naplóban mind megvan)")
    if r.get("top_keywords"):
        L += ["", "LEGTÖBB KATTINTÁST HOZÓ KULCSSZAVAK"] + [
            f"  {k['text']} [{k['match']}] – {num(k['clicks'])} kattintás, {money(k['cost_micros'], cur)} ({k['ad_group']})" for k in r["top_keywords"]]
    if r.get("top_terms"):
        L += ["", "LEGGYAKORIBB KERESÉSEK"] + [f"  „{t['term']}” – {num(t['clicks'])} kattintás, {money(t['cost_micros'], cur)}" for t in r["top_terms"]]
    if r.get("ads"):
        L += ["", "HIRDETÉSEK"]
        for a in r["ads"]:
            web = a.get("web")
            L.append(f"  {a['ad_group']} (#{a['ad_id']}, {a['status']}{'' if a['approval'] in ('', 'APPROVED') else ', ' + a['approval']}) – {num(a['clicks'])} kattintás, "
                     f"{money(a['cost_micros'], cur)}" + (f", {num(web['visits'])} látogatás / {num(web['engaged'])} bevont" if web else ""))
    notes = list(r.get("notes", [])) + [n["text"] for n in r.get("notices", [])]
    if notes:
        L += ["", "JEGYZETEK"] + [f"  • {n}" for n in notes]
    L += ["", "—", f"Ads Engine {version.label()} · üzemmód: {r['mode']} · {r['generated']}"]
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------ HTML levél (e-mail-biztos: táblázatok, soron belüli stílus)
FONT = "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif"
INK, MUTED, LINE, ACCENT = "#1f2937", "#6b7280", "#e5e7eb", "#2563eb"


def _e(x):
    return html.escape(str(x), quote=True)


def _h2(t):
    return f'<h2 style="margin:26px 0 8px;font-size:15px;color:{INK};text-transform:uppercase;letter-spacing:.04em">{_e(t)}</h2>'


def _box(t, bg, border):
    return f'<div style="margin:14px 0;padding:10px 14px;background:{bg};border-left:4px solid {border};border-radius:4px;font-size:14px">{t}</div>'


def _table(headers, rows, aligns=None):
    aligns = aligns or ["left"] * len(headers)
    th = "".join(f'<th align="{a}" style="padding:6px 8px;border-bottom:2px solid {LINE};font-size:12px;color:{MUTED};font-weight:600">{_e(h)}</th>' for h, a in zip(headers, aligns))
    body = "".join("<tr>" + "".join(f'<td align="{a}" style="padding:6px 8px;border-bottom:1px solid {LINE};font-size:13px;color:{INK}">{c}</td>' for c, a in zip(row, aligns)) + "</tr>"
                   for row in rows)
    return f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse">{"<tr>" + th + "</tr>" if headers else ""}{body}</table>'


def render_html(r, narrative=None):
    cur = r["currency"]
    parts = [f'<h1 style="margin:0 0 4px;font-size:20px;color:{INK}">{_e(r["name"])} – heti Google Ads jelentés</h1>'
             f'<div style="color:{MUTED};font-size:13px">{_e(period_label(r))}</div>']
    if r["mode"] != "live":
        parts.append(_box("<b>Próbaüzem (dry).</b> Ebben az üzemmódban a motor semmit nem módosít a Google Ads-ben; a módosításokat csak ellenőrzi.", "#eff6ff", ACCENT))
    if narrative:
        parts.append(f'<p style="font-size:16px;line-height:1.45;margin:16px 0 8px"><b>{_e(narrative.headline)}</b></p>')
        parts += [f'<p style="font-size:14px;line-height:1.5;margin:8px 0">{_e(p)}</p>' for p in narrative.paragraphs]
        if narrative.next_steps:
            parts.append("<ul style='font-size:14px;line-height:1.5;margin:8px 0 8px 18px;padding:0'>" + "".join(f"<li>{_e(s)}</li>" for s in narrative.next_steps) + "</ul>")
    else:
        parts += [f'<p style="font-size:14px;line-height:1.5;margin:8px 0">{_e(s)}</p>' for s in deterministic_summary(r)]
    if r.get("todo"):
        parts.append(_box("<b>A te teendőd</b><ul style='margin:6px 0 0 18px;padding:0'>" + "".join(f"<li>{_e(t)}</li>" for t in r["todo"]) + "</ul>", "#fffbeb", "#d97706"))
    if "google" in r:
        t, p = r["google"]["this"], r["google"]["prev"]
        rows = [["Költés", money(t["cost_micros"], cur), money(p["cost_micros"], cur), delta(t["cost_micros"], p["cost_micros"])],
                ["Kattintás", num(t["clicks"]), num(p["clicks"]), delta(t["clicks"], p["clicks"])],
                ["Megjelenés", num(t["impressions"]), num(p["impressions"]), delta(t["impressions"], p["impressions"])],
                ["Kattintási arány", pct(t["ctr"]), pct(p["ctr"]), ""],
                ["Átl. kattintási díj", money(t["avg_cpc_micros"], cur), money(p["avg_cpc_micros"], cur), delta(t["avg_cpc_micros"], p["avg_cpc_micros"])]]
        w = r.get("web", {})
        if w.get("available"):
            tot = w["total"]
            rows += [["Látogatás a hirdetésekből", num(tot["visits"]), "–", ""],
                     ["Bevont látogatás", f"{num(tot['engaged'])}" + (f" ({pct(w['engaged_rate'], 0)})" if w.get("engaged_rate") is not None else ""), "–", ""],
                     ["Költség / bevont látogatás", money(w.get("cost_per_engaged_micros"), cur), "–", ""],
                     ["Kulcscselekvés (kvíz kész, kártya…)", num(tot["key_actions"]), "–", ""]]
        parts.append(_h2("Számok") + _table(["", "Ez a hét", "Előző hét", "Változás"], [[_e(c) for c in row] for row in rows], ["left", "right", "right", "right"]))
        if r.get("budget"):
            b = r["budget"]
            parts.append(f'<p style="font-size:13px;color:{MUTED};margin:8px 0">Keret: heti {_e(money(b["weekly_micros"], cur))} (napi {_e(money(r["approved_daily_micros"], cur))}) · '
                         f'elköltve {_e(money(b["spent_micros"], cur))} ({_e(pct(b["used"], 0))})</p>')
    parts.append(_h2("Mit csináltam a héten"))
    if r["actions"]:
        items = []
        for a in r["actions"]:
            extra = ""
            d = a.get("detail", {})
            if a["kind"] in ("rotate_rsa", "fix_disapproved"):
                extra = (f"<br><span style='color:{MUTED}'>kihullott: {_e('; '.join(d.get('removed', [])[:3]))}</span>" if d.get("removed") else "") + \
                        (f"<br><span style='color:{MUTED}'>új: {_e('; '.join(d.get('added', [])[:3]))}</span>" if d.get("added") else "")
            if a["status"] == "failed" and a.get("because"):
                extra += f"<br><span style='color:#b91c1c'>hiba: {_e(a['because'][0])}</span>"
            items.append(f"<li style='margin:4px 0'><b>{_e(KIND_LABEL.get(a['kind'], a['kind']))}:</b> {_e(a['target'])} – {_e(a['reason'])} "
                         f"<span style='color:{MUTED}'>[{_e(STATUS_LABEL.get(a['status'], a['status']))}]</span>{extra}</li>")
        parts.append("<ul style='font-size:13px;line-height:1.45;margin:0 0 0 18px;padding:0'>" + "".join(items) + "</ul>")
    else:
        parts.append(f"<p style='font-size:13px;color:{MUTED};margin:0'>Nem volt módosítás.</p>")
    if r.get("creative"):
        parts.append(_h2("Képek") + f"<p style='font-size:13px;line-height:1.45;margin:0'>{_e(creative_line(r['creative']))}</p>")
    if r["rejected"]:
        parts.append(_h2("Amit javasoltam, de a szabályok nem engedtek"))
        items = [f"<li style='margin:3px 0'>{_e(KIND_LABEL.get(a['kind'], a['kind']))}: {_e(a['target'])} – <span style='color:{MUTED}'>{_e(a['because'][0] if a['because'] else a['reason'])}</span></li>"
                 for a in r["rejected"][:MAX_REJECTED_SHOWN]]
        if len(r["rejected"]) > MAX_REJECTED_SHOWN:
            items.append(f"<li style='color:{MUTED}'>… és még {len(r['rejected']) - MAX_REJECTED_SHOWN} (a naplóban mind megvan)</li>")
        parts.append("<ul style='font-size:13px;line-height:1.45;margin:0 0 0 18px;padding:0'>" + "".join(items) + "</ul>")
    if r.get("top_keywords"):
        parts.append(_h2("Legtöbb kattintást hozó kulcsszavak") + _table(
            ["Kulcsszó", "Csoport", "Kattintás", "Költés"],
            [[_e(f"{k['text']} [{k['match']}]"), _e(k["ad_group"]), num(k["clicks"]), _e(money(k["cost_micros"], cur))] for k in r["top_keywords"]],
            ["left", "left", "right", "right"]))
    if r.get("top_terms"):
        parts.append(_h2("Leggyakoribb keresések") + _table(
            ["Keresés", "Kattintás", "Költés"], [[_e(f"„{t['term']}”"), num(t["clicks"]), _e(money(t["cost_micros"], cur))] for t in r["top_terms"]], ["left", "right", "right"]))
    if r.get("ads"):
        rows = []
        for a in r["ads"]:
            web = a.get("web")
            rows.append([_e(a["ad_group"]), _e(a["status"] + ("" if a["approval"] in ("", "APPROVED") else " · " + a["approval"])), num(a["clicks"]),
                         _e(money(a["cost_micros"], cur)), f"{num(web['visits'])} / {num(web['engaged'])}" if web else "–"])
        parts.append(_h2("Hirdetések") + _table(["Hirdetéscsoport", "Állapot", "Kattintás", "Költés", "Látogatás / bevont"], rows, ["left", "left", "right", "right", "right"]))
    notes = list(r.get("notes", [])) + [n["text"] for n in r.get("notices", [])]
    if notes:
        parts.append(_h2("Jegyzetek") + "<ul style='font-size:13px;line-height:1.45;margin:0 0 0 18px;padding:0'>" + "".join(f"<li style='margin:3px 0'>{_e(n)}</li>" for n in notes) + "</ul>")
    parts.append(f'<hr style="border:0;border-top:1px solid {LINE};margin:26px 0 8px"><div style="color:{MUTED};font-size:12px">Ads Engine {_e(version.label())} · '
                 f'üzemmód: {_e(r["mode"])} · {_e(r["generated"])}</div>')
    return (f'<!doctype html><html lang="hu"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{_e(subject(r))}</title></head>'
            f'<body style="margin:0;padding:0;background:#f3f4f6;{FONT}"><table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:16px">'
            f'<table role="presentation" width="640" cellspacing="0" cellpadding="0" style="max-width:640px;width:100%;background:#ffffff;border-radius:8px"><tr><td style="padding:24px 28px;{FONT};color:{INK}">'
            + "".join(parts) + "</td></tr></table></td></tr></table></body></html>")


# ------------------------------------------------------------------ mentés és küldés
def save(settings, report, text, html_body, narrative=None):
    """A jelentés a /data/reports alá (JSON + szöveg + HTML): a levél elvesztése esetén is megvan. Visszaadja a JSON útvonalát."""
    folder = settings.data_dir / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{report['project']}-{report['period']['end']}"
    (folder / f"{stem}.txt").write_text(text, encoding="utf-8")
    (folder / f"{stem}.html").write_text(html_body, encoding="utf-8")
    path = folder / f"{stem}.json"
    path.write_text(json.dumps({**report, "narrative": narrative.model_dump() if narrative else None,
                                "engine": {"version": version.version(), "build": version.build_id()}}, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load_saved(path):
    """Egy korábban mentett jelentés (JSON) → (jelentés, Narrative | None): a sikertelen levélküldés újrapróbálásához és a `report` parancshoz."""
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    n = data.pop("narrative", None)
    data.pop("engine", None)
    return data, (Narrative(**n) if n else None)


def deliver(settings, project, store, report, narrative=None, *, send=mailer.send):
    """Összeállítja, elmenti és elküldi a levelet. Visszaad: {text, html, subject, path, mailed, mail_error}.
    A levél hibája nem állítja meg a motort: a jelentés a /data/reports-ban így is megvan."""
    text, body = render_text(report, narrative), render_html(report, narrative)
    path = save(settings, report, text, body, narrative)
    out = {"text": text, "html": body, "subject": subject(report), "path": str(path), "mailed": False, "mail_error": ""}
    try:
        send(settings, out["subject"], text, body)
        out["mailed"] = True
    except mailer.MailError as e:
        out["mail_error"] = str(e)
        log.warn("report.mail_failed", error=str(e))
    store.put(f"{project.slug}.last_report", {"period_end": report["period"]["end"], "path": str(path), "mailed": out["mailed"]})
    return out


# ------------------------------------------------------------------ havi terv levele
MONTHS = ["január", "február", "március", "április", "május", "június", "július", "augusztus", "szeptember", "október", "november", "december"]


def month_label(month):
    y, m = month.split("-")
    return f"{y}. {MONTHS[int(m) - 1]}"


def monthly_subject(m):
    prefix = "" if m["mode"] == "live" else "[PRÓBA] "
    return f"{prefix}[{m['name']}] Havi terv · {month_label(m['month'])} · téma: {m.get('theme', '–')}"


def _kw_status(k):
    return "kész" if k["status"] == "applied" else "próba: a Google elfogadta, de nem írtam"


def render_monthly_text(m):
    title = f"{m['name']} – havi terv ({month_label(m['month'])})"
    L = [title, "=" * len(title)]
    if m["mode"] != "live":
        L += ["", "PRÓBAÜZEM (dry): ebben az üzemmódban a motor semmit nem módosít a Google Ads-ben, a módosításokat csak ellenőrzi."]
    L += ["", "A HÓNAP TÉMÁJA", f"  {m.get('theme', '–')}"] + ([f"  {m['rationale']}"] if m.get("rationale") else [])
    kw = m.get("keywords", {})
    L += ["", "ÚJ KULCSSZAVAK"]
    L += [f"  • {k['text']} → {k['ad_group']} [{_kw_status(k)}]" for k in kw.get("added", [])] or ["  Ebben a hónapban nem íródott új kulcsszó."]
    for k in kw.get("failed", []):
        L.append(f"  • {k['text']}: NEM sikerült ({k['because'][0] if k['because'] else '?'})")
    if kw.get("rejected"):
        L += ["", "AMIT JAVASOLTAM, DE A SZABÁLYOK NEM ENGEDTEK (nem hajtottam végre)"]
        L += [f"  • {k['text']} – {k['because'][0] if k['because'] else ''}" for k in kw["rejected"][:MAX_REJECTED_SHOWN]]
    for heading, key in (("HIRDETÉSI SZEMPONTOK (a szövegíró ezeket használja a következő cseréknél)", "ad_angles"),
                         ("KÍSÉRLETEK (ezekről te döntesz; a motor nem hajtja végre)", "experiments"), ("MIT TANULTUNK", "learnings")):
        if m.get(key):
            L += ["", heading] + [f"  • {x}" for x in m[key]]
    if m.get("notes"):
        L += ["", "JEGYZETEK"] + [f"  • {n}" for n in m["notes"]]
    L += ["", "—", f"Ads Engine {version.label()} · üzemmód: {m['mode']} · {m['generated']}"]
    return "\n".join(L) + "\n"


def _ul(items, size=13):
    return f"<ul style='font-size:{size}px;line-height:1.45;margin:0 0 0 18px;padding:0'>" + "".join(items) + "</ul>"


def render_monthly_html(m):
    kw = m.get("keywords", {})
    parts = [f'<h1 style="margin:0 0 4px;font-size:20px;color:{INK}">{_e(m["name"])} – havi terv</h1><div style="color:{MUTED};font-size:13px">{_e(month_label(m["month"]))}</div>']
    if m["mode"] != "live":
        parts.append(_box("<b>Próbaüzem (dry).</b> Ebben az üzemmódban a motor semmit nem módosít a Google Ads-ben; a módosításokat csak ellenőrzi.", "#eff6ff", ACCENT))
    parts.append(_h2("A hónap témája") + f'<p style="font-size:16px;line-height:1.45;margin:0 0 6px"><b>{_e(m.get("theme", "–"))}</b></p>' +
                 (f'<p style="font-size:14px;line-height:1.5;margin:0">{_e(m["rationale"])}</p>' if m.get("rationale") else ""))
    items = [f"<li style='margin:4px 0'><b>{_e(k['text'])}</b> → {_e(k['ad_group'])} <span style='color:{MUTED}'>[{_e(_kw_status(k))}]</span></li>" for k in kw.get("added", [])]
    items += [f"<li style='margin:4px 0'>{_e(k['text'])}: <span style='color:#b91c1c'>NEM sikerült ({_e(k['because'][0] if k['because'] else '?')})</span></li>" for k in kw.get("failed", [])]
    parts.append(_h2("Új kulcsszavak") + (_ul(items) if items else f"<p style='font-size:13px;color:{MUTED};margin:0'>Ebben a hónapban nem íródott új kulcsszó.</p>"))
    if kw.get("rejected"):
        rej = [f"<li style='margin:3px 0'>{_e(k['text'])} – <span style='color:{MUTED}'>{_e(k['because'][0] if k['because'] else '')}</span></li>" for k in kw["rejected"][:MAX_REJECTED_SHOWN]]
        parts.append(_h2("Amit javasoltam, de a szabályok nem engedtek") + _ul(rej))
    for heading, key in (("Hirdetési szempontok", "ad_angles"), ("Kísérletek (ezekről te döntesz)", "experiments"), ("Mit tanultunk", "learnings")):
        if m.get(key):
            parts.append(_h2(heading) + _ul([f"<li style='margin:4px 0'>{_e(x)}</li>" for x in m[key]], 14))
    if m.get("notes"):
        parts.append(_h2("Jegyzetek") + _ul([f"<li style='margin:3px 0'>{_e(n)}</li>" for n in m["notes"]]))
    parts.append(f'<hr style="border:0;border-top:1px solid {LINE};margin:26px 0 8px"><div style="color:{MUTED};font-size:12px">Ads Engine {_e(version.label())} · üzemmód: {_e(m["mode"])} · {_e(m["generated"])}</div>')
    return (f'<!doctype html><html lang="hu"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{_e(monthly_subject(m))}</title></head>'
            f'<body style="margin:0;padding:0;background:#f3f4f6;{FONT}"><table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:16px">'
            f'<table role="presentation" width="640" cellspacing="0" cellpadding="0" style="max-width:640px;width:100%;background:#ffffff;border-radius:8px"><tr><td style="padding:24px 28px;{FONT};color:{INK}">'
            + "".join(parts) + "</td></tr></table></td></tr></table></body></html>")


def deliver_monthly(settings, project, store, m, *, send=mailer.send):
    """A havi terv levele: mentés a /data/plans alá (szöveg + HTML) és küldés. Visszaad: {subject, mailed, mail_error}."""
    text, body = render_monthly_text(m), render_monthly_html(m)
    folder = settings.data_dir / "plans"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{project.slug}-{m['month']}.txt").write_text(text, encoding="utf-8")
    (folder / f"{project.slug}-{m['month']}.html").write_text(body, encoding="utf-8")
    out = {"subject": monthly_subject(m), "mailed": False, "mail_error": ""}
    try:
        send(settings, out["subject"], text, body)
        out["mailed"] = True
    except mailer.MailError as e:
        out["mail_error"] = str(e)
        log.warn("monthly.mail_failed", error=str(e))
    store.put(f"{project.slug}.last_monthly", {"month": m["month"], "mailed": out["mailed"]})
    return out
