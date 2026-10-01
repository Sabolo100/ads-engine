# Változásnapló – Ads Engine

A verzió egy helyen van: a `VERSION` fájlban (FŐ.MELLÉK.JAVÍTÁS). Látszik a `python -m ads_engine --version` kimenetében, a heti levél láblécében és az állapot-végponton (`/healthz`), mellette a build-azonosítóval (a kód tartalom-hash-e).

## 0.2.0 – 2026-10-01 – Pacsi Search: csomag, validátorok, kampányépítő
- **Ads Pack** (a szerződés: `docs/ADS_ENGINE_BEKOTES.md`): `brief.json` + `creatives.json` a projekt saját oldaláról, séma szerint (`schema/`), saját JSON-Schema ellenőrzővel. Biztonságos letöltés (csak https, engedélyezett gazdagépek a motor konfigjában, nyilvános IP, méret- és átirányítás-korlát), ETag-gyorsítótár.
- **Validátorok kódban:** hirdetésszöveg (hossz, írásjelek, emoji, csupa nagybetű, URL/e-mail/telefon), tiltott szavak és versenytársak, **tények** (minden szám és kényes állítás a brief tényeiből), kulcsszavak és negatívok (a negatív nem zárhatja ki a pozitívat). Az önálló, függőség nélküli validátor ugyanezt futtatja a másik projektben (`validator/ads_pack_validator.py`, `--online`: nyitóoldalak UTM-megőrzése, képméretek).
- **Képek** a Google méreteire (1,91:1 / 1:1 / 4:5): vágás, egyöntetű szélű képnél színnel kitöltés, ≤ 5 MB, tartalom-hash név.
- **Kampányépítő:** egy atomi Search-kampányfa (keret, `ads-engine` címke, szüneteltetett kampány, hely és nyelv, negatívok, hirdetéscsoportok, kulcsszavak, RSA, hivatkozások, kiemelések, képek, UTM-utótag); az EU politikai hirdetési nyilatkozat, `explicitlyShared=false`, hálózati beállítások, `PRESENCE`, AI Max ki.
- **`plan` / `launch` / `go-live`:** dry módban a teljes fa `validateOnly`; live módban szüneteltetett kampány (a képek külön kérésben), újrafuttatás nem duplikál; a go-live a valódi keretet és a bekapcsolást egyetlen atomi műveletben állítja.
- **AI-réteg** (Claude, Anthropic SDK, strukturált kimenet, napló) és **hirdetésszöveg-író** validátoros szűréssel.
- **Pacsi-oldal:** `pacsit.hu/ads/` hirdetési csomag, landing-útvonalak, `bevont` Umami-esemény (app 1.9.0).
- Álszerverek (Google Ads, Anthropic, csomag-szerver) és ~210 próba.

## 0.1.0 – 2026-10-01 – Alap
- Projektváz: konfiguráció (környezeti változók + `config/projects.toml`), JSON-napló titokkitakarással, HTTP-segéd újrapróbálással.
- Állapottároló (SQLite, WAL): futások, művelet-napló (előtte/utána), feladat-nyilvántartás, számlálók (AI-képkeret), zár (egyetlen író).
- Google Ads REST-kliens (v25): szolgáltatásfiókos belépés (RS256 JWT), `search`, `mutate` (`validateOnly`, atomi, ideiglenes azonosítók), hibaértelmezés, újrapróbálás.
- Hivatalos v25 leíró-dokumentum alapú kérés-ellenőrzés (`api-check`, payload-validátor).
- Védelmek magja: keret-őr (napi 2,1×, 30 napi 30,4×, elírás-védelem ≥ 2×), címke-alapú tulajdonjog.
- `check` (a következő hiányzó lépést kiírja), `status`, `api-check`, `--version`.
- Álszerver (Google OAuth + Ads REST) és automatikus próbák.
