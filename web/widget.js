/*
 * Widget assistente ricambi.
 * Uso:  <script src="widget.js" data-api="http://localhost:8000"></script>
 *
 * Tutto vive dentro uno Shadow DOM: il CSS del sito ospite non puo'
 * rompere il widget, e il widget non puo' rompere il sito.
 *
 * La formattazione del testo del bot (grassetto, elenchi, paragrafi)
 * e' fatta in casa costruendo nodi DOM: nessuna libreria esterna e
 * nessun innerHTML, quindi il testo del modello non puo' in alcun
 * caso iniettare markup nella pagina.
 */
(function () {
  const script = document.currentScript;
  const API = (script && script.dataset.api) || "";

  // I font vanno caricati a livello di documento, non nello shadow root
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "https://fonts.googleapis.com/css2?family=Poppins:wght@500;600;700&family=Inter:wght@400;500;600&family=IBM+Plex+Mono:wght@500&display=swap";
  document.head.appendChild(link);

  const host = document.createElement("div");
  host.id = "assistente-ricambi";
  document.body.appendChild(host);
  const root = host.attachShadow({ mode: "open" });

  root.innerHTML = `
    <style>
      :host {
        --ink:#0C0C0C; --steel:#2E3134; --mute:#8A9095;
        --line:#E2E4E6; --mist:#F4F5F6; --paper:#FFF;
        --red:#E30613; --red-deep:#B00410; --red-soft:#FDECED; --alert:#B00410;
        --body:"Inter",system-ui,-apple-system,sans-serif;
        --disp:"Poppins","Inter",system-ui,sans-serif;
        --mono:"IBM Plex Mono",ui-monospace,monospace;
        --r:4px;
      }
      *{box-sizing:border-box}
      
      /* ---- bollino di apertura (come il tasto "torna su" del sito) ---- */
      .launcher{
        position:fixed; right:24px; bottom:24px; width:58px; height:58px;
        border:0; padding:0; cursor:pointer;
        background:var(--red); border-radius:var(--r);
        box-shadow:0 8px 20px rgba(227,6,19,.28);
        transition:background .18s ease, transform .18s ease;
      }
      .launcher:hover{background:var(--red-deep); transform:translateY(-2px)}
      .launcher:focus-visible{outline:3px solid var(--ink);outline-offset:3px}
      .launcher svg{display:block;width:100%;height:100%}
      .launcher .glyph{fill:#fff}
      .launcher .chev{fill:#fff;opacity:.75}
      .launcher[hidden]{display:none}

      /* ---- pannello ---- */
      .panel{
        position:fixed; right:24px; bottom:24px; width:378px;
        max-width:calc(100vw - 32px);
        height:min(596px, calc(100vh - 48px)); background:var(--paper);
        display:none; flex-direction:column; overflow:hidden;
        border-radius:var(--r);
        box-shadow:0 18px 48px rgba(12,12,12,.24);
        font-family:var(--body); color:var(--ink);
      }
      .panel.open{display:flex; animation:rise .22s cubic-bezier(.2,.8,.3,1)}
      @keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}

      header{
        background:var(--ink); color:#fff; padding:15px 16px;
        display:flex; align-items:center; gap:11px;
        border-bottom:3px solid var(--red);
      }
      header .tag::before{content:"\\00BB"; color:var(--red); margin-right:7px; font-weight:700}
      header .tag{
        font-family:var(--disp); font-weight:600; font-size:18px;
        letter-spacing:.04em; text-transform:uppercase; line-height:1;
      }
      header .sub{font-size:11px; color:#9AA0A5; letter-spacing:.03em; margin-top:4px}
      header .grow{flex:1}
      header button{
        background:none;border:0;color:#9AA0A5;cursor:pointer;
        font-size:22px;line-height:1;padding:2px 4px;
      }
      header button:hover{color:var(--red)}

      .log{flex:1; overflow-y:auto; padding:18px 16px; background:var(--mist)}
      .log::-webkit-scrollbar{width:9px}
      .log::-webkit-scrollbar-thumb{background:#CDD1D4;border-radius:9px}

      .msg{margin-bottom:13px; display:flex}
      .msg .bubble{
        max-width:84%; padding:11px 13px; font-size:14.5px; line-height:1.55;
        border-radius:var(--r); word-wrap:break-word; overflow-wrap:anywhere;
      }
      .msg.bot .bubble{background:var(--paper); border-left:3px solid var(--red)}
      .msg.user{justify-content:flex-end}
      .msg.user .bubble{background:var(--steel); color:#fff; white-space:pre-wrap}
      .msg.err .bubble{background:var(--red-soft);border-left:3px solid var(--alert);color:#7A0A11}

      /* ---- testo formattato dentro le bolle ---- */
      .bubble > *:first-child{margin-top:0}
      .bubble > *:last-child{margin-bottom:0}
      .bubble p{margin:0 0 9px}
      .bubble strong{font-weight:600}
      .bubble em{font-style:italic}
      .bubble ul,.bubble ol{margin:0 0 9px; padding-left:19px}
      .bubble li{margin-bottom:4px}
      .bubble li::marker{color:var(--red)}
      .bubble a{color:var(--red); text-decoration:underline; text-underline-offset:2px}
      .bubble a:hover{color:var(--ink)}
      .esplosi{display:grid; grid-template-columns:1fr 1fr; gap:6px; margin-top:10px}
      .esplosi a{display:block; border:1px solid var(--line); border-radius:var(--r); background:#fff; overflow:hidden}
      .esplosi img{width:100%; height:74px; object-fit:contain; display:block}
      .esplosi a:hover{border-color:var(--red)}

      /* ---- targhetta del codice ricambio (elemento firma) ---- */
      .plate{
        display:inline-flex; align-items:center; gap:8px; margin:3px 4px 3px 0;
        background:var(--ink); color:#fff; padding:6px 10px; border-radius:var(--r);
        font-family:var(--mono); font-size:12.5px; letter-spacing:.02em;
        cursor:pointer; border:0; vertical-align:middle;
        transition:background .15s ease;
      }
      .plate::before{
        content:""; width:5px; height:5px; border-radius:50%;
        background:var(--red); flex:none;
      }
      .plate:hover{background:var(--red)}
      .plate:hover::before{background:#fff}
      .plate.copiato{background:var(--red)}
      .plate.copiato::before{background:#fff}

      /* ---- indicatore di lavoro ---- */
      .working{
        display:flex; align-items:center; gap:9px;
        padding:12px 13px; background:var(--paper);
        border-left:3px solid var(--red); border-radius:var(--r);
        width:max-content; max-width:84%; margin-bottom:13px;
      }
      .working .punti{display:flex; gap:5px; flex:none}
      .working i{width:6px;height:6px;background:var(--red);display:block;border-radius:50%;
                animation:pulse 1.1s infinite}
      .working i:nth-child(2){animation-delay:.15s}
      .working i:nth-child(3){animation-delay:.3s}
      @keyframes pulse{0%,60%,100%{opacity:.22}30%{opacity:1}}
      
      .working .stato{
        font-size:12.5px; font-style:italic; color:var(--mute);
        line-height:1.35; overflow-wrap:anywhere;
      }
      .working .stato .pct{font-family:var(--mono); font-style:normal; color:var(--red)}
      .working .avanzamento{
        display:block; height:2px; margin-top:5px; background:var(--line);
        border-radius:2px; overflow:hidden;
      }
      .working .avanzamento > b{
        display:block; height:100%; width:0; background:var(--red);
        transition:width .5s ease;
      }

      /* ---- barra di scrittura ---- */
      form{display:flex; border-top:1px solid var(--line); background:var(--paper)}
      textarea{
        flex:1; border:0; resize:none; padding:14px; font:inherit; font-size:14.5px;
        max-height:110px; outline:none; color:var(--ink); font-family:var(--body);
      }
      textarea::placeholder{color:var(--mute)}
      textarea:focus{box-shadow:inset 3px 0 0 var(--red)}
      form button{
        border:0; background:var(--red); color:#fff; cursor:pointer;
        padding:0 20px; font-family:var(--disp); font-weight:600; font-size:13px;
        letter-spacing:.08em; text-transform:uppercase;
        transition:background .15s ease;
      }
      form button:hover{background:var(--red-deep)}
      form button:disabled{background:var(--line); color:var(--mute); cursor:default}

      @media (max-width:480px){
        .panel{right:0;bottom:0;width:100vw;height:100vh;max-width:none;border-radius:0}
        .launcher{right:16px;bottom:16px}
      }
      @media (prefers-reduced-motion:reduce){
        .panel.open{animation:none} .launcher{transition:none}
        .working i{animation:none;opacity:.5}
      }
    </style>

    <button class="launcher" aria-label="Apri l'assistente ricambi">
      <svg viewBox="0 0 58 58" aria-hidden="true">
        <g class="glyph">
          <rect x="15" y="17" width="28" height="20" rx="2"/>
          <path d="M20 37h8l-8 8z"/>
        </g>
        <g class="chev">
          <path d="M23 23l4 4-4 4-1.6-1.6L24.8 27l-3.4-3.4z"/>
          <path d="M29 23l4 4-4 4-1.6-1.6L30.8 27l-3.4-3.4z"/>
        </g>
      </svg>
    </button>

    <section class="panel" role="dialog" aria-label="Assistente ricambi">
      <header>
        <div>
          <div class="tag">Ricambi PerniceStore</div>
          <div class="sub">Cerca per modello e matricola</div>
        </div>
        <div class="grow"></div>
        <button class="close" aria-label="Chiudi">&times;</button>
      </header>
      <div class="log" aria-live="polite"></div>
      <form>
        <textarea rows="1" placeholder="Che pezzo ti serve?"></textarea>
        <button type="submit">Invia</button>
      </form>
    </section>
  `;

  const $ = (s) => root.querySelector(s);
  const launcher = $(".launcher"), panel = $(".panel"), log = $(".log");
  const form = $("form"), input = $("textarea"), send = $("form button");

  let sessione = null;
  let occupato = false;

  const BENVENUTO =
    "Ciao. Dimmi che macchina hai **(marca, serie e modello)** e il pezzo che cerchi, " +
    "e ti trovo il codice.\n\n" +
    "Se hai la matricola sottomano tienila pronta: serve a scegliere " +
    "la variante giusta.";

  // ==================================================================
  // FORMATTAZIONE
  // ==================================================================

  // Codici ricambio: 0.044.1567.0/10, 2.8029.760.0/30, 0.902.S001.0
  // const RE_CODICE = /\b\d[\dA-Z]*(?:\.[\dA-Z]+){2,}(?:\/\d+)?\b/g;
  let RE_CODICE = /(?!)/g; 
  const RE_ELENCO = /^\s*[-*+•]\s+(.*)$/;
  const RE_LINK = /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g;
  const RE_NUMERO = /^\s*(\d{1,2})[.)]\s+(.*)$/;

  // --- //
  function aggiornaCodici(codici) {
    if (!codici || !codici.length) return;
    const alt = codici
      .slice().sort((a, b) => b.length - a.length)   // i piu' lunghi per primi
      .map(c => c.replace(/[.*+?^${}()|[\]\\\/]/g, "\\$&"))
      .join("|");
    RE_CODICE = new RegExp("(?:" + alt + ")", "g");
  }
  // --- //

  function targhetta(cod) {
    const p = document.createElement("button");
    p.className = "plate";
    p.type = "button";
    p.textContent = cod;
    p.title = "Copia il codice";
    p.onclick = () => {
      navigator.clipboard.writeText(cod);
      p.textContent = "copiato";
      p.classList.add("copiato");
      setTimeout(() => {
        p.textContent = cod;
        p.classList.remove("copiato");
      }, 1100);
    };
    return p;
  }

  /* Testo semplice: i codici ricambio diventano targhette cliccabili. */
  function inserisciTesto(contenitore, testo) {
    let ultimo = 0;
    RE_CODICE.lastIndex = 0;
    testo.replace(RE_CODICE, (cod, pos) => {
      if (pos > ultimo) contenitore.append(testo.slice(ultimo, pos));
      contenitore.append(targhetta(cod));
      ultimo = pos + cod.length;
      return cod;
    });
    if (ultimo < testo.length) contenitore.append(testo.slice(ultimo));
  }

  /* Una riga: applica **grassetto**, poi delega il resto. */
  /* Link markdown [testo](url) -> ancora, oppure anteprima se e' un disegno */
  function inserisciConLink(contenitore, testo) {
    let ultimo = 0;
    RE_LINK.lastIndex = 0;
    testo.replace(RE_LINK, (tutto, etichetta, url, pos) => {
      if (pos > ultimo) inserisciTesto(contenitore, testo.slice(ultimo, pos));
      const a = document.createElement("a");
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = etichetta;
      contenitore.append(a);
      if (/\.(gif|png|jpe?g)$/i.test(url)) esplosi.push(url);
      ultimo = pos + tutto.length;
      return tutto;
    });
    if (ultimo < testo.length) inserisciTesto(contenitore, testo.slice(ultimo));
  }

  function inserisciRiga(contenitore, riga) {
    riga = riga.replace(/`([^`\n]+)`/g, "$1"); // toglie i backtick
    riga.split(/(\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\n]+\*)/g).forEach((pezzo) => {
      if (!pezzo) return;
      if (/^(\*\*[^*\n]+\*\*|__[^_\n]+__)$/.test(pezzo)) {
        const f = document.createElement("strong");
        inserisciConLink(f, pezzo.slice(2, -2));
        contenitore.append(f);
      } else if (/^\*[^*\n]+\*$/.test(pezzo)) {
        const e = document.createElement("em");
        inserisciConLink(e, pezzo.slice(1, -1));
        contenitore.append(e);
      } else {
        inserisciConLink(contenitore, pezzo);
      }
    });
  }

  /* Raggruppa le righe in paragrafi ed elenchi e costruisce i nodi. */
  let esplosi = [];

  function componi(testo) {
    esplosi = [];
    const frag = document.createDocumentFragment();
    const righe = String(testo).replace(/\r/g, "").split("\n");
    let blocco = null, tipo = null;

    righe.forEach((riga) => {
      if (!riga.trim()) { blocco = null; tipo = null; return; }

      const punt = riga.match(RE_ELENCO);
      const num = punt ? null : riga.match(RE_NUMERO);

      if (punt || num) {
        const t = punt ? "ul" : "ol";
        if (tipo !== t) {
          blocco = document.createElement(t);
          frag.append(blocco);
          tipo = t;
        }
        const li = document.createElement("li");
        inserisciRiga(li, punt ? punt[1] : num[2]);
        blocco.append(li);
      } else {
        if (tipo !== "p") {
          blocco = document.createElement("p");
          frag.append(blocco);
          tipo = "p";
        } else {
          blocco.append(document.createElement("br"));
        }
        inserisciRiga(blocco, riga.trim());
      }
    });

    // anteprime dei disegni citati: il cliente riconosce il pezzo
    // guardando l'esploso, anche senza conoscere i termini tecnici
    if (esplosi.length) {
      const galleria = document.createElement("div");
      galleria.className = "esplosi";
      [...new Set(esplosi)].slice(0, 4).forEach((url) => {
        const a = document.createElement("a");
        a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer";
        const img = document.createElement("img");
        img.src = url; img.loading = "lazy"; img.alt = "Disegno esploso";
        a.append(img);
        galleria.append(a);
      });
      frag.append(galleria);
    }
    return frag;
  }

  // ==================================================================
  // INTERFACCIA
  // ==================================================================

  function bolla(tipo, testo) {
    const wrap = document.createElement("div");
    wrap.className = "msg " + tipo;
    const b = document.createElement("div");
    b.className = "bubble";

    if (tipo === "user") {
      b.textContent = testo;
    } else {
      b.append(componi(testo));
    }

    wrap.append(b);
    log.append(wrap);
    log.scrollTop = log.scrollHeight;
    return wrap;
  }

  function attesa() {
    const w = document.createElement("div");
    w.className = "working";
  
    const punti = document.createElement("span");
    punti.className = "punti";
    punti.append(document.createElement("i"),
                document.createElement("i"),
                document.createElement("i"));
  
    const stato = document.createElement("span");
    stato.className = "stato";
  
    const testo = document.createElement("span");
    testo.className = "testo";
    const pct = document.createElement("span");
    pct.className = "pct";
    const barra = document.createElement("span");
    barra.className = "avanzamento";
    barra.hidden = true;
    const riempi = document.createElement("b");
    barra.append(riempi);
  
    stato.append(testo, pct, barra);
    w.append(punti, stato);
    log.append(w);
    log.scrollTop = log.scrollHeight;
  
    /* Aggiorna la riga senza ricrearla: cosi' non "salta" a ogni evento. */
    w.aggiorna = (msg, percentuale) => {
      if (msg) testo.textContent = msg;
      if (typeof percentuale === "number" && percentuale > 0) {
        pct.textContent = "  " + percentuale + "%";
        barra.hidden = false;
        riempi.style.width = Math.min(percentuale, 100) + "%";
      }
      log.scrollTop = log.scrollHeight;
    };
    return w;
  }

  function apri() {
    panel.classList.add("open");
    launcher.hidden = true;
    input.focus();
    if (!log.children.length) bolla("bot", BENVENUTO);
  }

  function chiudi() {
    panel.classList.remove("open");
    launcher.hidden = false;
    launcher.focus();
  }

  launcher.onclick = apri;
  $(".close").onclick = chiudi;
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && panel.classList.contains("open")) chiudi();
  });

  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 110) + "px";
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
  });

  form.onsubmit = async (e) => {
    e.preventDefault();
    const testo = input.value.trim();
    if (!testo || occupato) return;
  
    bolla("user", testo);
    input.value = "";
    input.style.height = "auto";
    occupato = true;
    send.disabled = true;
    const spinner = attesa();
  
    try {
      const r = await fetch(API + "/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messaggio: testo, sessione }),
      });
  
      if (!r.ok || !r.body) throw new Error("risposta non valida");
  
      const lettore = r.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let risposto = false;
  
      while (true) {
        const { done, value } = await lettore.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
  
        // i frame SSE sono separati da una riga vuota; l'ultimo pezzo puo'
        // essere incompleto, quindi resta nel buffer per il giro dopo
        const parti = buffer.split("\n\n");
        buffer = parti.pop();
  
        for (const parte of parti) {
          const riga = parte.split("\n").find((x) => x.startsWith("data: "));
          if (!riga) continue;
  
          let ev;
          try { ev = JSON.parse(riga.slice(6)); } catch { continue; }
  
          if (ev.tipo === "stato") {
            spinner.aggiorna(ev.testo, ev.percentuale);
  
          } else if (ev.tipo === "risposta") {
            sessione = ev.sessione || sessione;
            aggiornaCodici(ev.codici);
            spinner.remove();
            bolla(ev.errore ? "err" : "bot", ev.testo);
            risposto = true;
          }
        }
      }
  
      if (!risposto) {
        spinner.remove();
        bolla("err", "La risposta si e' interrotta. Riprova.");
      }
    } catch (err) {
      spinner.remove();
      bolla("err", "Connessione al servizio non riuscita. Controlla la rete e riprova.");
      console.error(err);
    } finally {
      occupato = false;
      send.disabled = false;
      input.focus();
    }
  };
})();
