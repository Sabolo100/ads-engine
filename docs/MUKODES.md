# Üzemeltetés – mit csinál a motor, mikor, és mit kell tenned

Az Ads Engine a szerveren, a laptop nélkül fut (Coolify, egy Docker-konténer). Ez a dokumentum azt írja le, **mit csinál magától**, **mire nem képes soha kérdés nélkül**, és **mi a te dolgod**. A beállítást a `docs/GOOGLE_ADS_BEALLITAS.md` tartalmazza.

## 1. Napirend

Minden időpont `Europe/Budapest`. Az ütemező percenként léptet; a feladatok **idempotensek** (egy időszakban egyszer „kész”), újraindítás után a hiányzót pótolják (a napi szinkront aznap, a heti kört a hét végéig).

| Mikor | Feladat | Mit csinál |
|---|---|---|
| naponta 06:30 | `sync` | tegnapi költés és keret ellenőrzése (fékek), hirdetések jóváhagyási állapota, **kézi módosítás észlelése**, a projekt briefjének változása, nyitóoldal. **Nem optimalizál.** |
| hétfőn 07:00 | `weekly` | kiértékelés (múlt hét + trend, Google + Umami) → javaslatok → szabályok → végrehajtás → **magyar heti levél**. Csak akkor indul, ha aznap a napi szinkron lefutott. |
| óránként | `guard` | a nyitóoldal elérhetősége (az UTM megmarad-e); két egymás utáni hibára szünetelteti a kampányt |
| havonta | `apicheck` | a Google Ads API verziójának élete: lejárat, újabb főverzió, változott leíró |
| havonta, az 1. napon 07:30 után | `monthly` | **havi terv**: a hónap témája, új kulcsszavak, hirdetési szempontok, kísérletek, tanulságok → levél (a napi szinkron után; pótolható a hónap végéig) |

Hiba esetén a feladat 30 perc múlva újrapróbálkozik (időszakonként legfeljebb három próbálkozás), és **levelet küld** (az utolsó sikertelen próbáról külön). Egyszerre egy példány dolgozhat (zár a `/data` kötetben), így a Coolify frissítésekor sem fut dupla.

## 2. A heti kör: mit módosíthat a motor

**Zárt műveletkészlet** – minden más tiltott és naplózott. Az AI csak *javasol*; a döntést a **kódban lévő** szabályok szűrik (nem promptban).

| Művelet | Feltétel (kódban) | Korlát |
|---|---|---|
| **Negatív kulcsszó** a keresési kifejezések alapján | az AI irrelevánsnak ítéli a kifejezést; a negatív (≤ 4 szó) szó szerint benne van a megfigyelt kifejezésben; **nem zárhatja ki** a magkifejezést, a pozitív kulcsszavakat és egy másik, releváns keresést; még nincs meg | 15 / hét |
| **Kulcsszó szüneteltetése** | ≥ 25 kattintás, ≥ 5 webes látogatás és **nulla bevont** látogatás (Umami); nem magkifejezés; nem az utolsó aktív a csoportban; nincs „kézben” | 3 / hét |
| **Hirdetésszöveg-csere** | a Google legalább 3 szöveget gyengének (LOW) jelez; az új szöveg átmegy minden validátoron (hossz, tiltott szó, **tények**); új RSA készül, a régi szünetel; a kitűzött szöveget nem cseréli | 2 / hét, hirdetéscsoportonként 14 naponta |
| **Elutasított hirdetés** | szöveg okú elutasításnál új RSA a kifogásolt szöveg nélkül (60 napon belül legfeljebb 2 kísérlet csoportonként), utána a hirdetés szünetel; nem szöveg okú ok (nyitóoldal, szabályzat) esetén csak szól | – |
| **Brief-változás** | ha a projekt briefje megváltozott (új tiltott szó, tény, versenytárs-márka), az élő hirdetéseket és kulcsszavakat újraellenőrzi; a szabálysértők szünetelnek | – |
| **Új kulcsszó** (havi terv) | az AI javasol; a kód csak MEGLÉVŐ hirdetéscsoportba engedi: ≤ 10 szó, legalább két szó, a magkifejezésekhez közel, nem tiltott szó, nem versenytárs, nem ütközik negatívval, nem duplikátum, a csoport nincs „kézben” | 10 / hónap |
| **Képek** (kreatív-gyár) | a projekt saját képei (ha az indításkor nem fértek fel), ingyenes vágásváltozatok, új AI-kulcsvizuálok – csak ha a Google már engedi (lásd lent), és csak képnézés után | AI-kép: `max_images_per_week` (alap 10), a motor heti célja 3; ≤ 15 bekapcsolt kép / kampány |

**Megfigyelési időszak:** a go-live után **14 napig** csak a biztonsági fékek és az elutasított hirdetések javítása működik; a kulcsszavakhoz és a szövegekhez nem nyúl.

**Soha, kérdés nélkül:** a keret emelése · ország vagy nyelv bővítése · törlés (csak szüneteltetés) · fiók- és számlázási beállítás · nem a motor saját kampánya (minden saját kampánya az `ads-engine` címkét viseli) · amit **ember** szüneteltetett · versenytárs-márkanév · a briefben nem szereplő tényállítás.

**Ha te nyúlsz hozzá:** a motor a Google változás-eseményeiből és az utolsó ismert állapotból észleli, ha ember (vagy automatikus szabály/ajánlás) módosított egy objektumot. Az ilyen objektum **28 napig „kézben van”**: a motor nem írja felül, és szól. Ha a *kampányt* módosítod, az egész kampányt békén hagyja; a **keret** módosítása nem számít ilyennek (azt átveszi).

Hogy a motor milyen számokat mér, és miért nem konverziót: a süti nélküli oldal ígérete miatt a minőség-őr a **költség / bevont látogatás** az Umami-ból (`bevont` esemény). Az olcsó, de azonnal visszapattanó kattintást nem hajszolja.

### Képek – amit a Google szabálya megszab

A Search kampány **kép-bővítményére** a Google szigorú szabályokat ír, és ezek az ötletünket is alakítják:
- **Szöveg, felirat és logó nem lehet a képen** (a Performance Max kivétel, a Search nem), utólag szerkesztett kollázs vagy keret sem, és a kép nem lehet elmosott, életlen, torz vagy rosszul vágott. Ezért a motor **képre szöveget nem tesz** – a szövegváltozat a hirdetésszövegekben (RSA) készül –, a képeknél a **vágás/méret** és az új, **szövegmentes kulcsvizuál** a változó.
- A fióknak legalább **60 naposnak** kell lennie, jó szabályzati előzménnyel, aktív szöveges hirdetéssel és az elmúlt 30 napban költéssel. Ezért az új fiókon a kép-bővítmény eleinte nem működik: a motor a kampányt képek nélkül hozza létre (ez nem hiba), és a heti körben **egy `validateOnly` próbával megnézi, hogy a fiók már jogosult-e**. Amíg nem az, semmit nem készít és **AI-képre sem költ**.

A gyár forrásai: (1) a projekt saját képei (védettek: soha nem szünetelnek; ha az indításkor nem fértek fel, a heti kör pótolja), (2) ingyenes vágásváltozatok ugyanabból a képből (legfeljebb 30 % vágással, elmosott kitöltés nélkül), (3) új AI-kulcsvizuálok (`gpt-image-2`, a brief `brand.image_style_prompt` mezőjével).
- **AI-kép költsége és kerete:** hetente legfeljebb a projekt kerete (`max_images_per_week`, alap 10; a motor heti célja 3). A számláló az OpenAI-hívás **előtt** nő, a sikertelen kérés is beleszámít, kulcs- vagy keret-hibánál a motor leáll. **Próbaüzemben (`dry`) AI-képet nem kér.**
- **Képnézés:** minden AI-képet Claude néz át (szöveg vagy logó a képen, rajzolási hiba, elmosottság, túl sok üres felület, tiltott tartalom); ellenőrző (`ANTHROPIC_API_KEY`) nélkül AI-kép **nem kerül fel**.
- **Kapacitás:** kampányonként legfeljebb 15 bekapcsolt kép. A helyet a leggyengébb kattintási arányú, **a motor által készített**, legalább 14 napos, elég megjelenést látott kép **szüneteltetése** adja (törlés soha, a projekt saját képe soha).

## 3. Biztonsági fékek

A Google napi kerete **átlag**: egy nap akár a duplát is költheti, havonta legfeljebb napi × 30,4-et számláz. A „heti keret” ezért **7 × a napi keret, nem kemény plafon**. A fékek a valós kockázatot figyelik, és **dry üzemmódban is élesek** (csak csökkenthetnek költést).

| Védelem | Feltétel | Mit tesz |
|---|---|---|
| Túlköltés | a tegnapi költés > 2,1 × a napi keret | kampányszünet + azonnali levél |
| Havi határ | az elmúlt 30 nap költése > 1,10 × a rögzített napi keretek összege | kampányszünet + levél |
| Elírás-védelem | a keret ≥ 2 × a jóváhagyott (egy extra nulla ne kerüljön pénzbe) | kampányszünet + megerősítés kell |
| Keretváltozás | kisebb emelés vagy csökkentés | átveszi, a heti levélben jelzi |
| Nincs go-live | a kampány fut, de nincs jóváhagyott keret | szünet |
| Nyitóoldal | két egymás utáni óránkénti ellenőrzés sikertelen | szünet + levél; két jó ellenőrzés után **magától visszakapcsol** (ha nem keret-fék szüneteltette) |
| `STOP` fájl | a `/data/STOP` létezik | a motor semmit nem ír, csak a fékek futnak |

Fék után a visszakapcsolás **a te döntésed**: nézd meg a levelet és a keretet a Google Ads-ben, majd `python -m ads_engine confirm-budget --yes --enable`. (`--yes` nélkül nem történik semmi; `--enable` csak a **fék által** szüneteltetett kampányokat kapcsolja vissza, azt, amit te szüneteltettél, soha.)

## 4. Levelek

- **Heti jelentés (hétfő):** összefoglaló (az AI számok nélküli értelmezése; ha nem elérhető, kódból írt), számok az előző héttel, keret, **a te teendőd**, mit csinált a motor, mit javasolt de a szabályok nem engedtek, legjobb kulcsszavak, keresések, hirdetések, jegyzetek, lábléc (verzió + build). A teljes jelentés a `/data/reports/` alatt is megvan (JSON, szöveg, HTML).
- **Azonnali levelek:** FÉK · kézzel módosított objektum · elutasított hirdetés · nyitóoldal nem elérhető · az Ads Pack hibás · a brief miatt szünet · feladat-hiba · API-figyelmeztetés. Ugyanaz az üzenet napokig nem ismétlődik.
- Próbaüzemben (`dry`) a tárgy `[PRÓBA]` előtagot kap, és a levél kimondja, hogy nem történt módosítás.

## 5. Mi a te dolgod

| Mit | Hogyan |
|---|---|
| **Keret** | a Google Ads-ben bármikor szerkesztheted; a motor átveszi. Kétszeres vagy nagyobb emelésnél megerősítést kér. A motor a keretet **soha nem emeli**. |
| **Szünet** | a Google Ads-ben bármikor; a te szüneteltetésed felülír mindent, és a motor nem kapcsolja vissza. |
| **Vészleállítás** | `python -m ads_engine stop` (STOP fájl); újraindítás: `resume`. |
| **Go-live** | `python -m ads_engine go-live --weekly-budget <összeg> --yes` (csak `ENGINE_MODE=live` mellett): egy lépésben beállítja a keretet és bekapcsolja a kampányt. A keretet előtte külön megbeszéljük. |
| **Irány és tiltások** | a projekt `brief.json`-jában (`/ads/brief.json`); a motor naponta figyeli, és a változást a már futó hirdetéseken is érvényesíti. |
| **Fék után** | `confirm-budget --yes --enable` (lásd a 3. pontot). |

## 6. Parancsok

```
python -m ads_engine --version                         verzió és build-azonosító
python -m ads_engine check [--offline]                 beállítás-ellenőrzés + a KÖVETKEZŐ HIÁNYZÓ LÉPÉS
python -m ads_engine status                            üzemmód, keret, go-live, utolsó szinkron és jelentés, zár, STOP
python -m ads_engine plan                              az Ads Pack letöltése és ellenőrzése, a kampányfa összegzése (nem ír)
python -m ads_engine launch [--yes]                    szüneteltetett kampány létrehozása (dry: csak próba)
python -m ads_engine go-live --weekly-budget N --yes   a keret beállítása és a kampány bekapcsolása
python -m ads_engine sync                              a napi szinkron és a védelmek azonnal
python -m ads_engine weekly [--mail] [--no-ai] [--yes]  a heti kör azonnal (élesben módosít, ezért --yes kell); --mail: levél is megy
python -m ads_engine monthly [--mail] [--yes]          a havi terv azonnal (élesben új kulcsszavakat ír, ezért --yes kell)
python -m ads_engine report                            az utolsó heti jelentés
python -m ads_engine confirm-budget --yes [--enable]   a Google Ads-ben látható keret elfogadása fék után
python -m ads_engine stop | resume                     a STOP fájl létrehozása / törlése
python -m ads_engine tick                              az esedékes feladatok egyszeri futtatása
python -m ads_engine serve                             a szolgáltatás (a konténer fő parancsa): ütemező + /healthz
python -m ads_engine healthcheck                       a helyi /healthz lekérdezése (Docker)
python -m ads_engine api-check                         a Google Ads API leíró frissítése, lejárat, újabb verzió
```

Üzemmód: `ENGINE_MODE=dry` (alap) mellett a motor **minden módosítást a Google `validateOnly` próbájával ellenőriz, de nem ír**; `live` mellett ír. A kézi parancsok (`sync`, `weekly`) a `serve` mellett is futtathatók a konténer termináljában: a zár miatt egyszerre csak az egyik dolgozik.

## 7. Adatok és napló

Minden a `/data` kötetben van (a Coolify tartós tárolója; ezt érdemes menteni):

| Fájl | Tartalom |
|---|---|
| `engine.sqlite` | a motor állapota: jóváhagyott keret, go-live dátum, futások (`runs`), **minden írás előtte/utána értékkel** (`actions`), feladatok (`jobs`), számlálók, az AI-hívások nyers kimenete (`llm_log`) |
| `reports/<projekt>-<hét vége>.{json,txt,html}` | a heti jelentések |
| `plans/<projekt>-<hónap>.{json,txt,html}` | a havi tervek |
| `creatives/<projekt>/` | a motor által készített képek (a nyilvántartás az `engine.sqlite`-ban van: forrás, képnézés, állapot) |
| `cache/` | a Google Ads API leírója |
| `STOP` | ha létezik, a motor nem ír |

Visszavonás külön parancs nincs: a visszavonás a te szüneteltetésed + a Google Ads előzményei + az `actions` napló. Ha a `/data` kötet **elveszik**, a motor nem ismeri a jóváhagyott keretet, ezért **szüneteltet** (biztonságos irány); a helyreállítás: ellenőrizd a keretet a Google Ads-ben, majd `confirm-budget --yes --enable`.

A naplósorok JSON-ban a konténer kimenetére mennek (Coolify → Logs); a titkokat a napló kitakarja.

## 8. Verzió és frissítés

A verzió egy helyen él (`VERSION`), és látszik a `--version`-ben, a heti levél láblécében és a `/healthz` válaszában, mellette a build-azonosító (a kód tartalom-hash-e). **Nincs automatikus deploy:** a frissítést (push után) kézzel indítjuk a Coolify API-n át. A `/data` kötet frissítéskor megmarad. Változásnapló: `CHANGELOG.md`.

## 9. Hibakeresés

| Jelenség | Merre nézz |
|---|---|
| Nem jön a heti levél | `status` (utolsó jelentés: „a levél NEM ment el”?), az SMTP-változók; a jelentés a `/data/reports/` alatt megvan; a levél nélkül maradt jelentést a motor 30 percenként, legfeljebb háromszor újraküldi (a kiértékelést nem futtatja újra) |
| A kampány szünetel | `status`: ⚠ sor; a levél megmondja az okot; `confirm-budget --yes --enable` |
| „Kézben van” üzenet | a motor észlelte a módosításodat; 28 napig nem nyúl az objektumhoz – ez szándékos |
| `check` hibát ír | a **KÖVETKEZŐ LÉPÉS** sor megmondja, melyik lépés hiányzik (`docs/GOOGLE_ADS_BEALLITAS.md`) |
| A heti kör nem módosít | megfigyelési időszak (14 nap), nincs Umami-adat, nincs elég kattintás, vagy a heti korlát elfogyott – a jelentés „Amit javasoltam, de a szabályok nem engedtek” része megmondja az okot |
| Nincsenek képek a kampányban | a jelentés „Képek” jegyzete megmondja: a Google még nem engedi a kép-bővítményt (új fiók: 60 nap, aktív szöveges hirdetés, költés az elmúlt 30 napban), vagy próbaüzem, vagy hiányzik az `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` az AI-képekhez |
| Az AI-kép elutasítva | a képnézés oka a jelentésben (szöveg a képen, rajzolási hiba…); a kérés pénzbe került, a heti keretből levonódott. Ha gyakori, a brief `brand.image_style_prompt` mezőjét pontosítsd |
| A havi terv nem készült el | `ANTHROPIC_API_KEY` hiányzik, vagy a brief hibás; `python -m ads_engine monthly` kiírja az okot |
| `/healthz` 503 | az ütemező-ciklus 10 perce nem lépett: a konténer újraindítása segít; a Coolify naplója mutatja az okot. `?strict=1`: éles kampány mellett 36 órája nem volt sikeres napi szinkron |

**Őszintén a korlátokról:** a motor a kattintások és a webes minőség szerint javít, de **eredményt nem garantál**; kis keretnél kevés az adat, ezért a szabályok óvatosak (14 napos megfigyelés, minimális kattintásszám, heti korlátok). A Google új fiókot és hirdetőt napokig ellenőrizhet, a hirdetések ez alatt nem futnak.
