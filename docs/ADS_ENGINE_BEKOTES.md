# Ads Engine – bekötési leírás

**Egy projekt bekötése az Ads Engine-be:** a projekt (weboldal, app) a saját oldalán kitesz néhány statikus fájlt (`/ads/` mappa), az Ads Engine pedig ezeket húzza, és a projekt nevében Google Ads hirdetést készít, futtat és iterál. Ez a dokumentum a **szerződés**: pontosan leírja, milyen formában kéri a motor a kész hirdetéseket. Egyben **prompt** is: add át annak a rendszernek (AI-ügynöknek vagy fejlesztőnek), amelyik a projektet építi, és ő elkészíti a projekt „Ads Engine modulját”.

- **Verzió:** 1.0 (a csomag sémája: `schema_version: 1`). A motor és a séma verziója a `CHANGELOG.md`-ben van.
- **Referencia-megvalósítás:** a Pacsi (`https://pacsit.hu/ads/`), a forrása `examples/pacsi/` a motor repójában.
- **Önálló validátor:** `validator/ads_pack_validator.py` (egyetlen fájl, külső csomag nélkül) – lásd a 7. pontot.

---

## 0. A prompt (másold be a projekt AI-ügynökének)

```text
Készítsd el ennek a projektnek az „Ads Engine modulját”. Az Ads Engine egy önjáró Google Ads kezelő: a projekt saját
oldaláról húzza a hirdetési csomagot, és abból kampányt épít, futtat, kiértékel és javít. A feladatod, hogy a projekt oldalán
egy statikus /ads/ mappa legyen, a mellékelt szerződés (ADS_ENGINE_BEKOTES.md) szerint:

1. Olvasd el a teljes ADS_ENGINE_BEKOTES.md-t, és a projekt valós tartalmából (a termék adataiból, a kész szövegekből és
   képekből) írd meg az /ads/brief.json-t (3. pont) és az /ads/creatives.json-t (4. pont). Ne találj ki tényt: minden szám és
   kényes állítás (ingyenes, garantált, ár, egészség…) csak a brief `product.facts` mezőjéből jöhet, és a tényeknek igaznak kell lenniük.
2. Tedd a képeket az /ads/img/ alá (JPEG vagy PNG, ≤ 5 MB; 1,91:1, 1:1 vagy 4:5 arány a 4. pont szerint).
3. Biztosítsd, hogy a nyitóoldalak átirányításai megtartsák a lekérdezést (?utm_…), és ha a projekt Umami-t vagy más
   süti nélküli mérést használ, legyen egy „bevont látogatás” esemény oldalbetöltésenként egyszer (5. pont).
4. A /ads/ mappát tedd élesbe (https), `X-Robots-Tag: noindex` fejléccel, ETag-gel vagy Last-Modified-del.
5. Futtasd a validátort: python ads_pack_validator.py https://<oldal>/ads/brief.json --online
   Addig javíts, amíg „A csomag megfelel a motor szabályainak” üzenetet nem kapod.
6. A végén add meg nekem a regisztrációhoz kellő adatokat (8. pont).
Soha ne tegyél a csomagba kulcsot, jelszót vagy személyes adatot. Ha a projekt szabályozott területen van (pénzügy, egészség,
gyógyszer, szerencsejáték, politika, élő állat értékesítése), jelezd a brief `compliance.category` mezőjében: „regulated”.
```

---

## 1. Hogyan működik (két mondatban)

A motor időnként letölti a `brief.json`-t és a `creatives.json`-t (`ETag` miatt csak változáskor), kódban ellenőrzi őket (hossz, írásjelek, tiltott szavak, **tények**), és csak a hibátlan csomagból épít Google Ads **Search** kampányt (szüneteltetve; a bekapcsolás és a keret az embernél van). Ezután hetente kiértékeli a Google- és a webes (Umami) számokat, és a szabályok szerint negatív kulcsszót ad hozzá, gyenge hirdetést szüneteltet, új szövegváltozatot és (ha engedélyezett) új képet készít; a csomagodban lévő változtatásokat észleli, és újra ellenőrzi.

**A te dolgod:** a csomag előállítása és karbantartása. **Nem a te dolgod:** a Google Ads fiók, a keret, a kampány bekapcsolása, a kulcsok.

## 2. Hol és hogyan érhető el a csomag

- **Cím:** `https://<oldal>/ads/brief.json` (kötelező) és `https://<oldal>/ads/creatives.json` (ajánlott), a képek `…/ads/img/…` alatt. Más mappanév is jó, ha a motor üzemeltetője a brief URL-jét így veszi fel.
- **Csak https**, hitelesítés és süti nélkül, nyilvánosan olvasható. A fájlokban nincs titok.
- **Gazdagép-korlát:** a motor csak azokat a gazdagépeket tölti le, amelyeket az üzemeltető a motor konfigjában (`projects.toml`: `allowed_hosts`, `image_hosts`) engedélyezett. A brief ezt **nem** írhatja felül, és minden nyitóoldal URL-jének is az engedélyezett gazdagépen kell lennie.
- **Biztonság:** a motor nem követ átirányítást engedélyezetlen gazdagépre, nem tölt le privát/belső címről, és korlátozza a méretet (JSON ≤ 2 MB, kép ≤ 12 MB).
- **Ajánlott fejlécek:** `X-Robots-Tag: noindex, nofollow` (a csomag nem oldal), `Cache-Control: no-cache` + `ETag`, hogy a változás azonnal látszik, de felesleges letöltés nincs. Nginx példa: lásd a Pacsi `deploy/nginx.conf`-ját (az `/ads/` location a szerverszintű `add_header`-eket meg kell ismételje).
- **Kulcsnevek angolul, az értékek az ország nyelvén** (a Pacsinál magyarul).

## 3. `brief.json` – kinek, mit, milyen hangon, milyen tényekkel

Séma: `schema/ads-brief.schema.json`. Kötelező: `schema_version`, `project`, `product`, `voice`, `landing_pages`, `keywords`.

| Mező | Kötelező | Leírás |
|---|---|---|
| `schema_version` | igen | `1` |
| `project.slug` | igen | kisbetű, 2–30 karakter (`a-z0-9-`); megegyezik a motor `projects.toml`-jában lévővel |
| `project.name` | igen | a márka neve, ahogy a hirdetésekben szerepel (≤ 40) |
| `project.site` | igen | a kezdőoldal https-címe, útvonal nélkül |
| `project.language`, `project.country` | igen | pl. `hu`, `HU`: a hirdetés nyelve és célországa |
| `product.summary` | igen | 20–600 karakter: mit csinál a termék, kinek |
| `product.offer` | nem | a fő ajánlat egy mondatban |
| `product.audiences[]` | nem | `{id, name, needs[], search_intents[]}`: kinek szól, mit keresnek |
| `product.usps[]` | nem | rövid előnyök (kiemelések alapja) |
| **`product.facts[]`** | **igen** | `{id, text, numbers[], tokens[]}` – lásd alább, ez a legfontosabb |
| `voice.tone` | igen | hangnem (pl. „tegező, meleg, rövid mondatok”) |
| `voice.avoid[]` | nem | stílus-tiltások (szabad szöveg) |
| `voice.forbidden_words[]` | nem | tiltott szavak: a kód ékezet- és kisbetű-függetlenül, szótő-kezdettel ellenőrzi (pl. `garantált` → `garantáltan` is) |
| `voice.forbidden_claims[]` | nem | tiltott állítások szabad szöveggel (az AI-szövegíró megkapja) |
| `compliance.category` | nem | `none` vagy `regulated` (szabályozott terület: a motor csak emberi jóváhagyással ír) |
| `landing_pages[]` | igen | `{id, url, purpose}`: a nyitóoldalak (a hirdetések ide visznek); `url` UTM nélkül, https |
| `keywords.core[]` | igen | magkifejezések (1–30): a motor ezeket soha nem zárja ki, és az új kulcsszavaknak ezekhez közel kell lenniük |
| `keywords.negatives[]` | nem | kezdő negatív kulcsszavak (kifejezés típusúak) – ami a hirdetett dologtól eltérő keresés (pl. „eladó”, „ár”, „letöltés”) |
| `keywords.competitor_brands[]` | nem | versenytárs-márkanevek: sehol nem használja őket |
| `tracking.umami_website_id` | nem | az Umami webhely-azonosító (a motor konfigja az irányadó) |
| `tracking.engaged_events[]`, `tracking.key_events[]` | nem | melyik esemény jelent „bevont látogatást” / kulcscselekvést (5. pont) |
| `ads.max_cpc` | nem | kattintásonkénti plafon a fiók pénznemében |
| `ads.business_name` | nem | cégnév a hirdetésekben (≤ 25) |
| `brand` | nem | `colors`, `fonts`, `logo`, `image_style_prompt`, `reference_images[]`: az AI-képgeneráláshoz |
| `seasonality[]` | nem | `{month, themes[]}`: havi témák |
| `creatives_url` | nem | a kész hirdetések (`creatives.json`) helye, relatív vagy abszolút |

### A `facts` – a rendszer gerince

A motor **kódban** ellenőrzi, hogy a hirdetésszövegben szereplő minden **szám** és **kényes állítás** egy ténnyel igazolt-e. Amit nem igazol tény, az nem kerül ki, akárki írta (te, egy skill vagy az AI).

- `numbers`: azok a számok, amelyek szerepelhetnek a szövegben. Példa: `{"id":"fajtak","text":"Az app 124 népszerű kutyafajtát mutat.","numbers":["124"]}`.
- `tokens`: azok a kényes szavak, amelyeket a tény igazol. Példa: `{"id":"ingyenes","text":"Az app ingyenes.","tokens":["ingyenes","ingyen"]}`.
- **Kényes állítások** (tényhez kötöttek): ingyen/ingyenes, regisztráció nélkül, garancia/garantált, bizonyított, hatásos/hatékony, megbízható, tudományos, szakértői, ár/akció/kedvezmény/olcsó/forint, allergia és egészség, **felsőfok** (legjobb, legolcsóbb…), egyediség (első, egyetlen, „1. számú”), 100 %.
- **Csak igaz tényt írj.** A tényeket az üzemeltető és a Google Ads is számon kérheti. Ha nem biztos, hagyd ki.

### Jó, ha tudod

- A `keywords.core` ne legyen túl általános (pl. csak „kutya”): ahhoz a negatívok nehezen illeszthetők.
- A `negatives` ne tartalmazzon olyat, ami a magkifejezéseket vagy a `creatives.json` kulcsszavait kizárná (a validátor szól: „kizárná a… kifejezést”).
- A szavak, amelyek más kategóriát idézhetnek (a Pacsinál a „párkereső” a társkereső-szabályt válthatta volna ki), kerüljenek a `forbidden_words`-be.

## 4. `creatives.json` – a kész hirdetések

Séma: `schema/creative-pack.schema.json`. Kötelező: `schema_version`, `version`, `adsets`. A `version` a csomag verziója (pl. `2026-10-05.1`): változáskor a motor újra ellenőriz.

### Hirdetéscsoportok (`adsets[]`, 1–20)

Egy téma / keresési szándék = egy csoport. Mezők:

| Mező | Szabály |
|---|---|
| `id` | `a-z0-9_-`, ≤ 30, egyedi |
| `channel` | `search` (reszponzív keresési hirdetés) |
| `theme` | a csoport neve (≤ 60) |
| `landing` | a brief `landing_pages[].id`-ja |
| `headlines[]` | **3–15 cím, mind ≤ 30 karakter, nincs felkiáltójel**, nincs emoji, nincs csupa nagybetűs szó (rövidítés kivételével), nincs URL/e-mail/telefon, nincs ismétlődés |
| `descriptions[]` | **2–4 leírás, mind ≤ 90 karakter, legfeljebb egy felkiáltójel**, ugyanazok a tiltások |
| `path1`, `path2` | megjelenített útvonal, ≤ 15 karakter, betű/szám/kötőjel; a 2. csak az 1. mellett |
| `keywords[]` | 1–40: `{text, match}`; `match` = `PHRASE` vagy `EXACT` (széles egyezés nincs); ≤ 80 karakter, ≤ 10 szó, nincs írásjel/URL |
| `negatives[]` | csak erre a csoportra érvényes negatívok |

- Írj **változatos** címeket (előny, kérdés, felszólítás, konkrét tény), mert a Google ezekből állít össze kombinációkat és méri a jobbakat.
- Egy-egy cím legyen **önmagában is értelmes**, mert a Google bármelyik kettővel/hárommal összeteheti.
- **Mondatszerű írás:** ne írj minden szót nagybetűvel („Pacsi Kutyafajta Választó” helyett „Pacsi kutyafajta-választó”).

### Hivatkozások, kiemelések, képek, videók

| Elem | Szabály |
|---|---|
| `sitelinks[]` (≤ 8) | `{text ≤ 25, description1 ≤ 35, description2 ≤ 35, landing}` |
| `callouts[]` (≤ 10) | ≤ 25 karakter, felkiáltójel nélkül (pl. „Ingyenes”, „Regisztráció nélkül”) |
| `images[]` | `{id, file, ratio, alt}`: JPEG vagy PNG, ≤ 5 MB. **Arány:** `1.91:1` (ajánlott 1200×628, min. 600×314), `1:1` (1200×1200, min. 300×300), `4:5` (960×1200, min. 480×600), ±1 % tűréssel. Ha az arány nem pontos, a motor a Google méreteire vágja/kitölti. `alt`: mit ábrázol a kép (akadálymentesség). A kép ne tartalmazzon sok szöveget, és ne legyen rajta csalogató/félrevezető felirat |
| `logos[]` | `{id, file}`: 1:1, min. 128×128 |
| `videos[]` | `{youtube_id, orientation, title}`: nyilvános vagy nem listázott YouTube-videó; függőleges (9:16) ajánlott. Egyelőre a Demand Gen kampányhoz (későbbi kiadás) |

A fájl elérési útja relatív a `creatives.json`-hoz (vagy abszolút https, engedélyezett gazdagépen).

### Amit a motor kódban ellenőriz a szövegeken

1. hossz, darabszám, ismétlődés; 2. írásjelek (felkiáltójel a címben, három pont, ismétlődő írásjel), emoji, csupa nagybetű, URL/e-mail/telefon; 3. a brief tiltott szavai és versenytársai; 4. **tények** (számok, kényes állítások); 5. kulcsszavak (hossz, szavak száma, írásjel, magkifejezéshez közel); 6. negatívok (ne zárjanak ki pozitív vagy mag-kifejezést).

## 5. Nyitóoldalak és mérés

**Követés.** A motor a kampányon egy végső URL-utótagot ad: `utm_source=google&utm_medium=cpc&utm_campaign=<slug>-kereso&utm_content={creative}&utm_term={keyword}`. A Google automatikus címkézése (gclid) ki lesz kapcsolva (az üzemeltető állítja be), hogy az oldal süti- és személyes adat nélküli maradjon.

**A nyitóoldalaknak meg kell tartaniuk a lekérdezést.**
- Minden átirányítás (http→https, `www`→nélküli, `/kviz`→`/#kviz`…) vigye tovább az `?utm_…` részt. Nginx: `return 302 "/?$args#kviz";`, `return 301 "/hirlevel/$is_args$args";`.
- A `#` utáni rész (az app belső állapota, pl. `#kviz`, `#f=gyerek&v=lista`) a Google végső URL-jében nem biztos, hogy az UTM elé/mögé kerül: ezért a mélylinkekhez **valódi útvonal** kell (`/kviz`), ami átirányít a megfelelő állapotba. A validátor `--online` módban lekéri a nyitóoldalakat egy tesztes UTM-mel, és hibát jelez, ha az átirányítás után az `utm_*` elveszett.
- Gyors, mobilbarát, nincs tolakodó felugró ablak, az oldal tartalma egyezik a hirdetés ígéretével (a Google ezt is méri).

**Süti nélküli statisztika (ajánlott).** A motor az UTM alapján a webes oldalon (Umami) azt nézi, hány hirdetésből jövő látogatás volt **bevont**. Ehhez a projektnek küldenie kell egy „bevont” eseményt **oldalbetöltésenként egyszer**, az első érdemi interakciónál (pl. kvíz indítása, kártya megnyitása, szűrő, keresés). Az Umami szűrői eseményszintűek, és az indulási esemény minden látogatáskor tüzel, ezért a visszapattanás nem mérhető nélküle. Minta (Pacsi, `src/js/15-stat.js`):

```js
const ENGAGE = new Set(['kviz-indul', 'kartya', 'szuro', 'kereses']);   // a projekt érdemi interakciói
let engaged = false;
function stat(name, data) {
  /* … az esemény küldése … */
  if (name && !engaged && ENGAGE.has(name)) { engaged = true; stat('bevont', { elso: name }); }
}
```

**Nincs Google-címke és süti az oldalon.** A motor nem használ konverziókövetést: a minőséget (költség/bevont látogatás) a saját, süti nélküli mérésből számolja.

## 6. Új kreatívok kérése (opcionális, kidolgozás alatt)

**Alapeset – nulla kód:** a projekt frissíti a `creatives.json`-t és a képeket (kézzel, skill-lel vagy AI-ügynökkel), a motor a következő futásnál észleli (ETag/`version`).

**Ha a projekt saját generátort futtat** (pl. egy Claude Code skill vagy szolgáltatás), a motor később kérés–válasz végponton is kérhet kreatívot. A szerződés (a motor ezt a formát fogja küldeni; a megvalósítás a következő kiadásban jön, a projekt nyugodtan előkészítheti):

```text
POST https://<oldal>/ads/request
Fejlécek: Content-Type: application/json
          X-Ads-Signature: sha256=<hex HMAC-SHA256 a nyers törzsre, a közös titokkal>
          X-Ads-Timestamp: <unix idő>      Idempotency-Key: <uuid>
Törzs:    {"schema_version":1, "project":"pacsi", "request_id":"<uuid>", "period":"2026-W41", "theme":"őszi séta",
           "language":"hu", "formats":["image:1.91:1","image:1:1","text:rsa"], "counts":{"images":4,"headlines":6,"descriptions":2},
           "constraints":{"avoid":["…"]}}
Válasz:   200 {"status":"ready"|"pending", "request_id":"…", "poll_after_seconds":60,
               "items":[{"kind":"image","ratio":"1.91:1","url":"https://<oldal>/ads/gen/x.jpg","sha256":"…","alt":"…"},
                        {"kind":"rsa","adset":"kviz","headlines":["…"],"descriptions":["…"]}],
               "errors":[]}
Lekérdezés: GET https://<oldal>/ads/request/<request_id>   (ugyanez az alak; pending esetén a poll_after_seconds után)
Visszajelzés: POST https://<oldal>/ads/feedback {"request_id":"…","rejected":[{"id":"…","reason":"…"}]}
```

Szabályok: az `Idempotency-Key` ismétlésére ugyanazt a választ add; a képeket `sha256`-tal és engedélyezett gazdagépen tedd elérhetővé; a motor a visszautasított elemekről megmondja, miért esett ki (`feedback`). **Ha nem akarsz végpontot, ne csinálj: az A) eset elég.**

**Skill a projektben.** Ha a projekt Claude Code-ot használ, készíts egy „google-ads-creative” skillt, amely a brief tényeiből új hirdetésszöveget és (ha van képgeneráló kulcs) új képet készít a `creatives.json` + `img/` alá, lefuttatja a validátort, és csak a hibátlan csomagot teszi élesbe.

## 7. Ellenőrzés – a validátor

```text
# letöltés (egyetlen fájl, Python 3.9+, külső csomag nélkül):
curl -O https://raw.githubusercontent.com/Sabolo100/ads-engine/main/validator/ads_pack_validator.py

python ads_pack_validator.py https://<oldal>/ads/brief.json            # séma + tartalom (szövegek, tények, kulcsszavak)
python ads_pack_validator.py https://<oldal>/ads/brief.json --online   # + nyitóoldalak (UTM megmarad-e?), képek (méret, arány, formátum)
python ads_pack_validator.py ads/brief.json                            # helyi fájlból is
```

Kilépési kód: `0` rendben, `1` hiba van. `--json` géppel olvasható kimenetet ad (CI-hoz). Ugyanazt a szabályt érvényesíti, mint a motor.

**Elfogadási lista** (mind legyen igaz, mielőtt a projekt kész):
- [ ] a validátor `--online` móddal „A csomag megfelel a motor szabályainak”-at ad;
- [ ] minden szám és kényes állítás igazolt tény (és a tények igazak);
- [ ] a nyitóoldalak elérhetők, gyorsak, mobilbarátok, és az átirányítások megtartják az `utm_*`-t;
- [ ] van „bevont” esemény (vagy a projekt nem használ webes mérést, és ezt jelezte az üzemeltetőnek);
- [ ] a csomagban nincs titok, kulcs, személyes adat;
- [ ] a `/ads/` `noindex`, és a fájlok https-en, hitelesítés nélkül elérhetők;
- [ ] a szövegek a projekt valós kínálatát írják le (nincs ígéret, amit a termék nem tud).

## 8. Regisztráció (az üzemeltetőnek kell megadnod)

A motor üzemeltetője a `config/projects.toml`-ba felvesz egy blokkot; ehhez ezek kellenek tőled:

```toml
[[project]]
slug = "<slug>"                                    # a brief project.slug-ja
name = "<márkanév>"
site = "https://<oldal>"
brief_url = "https://<oldal>/ads/brief.json"
customer_id = ""                                   # a Google Ads ügyfélfiók (az üzemeltető hozza létre az MCC alatt)
umami_website_id = "<azonosító vagy üres>"
allowed_hosts = ["<oldal>", "www.<oldal>"]         # a nyitóoldalak és a brief gazdagépe
image_hosts = ["<oldal>"]
```

Ezután az üzemeltető: `python -m ads_engine check` → `plan` → `launch --yes` (szüneteltetett kampány) → ő megadja a heti keretet és `go-live`. A motor addig semmit nem költ.

## 9. Amit a modul nem tehet

- Nem tehet a csomagba kulcsot, jelszót, tokent, személyes adatot, belső címet.
- Nem írhat tényt, amit a termék nem tud; nem használhat versenytárs-nevet; nem ígérhet garanciát, gyógyhatást, árat, amit nem tud tartani.
- Nem teheti szabályozott területre a hirdetést jelzés nélkül (`compliance.category: regulated`).
- Nem írja felül a motor címkézett Google Ads objektumait: a hirdetés-kezelés a motoré, a tartalom a csomagé.

## 10. Változások és verziók

- A csomag szerződése `schema_version`-nel jelölt; a motor csak az ismert verziót fogadja (új verziónál az üzemeltető jelzést kap).
- A brief vagy a creatives változásakor a motor a következő futásnál újra ellenőrzi az élő hirdetéseket: a szabályt sértőket szünetelteti, és levélben jelzi.
- A szabályok bővülnek (pl. új validátor): a legfrissebb validátor mindig a motor repójában van; futtasd újra a csomag változtatásainál.

## Függelék: gyakori hibák

| Üzenet | Teendő |
|---|---|
| `A címben nem lehet felkiáltójel` | vedd ki a „!”-t a címből (a leírásban egy lehet) |
| `A(z) cím „…” számát nem igazolja tény` | add hozzá a számot egy igaz tény `numbers` listájához, vagy írd át a szöveget |
| `…„ingyenesség” állítást tartalmaz, amit nem igazol tény` | vegyél fel igaz tényt `tokens: ["ingyenes"]`-szel, vagy hagyd ki |
| `Tiltott szó a briefben` | írd át a szöveget (vagy a tiltás nem helyes: vedd ki a `forbidden_words`-ből) |
| `Ez a negatív kulcsszó kizárná a(z) „…” kifejezést` | szűkítsd a negatívot (pl. „kutya eladó” a „kutya” helyett) |
| `az átirányítás elveszíti a követő paramétereket` | az átirányítás vigye tovább a `$args`/`$query_string`-et |
| `a megadott arány 1.91:1` | a kép nem 1,91:1: vágd át, vagy javítsd a `ratio` mezőt; a motor egyébként vágja/kitölti |
| `a(z) … gazdagép nincs az engedélyezettek között` | a motor üzemeltetője vegye fel az `allowed_hosts`-ba |
