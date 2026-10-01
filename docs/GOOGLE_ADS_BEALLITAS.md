# Beállítás – amit neked kell megcsinálnod (egyszer, kb. 30–40 perc)

Az Ads Engine a Google Ads-et API-n át kezeli, de **három dolgot csak te tudsz megadni**: egy Google Ads fiókot (a pénzed), egy Google Cloud-szolgáltatásfiókot (a motor „belépője”) és a kulcsokat a szervernek. Ez a dokumentum lépésről lépésre végigvezet; a `python -m ads_engine check` parancs bármikor kiírja a **következő hiányzó lépést**, és hivatkozik az itteni számozásra.

> **Soha ne írj be kulcsot, jelszót vagy a JSON-kulcsfájl tartalmát a chatbe** (sem nekem, sem másnak). A kulcsok csak a Coolify titkos környezeti változóiba kerülnek. A **heti keretet** nem most kérem: a go-live előtt külön kérdezem, és te a Google Ads-ben bármikor átállíthatod.

| # | Mit | Hol | Kb. |
|---|---|---|---|
| 1 | Google Ads: kezelői (MCC) fiók + „Pacsit” ügyfélfiók | ads.google.com | 10 perc |
| 2 | Google Cloud: projekt, Google Ads API, hozzáférési szint | console.cloud.google.com | 8 perc |
| 3 | Szolgáltatásfiók + JSON-kulcs | console.cloud.google.com | 5 perc |
| 4 | A szolgáltatásfiók hozzáadása az MCC-hez | Google Ads → Admin | 2 perc |
| 5 | Umami: csak-olvasó felhasználó | stat.pacsit.hu | 5 perc |
| 6 | Kulcsok a Coolify-ba | Coolify | 8 perc |

A végén szólj: **„kész”** – én lefuttatom a `check`-et, a próbát, és létrehozom a **szüneteltetett** kampányt, amit megnézhetsz.

---

## 1. Google Ads: kezelői fiók és a „Pacsit” ügyfélfiók

**Miért MCC?** A szolgáltatásfiókot egyszer, a kezelői (MCC) fiókhoz adod hozzá, így az **összes jelenlegi és jövőbeli ügyfélfiókot** eléri (később: kinaiauto.com, darwinai.hu, polibeli.hu – mindegyiknek külön fiók, külön keret, külön számla).

1. Ha még nincs MCC-d: <https://ads.google.com/home/tools/manager-accounts/> → **Create a manager account** (ingyenes). Ha már van, abba dolgozunk.
2. Az MCC-ben: **Accounts → + → New account**. Név: `Pacsit`. Ország: **Magyarország**. Időzóna: **(GMT+01:00) Budapest**. Pénznem: **HUF** – ⚠ **a fiók létrehozása után nem módosítható**.
3. Ha a Google kampány-létrehozó varázslót kínálja, **hagyd ki** (a kampányt a motor készíti; ha csak „Smart” módot kínál, válaszd az Expert/kampány nélküli utat).
4. **Fizetés:** Billing → fizetési mód. Javaslat: alacsony limitű (vagy külön) bankkártya, hogy egy hiba ne kerüljön sokba. (A motornak saját fékjei vannak, de a kártyalimit a külső háló.)
5. **Hirdetőazonosítás:** a Google kérheti (Admin → Advertiser verification). A `check` kiírja az állapotát; a határidőt tartsd be, különben a hirdetések nem futnak.
6. **Kapcsold ki az automatikus ajánlás-alkalmazást:** Recommendations → Auto-apply → mindent ki. (Különben a Google magától módosítaná a kampányokat, és összeütközne a motorral.)
7. **Kapcsold ki az automatikus címkézést (gclid):** Admin → Account settings → Auto-tagging → kapcsold ki. (A pacsit.hu „személyes adat és süti nélkül” működik; az UTM-paraméterek elegendők a méréshez.)
8. **Ajánlott háló (3 perc):** Tools → Rules → egy kampányszabály: „ha a mai költés nagyobb, mint a napi keret háromszorosa, szüneteltess és küldj e-mailt”. A motornak is van ilyen fékje, ez a független második.
9. **Írd fel két azonosítót** (a kötőjelek nélküli 10 számjegy; nem titkok): az **MCC** azonosítóját (ez lesz a `GADS_LOGIN_CUSTOMER_ID`) és a **„Pacsit” ügyfélfiókét** (ez kerül a `config/projects.toml`-ba, `customer_id`). Ezt a kettőt nyugodtan megírhatod nekem a chatben.

## 2. Google Cloud: projekt, Google Ads API, hozzáférési szint

> **Tudnivaló (2026):** a Google a *developer token*-t megszüntette (2026. szeptember 9.). A hozzáférési szint ma a **Google Cloud-projekthez** kötött, és ott kell kérni. A motor **nem küld** developer tokent.

1. <https://console.cloud.google.com/> → **új projekt** (név pl. `ads-engine`).
   ⚠ **Ha a projekt a céges (arworks.com) szervezetben jön létre**, a szolgáltatásfiók-kulcs létrehozását a szervezeti szabály (`iam.disableServiceAccountKeyCreation`) alapból tiltja. Két megoldás: (a) a szervezet **Org Policy Administrator**-a kivételt ad erre a projektre, vagy (b) a projektet **szervezeten kívül**, saját Google-fiókkal hozod létre. A `check` a 3. lépésnél jelzi, ha ez akadályoz.
2. **Billing:** kösd a projekthez egy éles (nem próbaidős) számlázási fiókot. (A Google Ads API használata ingyenes; a számlázás a projekt működési feltétele.)
3. **APIs & Services → Library → „Google Ads API” → Enable.**
4. **Hozzáférési szint:** a Cloud Console-ban a **Google Ads API → Overview** oldal (a projektben) → *Upgrade access level* → **Apply for access** → **Explorer**.
   Az **Explorer** szint éles fiókokon **napi 2 880 műveletet** enged – a motor ennek töredékét használja –, nincs benne fiók-létrehozás, felhasználókezelés, kulcsszó-tervező és számlázás-API (ezekre nincs szükségünk). A **Basic** szint márka-ellenőrzés után jön, az induláshoz nem kell.

## 3. Szolgáltatásfiók és JSON-kulcs

1. **IAM & Admin → Service Accounts → Create service account.** Név: `ads-engine`. A szerepkör-lépést **hagyd üresen** (a jogosultságot a Google Ads adja, nem a Cloud IAM).
2. Nyisd meg a létrehozott fiókot → **Keys → Add key → Create new key → JSON**. Letöltődik egy `.json` fájl. **Ez a motor jelszava**: ne küldd el, ne másold chatbe, ne tedd repóba.
3. Jegyezd fel a szolgáltatásfiók **e-mail-címét** (`ads-engine@<projekt-azonosító>.iam.gserviceaccount.com`) – a következő lépésben kell. (Az e-mail-cím nem titok.)
4. Ha a „Create new key” szürke vagy hibát ad (*Service account key creation is disabled*), lásd a 2. lépés figyelmeztetését (szervezeti szabály).

## 4. A szolgáltatásfiók hozzáadása az MCC-hez

1. Google Ads → **az MCC-ben** → **Admin → Access and security → Users → +**.
2. Írd be a szolgáltatásfiók e-mail-címét. Hozzáférési szint: **Standard** (elég: kampányt szerkeszthet, felhasználót és számlázást nem). Az „Email only” szint szolgáltatásfióknál nem támogatott.
3. **Add account.** Meghívót elfogadni nem kell: a hozzáférés azonnal él.

## 5. Umami: csak-olvasó felhasználó

A motor azt méri, hogy a hirdetésből jövő látogatók közül hányan használják valóban az appot (a „bevont” esemény). Ehhez az Umami-ból **csak olvas**. Az önhosztolt Umami (v3.0.x) megosztási linkje az API-hoz nem használható, ezért **felhasználónév + jelszó** kell, **csak olvasási joggal**:

1. `https://stat.pacsit.hu` → **Settings → Users → Create user**: felhasználónév `ads-engine`, erős jelszó, szerepkör **User** (nem admin).
2. **Settings → Teams → Create team** (pl. `Ads Engine`), majd add hozzá a felhasználót **View only** szereppel.
3. **Websites → pacsit.hu → Edit → Data → Transfer** → a csapatnak. (Csapat-tulajdonosként te továbbra is mindent látsz; a webhely azonosítója nem változik.)

> A webes adat csak akkor tartalmaz „bevont” eseményt, ha a **Pacsi app 1.9.0** (bevont esemény + UTM-t megtartó nyitóoldal-útvonalak) élesben van. Ezt a go-live előtt külön jóváhagyatom veled.

## 6. Kulcsok a Coolify-ba

Coolify → az **Ads Engine** alkalmazás → **Environment Variables** (futásidejű, **nem** build-idejű; a titkokat jelöld meg titkosként). Én az alkalmazást létrehozom, a **titkokat te** írod be – vagy ha szeretnéd, egy kis parancs beteszi őket a saját termináloddal; chatbe soha nem kérek kulcsot.

| Változó | Érték | Titok? |
|---|---|---|
| `ENGINE_MODE` | `dry` (az éles indulásig; **ne állítsd `live`-ra**, amíg nem beszéltük meg) | nem |
| `GADS_SA_JSON_B64` | a JSON-kulcsfájl **base64-ben** (lásd lent) | **igen** |
| `GADS_LOGIN_CUSTOMER_ID` | az MCC azonosítója, kötőjel nélkül | nem |
| `ANTHROPIC_API_KEY` | Anthropic API-kulcs (<https://console.anthropic.com/> → API keys; érdemes havi költési limitet állítani) | **igen** |
| `OPENAI_API_KEY` | az új AI-képekhez (gpt-image-2; hetente legfeljebb 10 kép, kemény korlát). A Search kép-bővítményhez a Google legalább 60 napos fiókot kér, ezért az első hetekben nem használja; elhagyható, és később is pótolható. Az OpenAI-fiókban érdemes havi költési limitet állítani | **igen** |
| `UMAMI_URL` | `https://stat.pacsit.hu` | nem |
| `UMAMI_USER` / `UMAMI_PASSWORD` | az 5. lépés felhasználója | a jelszó **igen** |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` | a `hello@pacsit.hu` meglévő SMTP-beállítása | a jelszó **igen** |
| `REPORT_TO` | a címzett(ek), vesszővel (a te e-mail-címed) | nem |
| `HEARTBEAT_URL` | *(opcionális)* külső figyelő (pl. healthchecks.io) ping-címe; a napi szinkron után hívja | igen |

**A kulcsfájl base64-be alakítása** (a kimenet egyetlen hosszú sor; a vágólapra kerül, onnan illeszd be a Coolify-ba):

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\Users\TE\Downloads\ads-engine-kulcs.json")) | Set-Clipboard
```

A beillesztés után **töröld a letöltött `.json` fájlt** a gépről (vagy tedd a jelszókezelődbe): a Coolify-ban lévő változat elég.

---

## Ellenőrzés

Bármikor futtathatod (a Coolify-alkalmazás termináljában, vagy ha lokálisan állítottad be a változókat, a saját gépeden):

```bash
python -m ads_engine check
```

Minden sor `✓` (rendben), `!` (figyelmeztetés), `→` (a te lépésed hiányzik) vagy `✗` (hiba). A kimenet végén a **KÖVETKEZŐ LÉPÉS** mondja meg, mi a következő teendő; a lépések száma ennek a dokumentumnak a számozását követi.

## Gyakori hibák

| Jelenség | Ok és teendő |
|---|---|
| `USER_PERMISSION_DENIED` | A szolgáltatásfiók nincs hozzáadva az MCC-hez (4. lépés), vagy a `GADS_LOGIN_CUSTOMER_ID` hibás (kötőjel nélküli, az **MCC** azonosítója legyen). |
| `invalid_grant` / `OAUTH_TOKEN_INVALID` | A JSON-kulcs hibás, törölt, vagy a base64 átalakítás sérült (egy sor legyen, szóköz nélkül). Új kulcs: 3. lépés. A gép órája legyen pontos. |
| 403 „Google Ads API has not been used in project…” | A *Google Ads API* nincs engedélyezve a projektben (2. lépés, 3. pont). |
| 403 hozzáférési szint / „developer token” | Az **Apply for access** (Explorer) még nincs kész (2. lépés, 4. pont). |
| A kulcs nem hozható létre | Szervezeti szabály (2. lépés, 1. pont). |
| Az Umami 401 / 404 | Rossz felhasználónév/jelszó, vagy a webhely nincs a csapatban (5. lépés). |
| Nem jön levél | Az `SMTP_*` változók és a `REPORT_TO`; a `python -m ads_engine weekly --mail --no-ai` kiírja a küldési hibát. |

## Mi történik ezután

1. Én lefuttatom: `check` → `plan` (a kampányfa összegzése, a Google-be nem ír) → `launch` (dry: csak próba) → éles, **szüneteltetett** kampány – ezt megnézheted a Google Ads-ben, **semmi nem fut és nem kerül pénzbe**.
2. Megkérdezem a **heti keretet** (a Google Ads-ben utólag is bármikor szerkesztheted).
3. A `go-live` a te jóváhagyásodra kapcsolja be a kampányt; utána 14 napig csak megfigyel, majd a heti kör módosít. A részleteket a `docs/MUKODES.md` írja le.
4. **Képek:** új fiókon a Google a Search kép-bővítményt (a hirdetés melletti képet) csak **60 nap** után engedi (jó szabályzati előzmény, aktív szöveges hirdetés és költés kell). A kampány addig képek nélkül fut – ez nem hiba –, és a motor magától pótolja őket, amint a Google engedi. Szöveg és logó a képen nem megengedett.
