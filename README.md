# Solartronics ár- és készletfeed

Napi frissülő ár- és készletfeed a [solartronics.de](https://www.solartronics.de) oldaláról.
Termékazonosító kulcs: **cikkszám** (`cikkszam`, az oldalon "Artikel-Nr.").

## Állandó linkek

Ezek a linkek frissítés után sem változnak:

- **JSON:** https://raw.githubusercontent.com/szabviktor-aligvarom/solartronics-feed/main/feed.json
- **CSV:** https://raw.githubusercontent.com/szabviktor-aligvarom/solartronics-feed/main/feed.csv

## Frissítés

GitHub Actions, minden nap **07:30 budapesti idő** (`cron: "30 5 * * *"` = 05:30 UTC).
Kézzel is indítható: Actions fül → "Napi ár- és készletfeed" → "Run workflow".

Publikus repóban a GitHub Actions ingyenes, így az üzemeltetés költsége nulla.
A commit-történet egyben ingyenes árnaplót is ad: minden nap látszik, mi változott.

## Adatforrás

A boltnak **nincs publikus JSON API-ja**. Ellenőrizve:

| Lehetőség | Eredmény |
|---|---|
| Shopify `/products.json` | nincs (HTTP 500) |
| WooCommerce `/wp-json/wc/store/products` | nincs (HTTP 500) |
| Google Shopping / Heureka XML | nincs (9 útvonal, mind 500) |
| Shopware `/api/products` | van, de hitelesítést kér (kulcs nélkül nem elérhető) |
| JSON-LD a termékoldalon | nincs (0 blokk) |

A bolt **Shopware**-en fut. Az adatkinyerés a termékoldalak **schema.org microdata**
jelöléséből történik (`Product`, `Offer`, `PriceSpecification`), ami gépi attribútumokban
adja a cikkszámot, EAN-t, árat, pénznemet, elérhetőséget és súlyt. Ez stabilabb, mint a
vizuális HTML-szerkezetre épülő kinyerés, mert ezeket a mezőket a Google keresőmegjelenés
használja, így a bolt üzemeltetőjének is érdeke változatlanul hagyni.

A termék-URL-ek két forrásból állnak össze: `sitemap.xml` **és** a kategórialisták bejárása.
Erre azért van szükség, mert néhány termék nem szerepel a sitemapben (nincs "szép" URL-je,
csak belső azonosítós), így önmagában a sitemap hiányos feedet adna.

## Mezők

| Mező | Jelentés |
|---|---|
| `cikkszam` | **azonosító kulcs**, egyedi |
| `nev` | terméknév |
| `ar` | aktuális ár, **bruttó kiskereskedelmi** (inkl. MwSt) |
| `netto_ar` | nettó ár |
| `listaar` | áthúzott / listaár (jelenleg mindenhol üres, lásd lent) |
| `akcios` | akciós-e (`igen`/`nem`) |
| `kedvezmeny_szazalek` | kedvezmény %-ban |
| `keszleten_van` | készleten van-e (`igen`/`nem`) |
| `keszlet_statusz` | részletes státusz: raktáron / beszerzés alatt / utánpótlás úton |
| `rendelheto` | kosárba tehető-e |
| `suly_kg` | súly kilogrammban |
| `url` | termékoldal |
| `fokep_url` | főkép |
| `utolso_modositas` | a sitemap `lastmod` értéke |
| `ar_fix` | `nem`, ha az ár csak az alapváltozatra igaz |
| `variansos` | van-e változatválasztó (pl. kábelhossz) |
| `kiszereles` | alapmennyiség, pl. "10 Meter" |
| `tol_ar` | "ab" (-tól) áras termék |
| `ar_tipus` | az ár az oldalról jött, vagy a nettó számított |
| `ean`, `marka`, `duplikalt_cikkszam`, `duplikalt_db` | kiegészítő adatok |

A pénznem a feed fejlécében van (`meta.penznem` a JSON-ben, `# penznem=EUR` a CSV első sorában): **EUR**.

## Tudnivalók az adatról

**Az árak bruttó kiskereskedelmi árak** (végfogyasztói, 19% német ÁFA-val), nem nettó
beszállítói árak. A bolt kétféle árszerkezetet használ: a többségnél kiírja a bruttót és a
nettót is, 60 terméknél viszont csak egy "inkl. MwSt." árat, ott a nettó 19%-kal visszaszámolt.
Az `ar_tipus` mező jelzi, melyik eset áll fenn.

**A készlet nem darabszám, csak státusz.** A bolt nem ad ki mennyiséget. Fontos: a bolt akkor
is engedi a rendelést, ha a termék nincs raktáron, ezért a `rendelheto` szinte minden terméknél
`igen`. A valódi készletinformáció a `keszleten_van` és a `keszlet_statusz` mezőben van.

**Jelenleg nincs akció a boltban.** Nulla áthúzott ár a teljes katalógusban, így a `listaar`,
`akcios` és `kedvezmeny_szazalek` mezők üresek/nemek. A mezők működnek: ha a szállító akciót
indít, a feed automatikusan jelezni fogja.

**66 terméknél az ár nem fix** (`ar_fix = nem`): változatválasztós termékek (jellemzően
méterben mért kábelek), ahol a feltüntetett ár az alapváltozatra vonatkozik, a hosszabb
változat drágább. Ezeket ne árazd be egy az egyben.

## Csendes hiba elleni védelem

A script **hibával leáll és nem írja felül a meglévő feedet**, ha:

- a termékszám 350 alá esik (`MIN_PRODUCTS`)
- a termékek több mint 20%-ánál nincs ár (`MAX_MISSING_PRICE_PCT`)
- az előző futáshoz képest 30%-nál nagyobbat zuhan a katalógus (`MAX_SHRINK_PCT`)

Ilyenkor a workflow commit-lépése nem fut le, tehát **az utolsó jó feed marad kint a linken**.
Az előző futás termékszámát a `state.json` tartja nyilván.

Mindhárom védelem tesztelve: szándékosan elbuktatva a script 1-es kóddal leállt, és a
`feed.json` változatlan maradt.

## Duplikált cikkszámok

Ha ugyanaz a cikkszám többször szerepelne, a **készleten lévő** változat marad meg
(másodsorban a rendelhető, majd a teljesebb adatú). Ilyenkor `duplikalt_cikkszam = igen`
és `duplikalt_db` mutatja, hány változat volt. A cikkszám a feed végén garantáltan egyedi.
