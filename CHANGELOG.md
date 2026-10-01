# Változásnapló – Ads Engine

A verzió egy helyen van: a `VERSION` fájlban (FŐ.MELLÉK.JAVÍTÁS). Látszik a `python -m ads_engine --version` kimenetében, a heti levél láblécében és az állapot-végponton (`/healthz`), mellette a build-azonosítóval (a kód tartalom-hash-e).

## 0.1.0 – 2026-10-01 – Alap
- Projektváz: konfiguráció (környezeti változók + `config/projects.toml`), JSON-napló titokkitakarással, HTTP-segéd újrapróbálással.
- Állapottároló (SQLite, WAL): futások, művelet-napló (előtte/utána), feladat-nyilvántartás, számlálók (AI-képkeret), zár (egyetlen író).
- Google Ads REST-kliens (v25): szolgáltatásfiókos belépés (RS256 JWT), `search`, `mutate` (`validateOnly`, atomi, ideiglenes azonosítók), hibaértelmezés, újrapróbálás.
- Hivatalos v25 leíró-dokumentum alapú kérés-ellenőrzés (`api-check`, payload-validátor).
- Védelmek magja: keret-őr (napi 2,1×, 30 napi 30,4×, elírás-védelem ≥ 2×), címke-alapú tulajdonjog.
- `check` (a következő hiányzó lépést kiírja), `status`, `api-check`, `--version`.
- Álszerver (Google OAuth + Ads REST) és automatikus próbák.
