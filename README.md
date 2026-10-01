# Ads Engine

Önjáró Google Ads kezelő több projekthez (Pacsi – pacsit.hu; később kinaiauto.com, darwinai.hu, polibeli.hu). A szerveren fut (Coolify, Docker), a laptop nélkül: hirdetést épít, hetente kiértékeli (Google Ads + Umami), és a korlátok között módosít. **A keretet és a kampányok be/kikapcsolását te kezeled a Google Ads felületén.**

Állapot: **0.1.0 – alap** (REST-kliens, védelmek, `check`). A teljes terv és az építési sorrend: `CHANGELOG.md`, a későbbi mérföldkövekben a `docs/` mappa.

## Gyors áttekintés

```
python -m ads_engine --version
python -m ads_engine check [--project pacsi] [--offline]   beállítás-ellenőrzés, kiírja a KÖVETKEZŐ HIÁNYZÓ LÉPÉST
python -m ads_engine status                                üzemmód, utolsó futások, jóváhagyott keret
python -m ads_engine api-check                             Google Ads API leíró frissítése, lejárat, újabb verzió
```

Üzemmód: `ENGINE_MODE=dry` (alap) semmit nem ír a Google Ads-be, csak a `validateOnly` próbát futtatja; `live` az éles indulás után. A biztonsági fékek (kampány szüneteltetése) dry módban is élesek, mert csak csökkenthetnek költést.

## Fejlesztés

```
pip install -r requirements.txt
python -m unittest discover -s tests          # ~100 próba, álszerverekkel (Google OAuth + Ads REST), a valódi Google-hez nem ér
```

A próbák a Google hivatalos REST-leíró dokumentuma (v25) ellen ellenőrzik a kéréseket (`.cache/googleads_v25.json`, az `api-check` tölti le).

## Titkok

Soha nem kerülnek a repóba: a Coolify titkos környezeti változóiban élnek (`GADS_SA_JSON_B64`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `UMAMI_*`, `SMTP_*`). A napló minden ismert titkot kitakar. A teljes lista: `ads_engine/config.py` fejléce.

## Verziózás

A verzió egy helyen van: `VERSION`. Látszik a `--version` kimenetében, a heti levél láblécében és az állapot-végponton, mellette a build-azonosító (a kód tartalom-hash-e). Változásnapló: `CHANGELOG.md`.
