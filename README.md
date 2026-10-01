# Ads Engine

Önjáró Google Ads kezelő több projekthez (Pacsi – pacsit.hu; később kinaiauto.com, darwinai.hu, polibeli.hu). A szerveren fut (Coolify, Docker), a laptop nélkül: hirdetést épít, hetente kiértékeli (Google Ads + Umami), és a **kódban lévő korlátok** között módosít, majd magyar levélben beszámol. **A keretet és a kampányok be/kikapcsolását te kezeled a Google Ads felületén.**

Állapot: **0.4.0** – Search-kampány építése, napi védelmek, heti kiértékelés és jelentés, **kreatív-gyár** (új képek, AI-kulcsvizuálok keretezve és ellenőrizve), **havi terv**, ütemező, Docker. A pontos változások: `CHANGELOG.md`.

## Hogyan működik

```
 Te: Google Ads felület (keret, szünet)          Te / a projekt: pacsit.hu/ads/brief.json (irány, tiltások, tények)
              │                                                   │
 ┌────────────▼───────────────────────────────────────────────────▼──────────────┐
 │ ads-engine (1 konténer, /data kötet)                                          │
 │  napi 06:30 szinkron + fékek · hétfő 07:00 heti kör · óránként nyitóoldal-őr  │
 │  Google Ads REST v25  ◄─►  kampány, kulcsszó, RSA, riportok (validateOnly!)   │
 │  Umami  ◄──  látogatás és „bevont” esemény UTM szerint                         │
 │  Claude (javaslat, szöveg)  →  KÓDBAN lévő szabályok  →  végrehajtás + napló   │
 │  heti magyar levél (SMTP)                                                      │
 └───────────────────────────────────────────────────────────────────────────────┘
```

Az AI csak **javasol**; a döntést a `guardrails.py` / `validators.py` szabályai hozzák (elég adat, tények, védett magkifejezések, heti korlátok, „kézben lévő” objektumok). Alapból `dry` üzemmód: minden módosítást a Google `validateOnly` próbájával ellenőriz, de nem ír.

## Dokumentáció

| Dokumentum | Kinek | Miről |
|---|---|---|
| `docs/GOOGLE_ADS_BEALLITAS.md` | a tulajdonosnak | az egyszeri beállítás lépésről lépésre (Google Ads, Cloud, szolgáltatásfiók, Umami, Coolify) |
| `docs/MUKODES.md` | a tulajdonosnak | napirend, mit módosíthat a motor, fékek, levelek, parancsok, hibakeresés |
| `docs/ADS_ENGINE_BEKOTES.md` | egy másik projektnek (és AI-ügynökének) | a szerződés és egyben prompt: milyen formában kéri a motor a kész hirdetéseket (`schema/`, `examples/pacsi/`, `validator/`) |

## Gyors áttekintés

```
python -m ads_engine --version
python -m ads_engine check                      beállítás-ellenőrzés, kiírja a KÖVETKEZŐ HIÁNYZÓ LÉPÉST
python -m ads_engine plan                       az Ads Pack letöltése és ellenőrzése, a kampányfa összegzése (nem ír)
python -m ads_engine launch --yes               szüneteltetett kampány létrehozása (dry: csak próba)
python -m ads_engine go-live --weekly-budget N --yes   a keret beállítása és a kampány bekapcsolása
python -m ads_engine serve                      a szolgáltatás: ütemező + /healthz (ez a Docker-konténer fő parancsa)
python -m ads_engine status | sync | weekly | monthly | report | confirm-budget | stop | resume | tick | healthcheck | api-check
```

A teljes parancslista és a használat: `docs/MUKODES.md`. Üzemmód: `ENGINE_MODE=dry` (alap) | `live`; a biztonsági fékek (kampány szüneteltetése) `dry` módban is élesek, mert csak csökkenthetnek költést.

## Futtatás (Docker)

```
docker build -t ads-engine .
docker run -d --name ads-engine -v ads-engine-data:/data -e ENGINE_MODE=dry --env-file titkok.env ads-engine
```

A `/data` tartós kötet kell (SQLite állapot, jelentések, gyorsítótár). A titkok futásidejű környezeti változók (lásd alább); a képbe nem kerülnek. Coolify: az alkalmazás Dockerfile-alapú, tartós tárolóval a `/data`-ra, **automatikus deploy nélkül**.

## Fejlesztés

```
pip install -r requirements.txt
python -m unittest discover -s tests          # ~450 próba álszerverekkel (Google OAuth + Ads REST, Anthropic, OpenAI, Umami, SMTP, oldal)
```

A próbák a valódi szolgáltatásokhoz nem érnek. A Google kéréseit a hivatalos REST-leíró dokumentum (v25) ellen ellenőrzik (`.cache/googleads_v25.json`, az `api-check` tölti le). Az önálló bekötési validátor újragenerálása: `python tools/build_validator.py`.

## Titkok

Soha nem kerülnek a repóba: a Coolify titkos környezeti változóiban élnek (`GADS_SA_JSON_B64`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `UMAMI_PASSWORD`, `SMTP_PASSWORD`, `HEARTBEAT_URL`). A napló minden ismert titkot kitakar. A teljes lista: `ads_engine/config.py` fejléce és `docs/GOOGLE_ADS_BEALLITAS.md` 6. lépés.

## Verziózás

A verzió egy helyen van: `VERSION` (FŐ.MELLÉK.JAVÍTÁS). Látszik a `--version` kimenetében, a heti levél láblécében és a `/healthz` válaszában, mellette a build-azonosító (a kód tartalom-hash-e). Változásnapló: `CHANGELOG.md`.
