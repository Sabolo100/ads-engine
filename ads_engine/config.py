"""Beállítások: környezeti változók (a titkok a Coolify titkos változóiban élnek) + config/projects.toml (nem titkos).

Környezeti változók:
  ENGINE_MODE            dry (alap: semmit nem ír, de a validateOnly-t meghívja) | live
  DATA_DIR               állapot és képek (Coolify-kötet: /data)           TZ: Europe/Budapest
  GADS_SA_JSON_B64       a Google szolgáltatásfiók JSON-kulcsa base64-ben (lásd google/auth.py)
  GADS_LOGIN_CUSTOMER_ID a kezelői (MCC) fiók azonosítója, kötőjel nélkül
  GADS_API_VERSION       alap: v25                       GADS_BASE_URL: csak próbákhoz
  ANTHROPIC_API_KEY      az elemzéshez és a szövegíráshoz (LLM_MODEL: alap claude-sonnet-5-5)
  OPENAI_API_KEY         képgeneráláshoz (gpt-image-2)
  UMAMI_URL / UMAMI_USER / UMAMI_PASSWORD   csak-olvasó Umami-felhasználó
  SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_FROM   a heti levélhez; REPORT_TO: címzettek (vesszővel)
A projektek (nem titkos adatok) a config/projects.toml-ban vannak (vagy a PROJECTS_FILE által megadott fájlban);
új projekt = új [[project]] blokk.
"""
import dataclasses
import os
import pathlib
import tomllib

from . import log

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_PROJECTS = ROOT / "config" / "projects.toml"


class ConfigError(Exception):
    pass


@dataclasses.dataclass
class Project:
    slug: str
    name: str
    site: str
    brief_url: str
    customer_id: str = ""              # Google Ads ügyfélfiók (kötőjel nélkül); üres, amíg a fiók nincs létrehozva
    umami_website_id: str = ""
    language: str = "hu"
    country: str = "HU"
    currency: str = "HUF"
    timezone: str = "Europe/Budapest"
    allowed_hosts: tuple = ()          # a végső URL-ek és a képek engedélyezett gazdagépei (itt, nem a briefben!)
    image_hosts: tuple = ()
    max_images_per_week: int = 10      # kemény korlát: az OpenAI-hívások száma ISO-hetenként


@dataclasses.dataclass
class Settings:
    mode: str
    data_dir: pathlib.Path
    timezone: str
    login_customer_id: str
    api_version: str
    base_url: str
    llm_model: str
    anthropic_key: str
    openai_key: str
    umami_url: str
    umami_user: str
    umami_password: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str
    report_to: list
    projects: dict
    env: dict

    @property
    def live(self):
        return self.mode == "live"

    @property
    def db_path(self):
        return self.data_dir / "engine.sqlite"

    def project(self, slug=None):
        if not self.projects:
            raise ConfigError("Nincs projekt a config/projects.toml-ban.")
        if slug is None:
            if len(self.projects) == 1:
                return next(iter(self.projects.values()))
            raise ConfigError("Több projekt van, add meg: --project <slug> (" + ", ".join(self.projects) + ")")
        if slug not in self.projects:
            raise ConfigError(f"Ismeretlen projekt: {slug} (van: {', '.join(self.projects)})")
        return self.projects[slug]


def load_projects(path=None):
    path = pathlib.Path(path or DEFAULT_PROJECTS)
    if not path.exists():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"A {path.name} nem érvényes TOML: {e}") from e
    out = {}
    fields = {f.name for f in dataclasses.fields(Project)}
    for raw in data.get("project", []):
        unknown = set(raw) - fields
        if unknown:
            raise ConfigError(f"Ismeretlen mező a projektben ({raw.get('slug', '?')}): {', '.join(sorted(unknown))}")
        for req in ("slug", "name", "site", "brief_url"):
            if not raw.get(req):
                raise ConfigError(f"A projektből hiányzik a '{req}' ({raw.get('slug', '?')}).")
        p = Project(**{**raw, "allowed_hosts": tuple(raw.get("allowed_hosts", ())), "image_hosts": tuple(raw.get("image_hosts", ())),
                       "customer_id": "".join(ch for ch in str(raw.get("customer_id", "")) if ch.isdigit())})
        if p.slug in out:
            raise ConfigError(f"Dupla projekt-azonosító: {p.slug}")
        out[p.slug] = p
    return out


def load(env=None, projects_path=None):
    env = dict(os.environ if env is None else env)
    mode = (env.get("ENGINE_MODE") or "dry").strip().lower()
    if mode not in ("dry", "live"):
        raise ConfigError("Az ENGINE_MODE értéke dry vagy live lehet.")
    smtp_port = env.get("SMTP_PORT", "465")
    if not smtp_port.isdigit():
        raise ConfigError("Az SMTP_PORT szám legyen.")
    s = Settings(
        mode=mode,
        data_dir=pathlib.Path(env.get("DATA_DIR") or (ROOT / "data")),
        timezone=env.get("TZ") or "Europe/Budapest",
        login_customer_id="".join(ch for ch in env.get("GADS_LOGIN_CUSTOMER_ID", "") if ch.isdigit()),
        api_version=env.get("GADS_API_VERSION") or "v25",
        base_url=env.get("GADS_BASE_URL") or "https://googleads.googleapis.com",
        llm_model=env.get("LLM_MODEL") or "claude-sonnet-5-5",
        anthropic_key=env.get("ANTHROPIC_API_KEY", ""),
        openai_key=env.get("OPENAI_API_KEY", ""),
        umami_url=(env.get("UMAMI_URL") or "").rstrip("/"),
        umami_user=env.get("UMAMI_USER", ""),
        umami_password=env.get("UMAMI_PASSWORD", ""),
        smtp_host=env.get("SMTP_HOST", ""),
        smtp_port=int(smtp_port),
        smtp_user=env.get("SMTP_USER", ""),
        smtp_password=env.get("SMTP_PASSWORD", ""),
        smtp_from=env.get("SMTP_FROM", ""),
        report_to=[a.strip() for a in env.get("REPORT_TO", "").split(",") if a.strip()],
        projects=load_projects(projects_path or env.get("PROJECTS_FILE")),
        env=env,
    )
    for secret in (s.anthropic_key, s.openai_key, s.umami_password, s.smtp_password):
        log.add_secret(secret)
    return s
