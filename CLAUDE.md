# Assistente ricambi multi-marca — contesto di progetto

Chatbot per un'azienda di ricambi di macchine agricole. Il cliente descrive
la sua macchina e il ricambio che cerca, in linguaggio naturale, e l'LLM
(via tool calling) interroga i cataloghi dei fornitori collegati e risponde
con i codici trovati. Interfacce: terminale (`bot.py`), web/widget
(`server.py` + `web/widget.js`).

Ambiente: Windows. Per lanciare Python usare **`py`**, non `python`. Il
progetto ha un virtualenv in `./venv` (`./venv/Scripts/python.exe`). Non è
(ancora) un repository git.

## Architettura

```
bot.py / server.py        interfacce (terminale / web), sottili
agente.py                 motore di conversazione: prompt, tool, ciclo LLM
config.py                 percorsi e impostazioni COMUNI (non di marca)
fornitori/
  __init__.py              Registro: scopre le marche, espone i 4 tool
  base.py                  interfaccia Fornitore che ogni marca implementa
  testo.py                 normalizzazione testo per la ricerca (comune)
  goldoni/                 catalogo statico scaricato in locale (SQLite)
  sdf/                     portale live SAME/Deutz-Fahr/Hurlimann/Lambo
web/                       widget JS per siti esterni + demo.html
debug/                     script di analisi una tantum (non fanno parte del bot)
```

### Principio fondamentale: astrazione dalla marca

**Tutto ciò che sta fuori da `fornitori/<marca>/` non deve sapere nulla di
marche specifiche.** `agente.py`, `config.py`, `server.py`, `bot.py` devono
funzionare identici che ci siano 2 marche o 10. Il test per ogni modifica:
*"per aggiungere una nuova marca, basta creare `fornitori/<nuova>/` con un
`crea()` e una classe che estende `Fornitore`? O devo toccare anche altri
file?"* Se la risposta è "devo toccare altro", è una violazione
dell'astrazione — è successo piu' volte in questa sessione (vedi changelog)
ed e' il tipo di bug a cui fare piu' attenzione in futuro.

Meccanismo: `fornitori/__init__.py` scopre le sottocartelle di `fornitori/`
che espongono `crea()`, le carica in un `Registro`, ed espone solo 4
funzioni all'LLM (`elenca_marche`, `trova_macchina`, `scegli_macchina`,
`cerca_ricambio`) che il `Registro` smista al fornitore giusto. Ogni marca
implementa `Fornitore` (`fornitori/base.py`): `marca`, `etichetta`, `alias`
(nomi/sinonimi con cui i clienti la chiamano — es. SDF ha alias `same`,
`deutz`, `lamborghini`, `hurlimann`...), `disponibile()`, `trova_macchina()`,
`scegli()`, `cerca_ricambio()`, `stato_preparazione()`.

`agente.py` costruisce la mappa alias→marca **dinamicamente leggendo
`registro.fornitori[...].alias`** (funzione `_costruisci_alias`), non da un
dizionario cablato — così un nuovo fornitore che dichiara i propri alias
viene riconosciuto senza toccare `agente.py`.

### Stato per sessione (importante, causa di bug se ignorato)

`FornitoreSdf` e `FornitoreGoldoni` **non sono stateless**: tengono "quale
macchina ho scelto" come attributi di istanza (`self.model_id`,
`self.catalogo`, `self.nome_macchina`, ecc.), non nella `Conversazione`.
Questo significa che **ogni sessione/conversazione deve avere il proprio
`Registro`** (quindi le proprie istanze `FornitoreXxx`) — condividerle tra
conversazioni diverse farebbe scrivere a due clienti concorrenti sulla
stessa variabile, con l'effetto che uno vede i ricambi della macchina
scelta dall'altro. `server.py` lo fa correttamente (un `Registro` per
sessione in `conversazione()`); NON "ottimizzare" questo aspetto senza
ripensare a fondo l'interfaccia `Fornitore` (andrebbe scorporato lo stato
"macchina scelta" dall'oggetto fornitore condiviso).

Le risorse davvero costose (crawl dei cataloghi, cache SQLite) **sono già**
condivise correttamente a livello di modulo negli adapter (dizionari
`_IN_CORSO`/`_PROGRESSO` globali in `fornitori/goldoni/adapter.py` e
`fornitori/sdf/adapter.py`), indipendentemente da quante istanze
`FornitoreXxx` esistono — quindi duplicare le istanze per sessione non
duplica il lavoro pesante.

## Changelog di questa sessione (cosa e' stato sistemato, e perche')

1. **`agente.py`**: rimosso il dizionario `GRUPPI` cablato (marca→alias).
   Ora `_costruisci_alias(registro)` lo costruisce leggendo
   `fornitore.alias` da ogni marca caricata. `marca_canonica`/
   `marca_dichiarata` sono diventati metodi di `Conversazione`
   (`_marca_canonica`, `_marca_dichiarata`).
2. **`fornitori/base.py`**: aggiunto `alias = ()` all'interfaccia.
3. **`fornitori/sdf/adapter.py`**: aggiunto
   `alias = ("same", "deutz", "deutz-fahr", ...)` a `FornitoreSdf`.
4. **`config.py`**: rimossi `DB_GOLDONI`, `COOKIE_SDF`, `leggi_cookie()`
   (specifici di marca, vivevano nel file condiviso). Aggiunta
   `leggi_credenziale(nome_file, variabile_ambiente)`, generica: prima la
   env var, poi il file in `dati/`.
5. **`fornitori/goldoni/db.py`**: `DB` ora calcolato localmente come
   `config.DATI / "goldoni.db"` invece che da `config.DB_GOLDONI`. Aggiunto
   `PRAGMA journal_mode = WAL` (Goldoni non l'aveva, SDF sì) per tollerare
   meglio letture/scritture concorrenti quando piu' sessioni sono attive.
6. **`fornitori/sdf/adapter.py`** + **`fornitori/sdf/db.py`**: il percorso
   del DB SDF era relativo alla cartella di lancio
   (`"./fornitori/sdf/sdf.db"`), in contraddizione con la regola dichiarata
   in `config.py` (percorsi calcolati dalla posizione del file, non dalla
   cwd). Ora `DEFAULT_PATH` in `db.py` usa
   `Path(__file__).resolve().parent / "sdf.db"`; `adapter.py` non duplica
   piu' il percorso, usa il default di `Db()`.
7. **`fornitori/sdf/cli.py`**: usa `config.leggi_credenziale(...)` invece
   di `config.COOKIE_SDF`; rimosso `import os` inutilizzato.
8. **`server.py`**: endpoint `/api/preparazione` usava
   `fornitori.get("sdf")` cablato (era gia' segnalato nel codice con un
   commento "! ! ! MODIFICARE..."). Ora usa
   `sess["registro"].stato_preparazione()`, gia' generico e usato altrove
   nello stesso file. Corretto anche un commento fuorviante su
   `REGISTRO_BASE` che diceva "condiviso" quando non lo e' (vedi sezione
   sopra sullo stato per sessione).
9. **`fornitori/goldoni/adapter.py`**, `trova_macchina`: due bug legati
   — (a) `richiede_conferma` era commentato, quindi il bot non chiedeva
   mai conferma della macchina selezionata per Goldoni (a differenza di
   SDF); (b) quando piu' serie diverse condividono lo stesso numero di
   modello (es. "3050" compare sia nella serie "3000" che in "Star 3000 V"
   che in "Star 3000" — vedi screenshot catalogo Goldoni discusso in
   sessione), la selezione automatica sceglieva quella con punteggio piu'
   alto **tra quelle gia' scaricate in cache**, ignorando eventuali pari
   merito non ancora scaricati — un pareggio reale che appariva come
   scelta certa. Ora `richiede_conferma` e' ripristinato, e si controlla
   il pareggio sull'intero elenco `tutti` (scaricati e non), non solo su
   `scaricati`.
10. **`agente.py`**, prompt di sistema: aggiunta sezione "SE IL CLIENTE
    USA UN TERMINE IMPRECISO, DIALETTALE O SBAGLIATO" — il modello deve
    usare la propria conoscenza meccanica generale per interpretare gergo
    (es. "bulbo" per un sensore), dichiarare l'interpretazione fatta
    ("ho cercato X, intendevi questo?"), e se non trova nulla chiedere al
    cliente di descrivere il pezzo (funzione, posizione, aspetto) invece
    di dire subito che non esiste.
11. **`agente.py`**, `Conversazione.turno()`: aggiunto un guardrail a
    livello di codice (`self.attesa_conferma`), perche' si e' osservato in
    un test reale che il modello (gemini-3.5-flash-lite) **ignora** il
    flag `richiede_conferma: true` restituito da `trova_macchina` e chiama
    comunque `scegli_macchina`/`cerca_ricambio` nello stesso turno, senza
    mai chiedere conferma al cliente (bug osservato: "ho un 3000" ha
    portato a scegliere in automatico un modello SDF completamente
    diverso, "SAME Buffalo 120", solo perche' il suo nome interno conteneva
    la stringa "3000"). Ora, quando `trova_macchina` restituisce un esito
    non certo (`richiede_conferma`, `piu_marche`, o candidate senza una
    `selezionata`), il codice **rifiuta** di eseguire `scegli_macchina`/
    `cerca_ricambio` nello stesso turno e restituisce un'istruzione che
    obbliga il modello a fermarsi e presentare le alternative, aspettando
    la risposta del cliente nel turno successivo (`self.attesa_conferma`
    si azzera a ogni nuovo messaggio).

## Cose note ma NON ancora fatte

- **Tabella di apprendimento sui termini imprecisi** (es. "bulbo" →
  "sensore pressione olio"): idea discussa (vedi guida dettagliata sotto),
  in attesa che l'utente ne parli con l'azienda prima di decidere come
  strutturarla e se renderla automatica o curata a mano.
- **Sfruttare le relazioni gia' presenti nei DB** (gruppi/sottogruppi e
  sostituzioni in `sdf.db`, gruppi/tavole in `goldoni.db`) per risposte
  piu' ricche ("altri modelli che usano questo ricambio", "ricambio
  sostitutivo"): idea discussa (vedi guida dettagliata sotto), nessuna
  implementazione ancora.
- **Estrazione ricambi dai cataloghi PDF** (linee Goldoni senza catalogo
  navigabile: ITMA, TYM, Lovol) per popolare `goldoni.db` una volta sola
  e non dipendere piu' dal sito per quelle linee: idea discussa (vedi
  guida dettagliata sotto), non ancora implementata.
- **Deploy**: non ancora fatto. Vercel e' stato scartato (vedi sotto),
  piattaforma consigliata: Railway (o Render/Fly.io in alternativa) — un
  host con processo sempre acceso e disco persistente, non serverless.

## Deploy: perche' non Vercel

Vercel e' **serverless**: ogni richiesta gira in una funzione che si
spegne subito dopo (o entro un timeout breve), non un processo persistente.
Il progetto invece si basa su:
- **thread in background** che continuano a lavorare minuti dopo che la
  richiesta HTTP e' gia' tornata (crawl cataloghi Goldoni/SDF,
  `_scarica_in_background`/`_assicura_catalogo`) — su serverless verrebbero
  uccisi a meta';
- **stato in memoria tra richieste** (`SESSIONI` in `server.py`,
  `_IN_CORSO`/`_PROGRESSO` negli adapter) — su serverless puo' sparire tra
  un'invocazione e l'altra;
- **SQLite su disco locale** (`dati/goldoni.db`, `fornitori/sdf/sdf.db`) —
  il filesystem delle funzioni Vercel e' di sola lettura tranne `/tmp`, che
  e' temporaneo.

Piattaforma consigliata: **Railway** (processo sempre acceso, volumi
persistenti, deploy da git, variabili d'ambiente via dashboard). In
alternativa Render o Fly.io. Checklist per il deploy:
- comando di avvio SENZA `--reload`, tipo
  `uvicorn server:app --host 0.0.0.0 --port $PORT`;
- volume persistente per `dati/goldoni.db` e `fornitori/sdf/sdf.db` (non
  devono azzerarsi a ogni redeploy);
- `dati/goldoni.db` gia' popolato va portato online (committato o caricato
  una volta sul volume), altrimenti si parte con catalogo vuoto;
- segreti (`GEMINI_API_KEY`, `SDF_COOKIE`, `SDF_USERNAME`/`SDF_PASSWORD`)
  come variabili d'ambiente della
  piattaforma, MAI committati — `config.leggi_credenziale`/
  `os.environ.get` gia' preferiscono le env var ai file;
  ATTENZIONE: prima di fare `git init`/push, i file `*.har` nella cartella
  del progetto (se non ancora cancellati) contengono una password in
  chiaro — vanno cancellati o comunque mai committati;
- `ORIGINI_CONSENTITE` (`config.py`) e' `"*"` di default: va bene per un
  test interno, da restringere se poi diventa pubblico.

## Login automatico SDF (implementato)

Implementato in `fornitori/sdf/client.py`: funzione `login_automatico(username,
password)` (10 passi Auth0 -> store -> ita.store -> eparts, vedi dettaglio
sotto) piu' l'integrazione in `SdfClient`:

- **Priorita' al cookie manuale**: se `SDF_COOKIE`/`dati/cookie.txt` e'
  presente, `SdfClient` lo usa direttamente e non tenta nessun login
  automatico — e' il fallback immediato in caso il login automatico si
  rompa (basta impostare/aggiornare il cookie a mano, senza toccare
  codice, e tutto torna a funzionare come prima).
- Se il cookie manuale non c'e' ma `SDF_USERNAME`/`SDF_PASSWORD` (o
  `dati/sdf_username.txt`/`dati/sdf_password.txt`) sono presenti,
  `SdfClient` esegue `login_automatico()` all'avvio per ottenere il
  `JSESSIONID`.
- Su `SessionExpired` durante una chiamata (`get`/`post`), se
  username/password sono disponibili viene rifatto un login automatico e
  la richiesta e' ritentata una sola volta (`SdfClient._rinnova_sessione`);
  altrimenti l'errore viene propagato come prima, invitando ad aggiornare
  il cookie a mano.
- `login_automatico()` non tenta di indovinare in caso di comportamento
  inatteso (credenziali sbagliate, sito cambiato, captcha comparso): si
  ferma subito con `LoginError` che indica il passo esatto fallito.

Tutto il codice vive dentro `fornitori/sdf/`, rispettando l'astrazione
dalla marca. Se il flusso di login SDF cambia in futuro, questa e' la
sezione da rifare: la ricostruzione originale (sotto) resta valida come
riferimento, e si puo' rilanciare `debug/analizza_har_sdf.py` su un nuovo
HAR per verificare cosa e' cambiato.

### Ricostruzione del flusso (riferimento)

Il portale usa **Auth0** (Universal Login) e poi una catena di **3 ponti
JWT** tra domini diversi, ognuno dei quali lascia un proprio cookie di
sessione. Ricostruito da HAR reali (login completo catturato ed
analizzato con `debug/analizza_har_sdf.py`, che puo' essere rilanciato su
nuove catture se il sito cambia).

### La catena completa

1. `GET https://store.sdfgroup.com/auth0` → **302** verso
   `https://sdfdataplatform.eu.auth0.com/authorize?client_id=...&redirect_uri=https://store.sdfgroup.com/signin-oidc&response_type=code&scope=openid+profile+email&response_mode=form_post&nonce=...&state=...`
2. Auth0 risponde **302** verso `/u/login?state=<state_auth0>` (uno
   state DIVERSO da quello del passo 1 — e' un token di transazione interno
   di Auth0, in formato msgpack/base64, prefisso tipico `hKFo...`).
3. `GET /u/login?state=<state_auth0>` → **200**, pagina di login vera e
   propria (form HTML).
4. `POST /u/login?state=<state_auth0>` con body
   `application/x-www-form-urlencoded`:
   `state=<state_auth0>&username=<email>&password=<password>`
   → **302** verso `/authorize/resume?state=<state_breve>`.
   **Nessun captcha ne' altri campi osservati** in questa cattura: form
   POST semplice, automatizzabile senza browser headless.
5. `GET /authorize/resume?state=<state_breve>` → **200**. La risposta e'
   una pagina HTML che si auto-invia (essendo `response_mode=form_post`)
   con `code` e lo `state` ORIGINALE del passo 1 (formato `CfDJ...`, tipico
   di ASP.NET/.NET OIDC middleware).
6. `POST https://store.sdfgroup.com/signin-oidc` con body
   `code=<code>&state=<state_originale>` → **302** verso
   `https://store.sdfgroup.com/auth0`. Qui `store.sdfgroup.com` diventa
   autenticato (imposta un proprio cookie di sessione, non visto
   direttamente nell'HAR — Chrome redige `Set-Cookie` dagli export HAR per
   sicurezza, ma il funzionamento del passo successivo lo conferma).
7. `GET https://store.sdfgroup.com/auth0` → **200**. La risposta e' UN
   ALTRO form auto-inviato, questa volta con un **JWT** (`eyJhbGci...`,
   HS256) generato da `store.sdfgroup.com` per il passo successivo.
8. `POST https://ita.store.sdfgroup.com/login` con body
   `jwt=<jwt_1>` (origin/referer: `store.sdfgroup.com`) → **302** verso
   `/`. **Qui viene autenticato il sito nopCommerce vero e proprio**
   (`ita.store.sdfgroup.com`, quello dove si naviga/acquista), e vengono
   impostati i cookie `.Nop.Authentication`, `.Nop.Customer`,
   `.Nop.Session`.
9. Quando si clicca il pulsante "vai ai ricambi" sul sito
   (`href="javascript:sdfCore.utilities.goToEparts('spareparts')"`, codice
   JS trovato in Sources), viene aperta
   `GET https://ita.store.sdfgroup.com/eparts/<catalogo>` (es.
   `spareparts`) — non catturata direttamente, ma il suo effetto si', al
   passo 10: restituisce un'altra pagina auto-inviante con un **nuovo JWT**
   generato da `ita.store.sdfgroup.com`.
10. `POST https://eparts.sdfgroup.com/explorer/jwt/cric.html` con body
    `jwt=<jwt_2>` (origin/referer: `ita.store.sdfgroup.com`) → **200**.
    Qui viene finalmente impostato **`JSESSIONID`** su
    `eparts.sdfgroup.com` (cookie `HttpOnly`, dominio esatto
    `eparts.sdfgroup.com`, NON condiviso con `.sdfgroup.com`) — e' l'unico
    cookie che serve al nostro `SdfClient` (`fornitori/sdf/client.py`),
    esattamente quello che oggi si incolla a mano in `dati/cookie.txt` /
    `SDF_COOKIE`.

Nota: dopo il passo 10 l'app SPA viene servita sotto
`/explorer/jwt/...` invece di `/explorer/...`, ma e' probabilmente solo
il modo in cui l'Angular app della SPA e' stata avviata (bootstrap da
quell'entry point) — le chiamate REST che il nostro codice fa oggi verso
`/explorer/rest/...` (senza `/jwt/`) funzionano gia' con il solo cookie
`JSESSIONID`, quindi **non serve cambiare `BASE` in `client.py`**, basta
ottenere un `JSESSIONID` valido con qualunque mezzo.

### Note per manutenzione futura

- Non e' stato ancora verificato quale cookie scade per primo nell'uso
  reale: quello di `store.sdfgroup.com` (impostato al passo 6) o
  `JSESSIONID` (passo 10). Se in futuro si vuole ottimizzare evitando di
  rifare l'intero login Auth0 (passi 1-6) quando basterebbe rigenerare
  solo i JWT successivi (passi 7-10), andrebbe misurato prima — non
  assunto.
- Per verificare/aggiornare questa ricostruzione in futuro (es. se SDF
  cambia il flusso di login): catturare un HAR di un login completo da
  zero (istruzioni: DevTools aperto PRIMA di navigare, "Preserve log"
  attivo, "Auto-open DevTools for popups" nelle impostazioni di DevTools
  per catturare le schede aperte con `window.open`) e lanciare
  `py debug/analizza_har_sdf.py <file.har>`.

## Idea: tabella di apprendimento sui termini imprecisi

Obiettivo: quando l'LLM interpreta un termine gergale/impreciso del
cliente (es. "bulbo" → "sensore pressione olio") e trova il ricambio
giusto, **memorizzare l'interpretazione** cosi' le richieste future con
termini simili non richiedono di nuovo all'LLM di indovinare — il sistema
si "allena" con l'uso reale, non con un dizionario scritto a mano.

Meccanismo pensato (nessuna implementazione ancora):

- Nuova tabella, condivisa e non specifica di marca (vive in `dati/` o in
  un modulo tipo `config.py`/nuovo `apprendimento.py` a livello
  radice, **non** dentro `fornitori/<marca>/`, altrimenti si rompe
  l'astrazione): qualcosa come
  `interpretazioni(termine_grezzo, marca, codice_trovato, categoria,
  confermato_dal_cliente, timestamp)`.
- Quando il cliente conferma che il ricambio trovato e' quello giusto
  (nel flusso esistente di conferma macchina/ricambio), si scrive una
  riga. Le ricerche mai confermate o finite a vuoto si possono comunque
  loggare separatamente, per capire dove il catalogo/l'interpretazione
  fallisce, senza inquinare la tabella "affidabile".
- Prima di far interpretare all'LLM un termine impreciso, si controlla
  se un termine uguale o simile e' gia' presente in tabella: se si', si
  usa direttamente il risultato gia' confermato invece di richiamare
  l'LLM per l'interpretazione.
- Un dizionario di sinonimi vero e proprio (termine → categoria/famiglia
  di ricambio, non legato a un singolo codice) puo' essere derivato
  *offline*, periodicamente, da questa tabella — ma andrebbe **rivisto a
  mano** prima di renderlo automatico: un'interpretazione sbagliata
  confermata per errore da un cliente distratto non deve diventare
  "verita'" per tutte le richieste future. Questo e' il punto su cui
  l'utente vuole prima sentire l'azienda.

## Idea: sfruttare le relazioni gia' presenti nei DB

I due DB hanno gia' piu' struttura "a grafo" di quanto venga usato oggi
nelle risposte:

- **`sdf.db`**: `part` si collega a un modello tramite
  `model_drawing` → `drawing`/`subgroup`/`group` — quindi "altri modelli
  SDF che montano questo stesso ricambio" e' gia' un join possibile oggi,
  senza nuove tabelle. `group`/`subgroup` hanno un `name`: e' una
  tassonomia funzionale gia' pronta (es. "impianto idraulico" e
  sottogruppi) da poter esporre come categoria nelle risposte o usare per
  restringere la ricerca quando il cliente descrive la funzione del
  pezzo invece del nome. C'e' anche `substitution` (old_code→new_code):
  un grafo di ricambi sostitutivi/equivalenti gia' raccolto ma mai
  interrogato nelle risposte attuali.
- **`goldoni.db`**: `tavole.gruppo`/`titolo` sono una categorizzazione
  piu' debole (testo libero, non normalizzato come in SDF), ma comunque
  utilizzabile per raggruppare risultati o suggerire "altri ricambi
  della stessa tavola/gruppo".

Idea concreta: aggiungere ai tool esistenti (o crearne uno nuovo,
`cerca_ricambi_correlati` o simile, esposto via `fornitori/__init__.py`
come gli altri 4) la possibilita' di rispondere anche con "questo
ricambio e' usato anche su: ..." o "esiste un ricambio sostitutivo: ...",
sfruttando i join sopra. Nessuna nuova infrastruttura (niente graph DB
esterno): sono query SQL sulle tabelle che esistono gia'.

## Idea: estrazione ricambi dai cataloghi PDF (linee senza catalogo navigabile)

Alcune linee Goldoni (es. ITMA, TYM, Lovol) non hanno un catalogo HTML
navigabile come le altre, solo PDF scaricabili dal sito
(`goldoni-ricambi.studioilgranello.com`). Obiettivo: estrarre i dati una
volta sola e popolare le stesse tabelle (`tavole`/`righe`) gia' usate per
il resto di `goldoni.db`, cosi' da non dipendere piu' dal sito per quelle
linee (stesso principio delle cache SQLite gia' usate altrove nel
progetto).

Punto critico: **non tutti i PDF hanno testo selezionabile**. Vanno
trattati in due modi diversi, non con la stessa pipeline:

- **PDF con testo nativo** (non scansionati): estraibile in modo
  affidabile con `pdfplumber` o `PyMuPDF`/`fitz` — testo con coordinate,
  eventualmente rilevamento tabelle integrato di `pdfplumber` per
  ricostruire le colonne posizione/codice/descrizione. Da trattare come
  i dati gia' scaricati dal catalogo HTML (stessa fiducia).
- **PDF scansionati** (solo immagine): serve OCR (es.
  `pytesseract`/Tesseract). Qui il rischio e' concreto: un OCR che
  confonde `O`/`0` o `I`/`1` in un codice ricambio produce un errore
  silenzioso e costoso (codice sbagliato dato al cliente). Da NON
  trattare come dato affidabile allo stesso modo del resto:
  - opzione minima e piu' sicura: indicizzare solo il testo OCR in
    full-text (senza pretendere righe strutturate affidabili) e, per
    quelle linee, far rispondere il bot con un rimando ("possibile
    riferimento a pagina X del catalogo PDF, da verificare") invece di
    un codice certo;
  - opzione piu' ambiziosa: estrarre comunque righe strutturate ma
    marcarle con un flag tipo `origine='ocr'` in `righe`, e trattarle
    con meno fiducia nelle risposte (es. invitare sempre a una verifica)
    finche' non si misura quanto sono effettivamente accurate.
- Prima di scrivere qualunque script: controllare pagina per pagina se il
  testo e' estraibile nativamente (con `fitz`, `page.get_text()` vuoto ma
  pagina con immagini ⇒ scansionata) — e' probabile che dentro lo stesso
  PDF ci siano pagine miste (testo nativo + inserti scansionati).
- Come per l'estrazione HTML esistente, il codice andrebbe isolato dentro
  `fornitori/goldoni/` (es. un modulo `pdf.py` o dentro l'adapter
  esistente), per rispettare l'astrazione dalla marca.
