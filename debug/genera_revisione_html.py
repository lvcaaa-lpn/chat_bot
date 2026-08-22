"""
Genera uno strumento HTML locale per revisionare a mano un'estrazione dati
da PDF scansionato (schema comune: JSON con tavole/righe, ogni riga con
posizione/codice/descrizione/confidenza + revisione.bbox/pagina_pdf/dpi
per individuare il ritaglio esatto nella pagina originale).

Riutilizzabile per qualunque catalogo estratto con questo stesso schema
(non solo pbt00135): basta passare un altro file JSON/PDF.

Uso:
    ./venv/Scripts/python.exe debug/genera_revisione_html.py \
        dati/estrazioni_pdf/pbt00135.json pbt00135.pdf \
        dati/estrazioni_pdf/pbt00135_revisione.html

Lo strumento e' un unico file HTML autosufficiente (immagini incluse come
data URI): apribile direttamente nel browser via doppio clic, nessun
server necessario. Un pulsante in fondo esporta il JSON corretto
(scaricato dal browser), da usare al posto del file originale una volta
completata la revisione.
"""
import base64
import io
import json
import sys

import fitz  # pymupdf
from PIL import Image

PADDING = 8  # px di margine attorno al riquadro ritagliato


def ritaglia_riga(pagine_cache, doc, pagina_pdf, dpi, bbox):
    chiave = (pagina_pdf, dpi)
    if chiave not in pagine_cache:
        pix = doc[pagina_pdf - 1].get_pixmap(dpi=dpi)
        img = Image.frombytes(
            "RGB" if pix.n >= 3 else "L", [pix.width, pix.height], pix.samples
        ).convert("RGB")
        pagine_cache[chiave] = img
    img = pagine_cache[chiave]
    left = max(0, bbox["left"] - PADDING)
    top = max(0, bbox["top"] - PADDING)
    right = min(img.width, bbox["right"] + PADDING)
    bottom = min(img.height, bbox["bottom"] + PADDING)
    ritaglio = img.crop((left, top, right, bottom))
    buf = io.BytesIO()
    ritaglio.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def genera(json_path, pdf_path, output_html_path):
    with open(json_path, encoding="utf-8") as f:
        dati = json.load(f)

    doc = fitz.open(pdf_path)
    pagine_cache = {}

    righe_flat = []
    for i_tav, tav in enumerate(dati["tavole"]):
        for i_riga, riga in enumerate(tav["righe"]):
            rev = riga.get("revisione")
            img_b64 = None
            if rev and rev.get("bbox"):
                img_b64 = ritaglia_riga(
                    pagine_cache, doc, rev["pagina_pdf"], rev["dpi"], rev["bbox"]
                )
            righe_flat.append(
                {
                    "id": f"t{i_tav}_r{i_riga}",
                    "tavola": tav.get("tavola"),
                    "titolo_tavola": tav.get("titolo"),
                    "pagina_pdf": rev["pagina_pdf"] if rev else tav.get("pagina_pdf"),
                    "posizione": riga.get("posizione"),
                    "codice": riga.get("codice"),
                    "descrizione": riga.get("descrizione"),
                    "confidenza": riga.get("confidenza"),
                    "img": img_b64,
                }
            )

    meta = {k: v for k, v in dati.items() if k != "tavole"}

    dati_json = json.dumps({"meta": meta, "righe": righe_flat}, ensure_ascii=False)
    dati_json = dati_json.replace("</", "<\\/")  # evita di chiudere il tag <script> per errore
    html = HTML_TEMPLATE.replace("__DATI_JSON__", dati_json).replace(
        "__NOME_OUTPUT__", json.dumps(f"{meta.get('catalogo', 'catalogo')}_corretto.json")
    )

    with open(output_html_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Generato: {output_html_path} ({len(righe_flat)} righe, {len(dati['tavole'])} tavole)")


HTML_TEMPLATE = r"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<title>Revisione estrazione ricambi</title>
<style>
  :root {
    --bg: #f5f5f4; --fg: #1c1c1a; --card: #ffffff; --border: #ddd8d0;
    --accent: #8a4b2f; --ok: #3d7a4d; --warn: #b8860b; --bad: #b3402f;
    --muted: #7a756c;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#1c1a17; --fg:#ece7de; --card:#26221d; --border:#3a352c;
      --accent:#d99a6c; --ok:#7fbf8f; --warn:#e0b24d; --bad:#e08070; --muted:#a39c8e; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
    font-family: -apple-system, "Segoe UI", Arial, sans-serif; }
  header { position: sticky; top:0; background:var(--bg); padding:16px 20px;
    border-bottom:1px solid var(--border); z-index:5; }
  header h1 { margin:0 0 6px; font-size:1.1rem; }
  .stats { font-size:0.85rem; color:var(--muted); margin-bottom:10px; }
  .filtri { display:flex; gap:8px; flex-wrap:wrap; }
  .filtri button { border:1px solid var(--border); background:var(--card); color:var(--fg);
    border-radius:20px; padding:5px 12px; font-size:0.8rem; cursor:pointer; }
  .filtri button.attivo { background:var(--accent); color:#fff; border-color:var(--accent); }
  main { padding:12px 20px 80px; max-width:1100px; margin:0 auto; }
  .tavola-titolo { margin:22px 0 8px; font-size:0.95rem; color:var(--muted); }
  .riga { display:flex; gap:14px; align-items:flex-start; background:var(--card);
    border:1px solid var(--border); border-radius:8px; padding:10px 12px; margin-bottom:8px; }
  .riga.nascosta { display:none; }
  .riga img { max-width:420px; max-height:70px; border:1px solid var(--border); border-radius:4px; background:#fff; }
  .campi { flex:1; display:grid; grid-template-columns: 70px 130px 1fr; gap:8px; }
  .campi input { width:100%; border:1px solid var(--border); background:var(--bg); color:var(--fg);
    border-radius:5px; padding:5px 7px; font-size:0.85rem; font-family:inherit; }
  .campi input.modificato { border-color: var(--accent); }
  .meta-riga { grid-column: 1 / -1; font-size:0.72rem; color:var(--muted); }
  .badge { display:inline-block; padding:1px 6px; border-radius:10px; font-size:0.68rem; margin-right:6px; }
  .badge.nota { background:rgba(61,122,77,.18); color:var(--ok); }
  .badge.vicino { background:rgba(184,134,11,.18); color:var(--warn); }
  .badge.nuovo { background:rgba(179,64,47,.18); color:var(--bad); }
  .stato { display:flex; flex-direction:column; align-items:center; gap:6px; width:34px; }
  .stato input[type=checkbox] { width:20px; height:20px; }
  footer { position: fixed; bottom:0; left:0; right:0; background:var(--card);
    border-top:1px solid var(--border); padding:10px 20px; display:flex; align-items:center; gap:14px; }
  footer button { background:var(--accent); color:#fff; border:none; border-radius:6px;
    padding:9px 18px; font-size:0.9rem; cursor:pointer; }
  footer .progresso { font-size:0.85rem; color:var(--muted); }
</style>
</head>
<body>
<header>
  <h1 id="titolo-pagina">Revisione estrazione ricambi</h1>
  <div class="stats" id="stats"></div>
  <div class="filtri" id="filtri"></div>
</header>
<main id="main"></main>
<footer>
  <button id="esporta">Esporta JSON corretto</button>
  <span class="progresso" id="progresso"></span>
</footer>
<script>
const DATI = __DATI_JSON__;
const NOME_OUTPUT = __NOME_OUTPUT__;

document.getElementById('titolo-pagina').textContent =
  'Revisione: ' + (DATI.meta.catalogo || '') + ' — ' + (DATI.meta.modello || '');

function classificaBadge(conf) {
  if (!conf) return {cls:'nuovo', label:conf};
  if (conf.startsWith('nota')) return {cls:'nota', label:'nota'};
  if (conf.startsWith('vicino_a_codice_noto')) return {cls:'vicino', label:'vicino a codice noto'};
  if (conf.startsWith('non_fornito')) return {cls:'nota', label:'non fornito singolarmente'};
  return {cls:'nuovo', label: conf.split(';')[0]};
}

const stato = {}; // id -> {verificato, modificato}
DATI.righe.forEach(r => stato[r.id] = {verificato:false, modificato:false});

const main = document.getElementById('main');
let tavolaCorrente = null;
DATI.righe.forEach(r => {
  if (r.tavola !== tavolaCorrente) {
    tavolaCorrente = r.tavola;
    const h = document.createElement('div');
    h.className = 'tavola-titolo';
    h.textContent = 'Tav. ' + r.tavola + ' — ' + (r.titolo_tavola || '') + '  (pag. PDF ' + r.pagina_pdf + ')';
    main.appendChild(h);
  }

  const div = document.createElement('div');
  div.className = 'riga';
  div.dataset.id = r.id;
  const b = classificaBadge(r.confidenza);

  div.innerHTML = `
    <div class="stato">
      <input type="checkbox" title="segna come verificata" data-role="verificata">
    </div>
    ${r.img ? `<img src="data:image/png;base64,${r.img}" alt="ritaglio pagina">` : '<div style="width:120px;color:var(--muted);font-size:.75rem">nessuna immagine</div>'}
    <div class="campi">
      <input data-role="posizione" placeholder="pos." value="${r.posizione ?? ''}">
      <input data-role="codice" placeholder="codice" value="${r.codice ?? ''}">
      <input data-role="descrizione" placeholder="descrizione" value="${(r.descrizione ?? '').replace(/"/g,'&quot;')}">
      <div class="meta-riga"><span class="badge ${b.cls}">${b.label}</span>${r.confidenza ?? ''}</div>
    </div>
  `;
  main.appendChild(div);

  div.querySelectorAll('input[data-role]:not([data-role=verificata])').forEach(inp => {
    inp.addEventListener('input', () => {
      inp.classList.add('modificato');
      stato[r.id].modificato = true;
      aggiornaProgresso();
    });
  });
  div.querySelector('[data-role=verificata]').addEventListener('change', e => {
    stato[r.id].verificato = e.target.checked;
    aggiornaProgresso();
  });
});

function aggiornaProgresso() {
  const tot = DATI.righe.length;
  const verificate = Object.values(stato).filter(s => s.verificato).length;
  const modificate = Object.values(stato).filter(s => s.modificato).length;
  document.getElementById('progresso').textContent =
    `${verificate}/${tot} verificate — ${modificate} modificate`;
}

// statistiche iniziali
const conteggi = {};
DATI.righe.forEach(r => {
  const b = classificaBadge(r.confidenza).cls;
  conteggi[b] = (conteggi[b]||0)+1;
});
document.getElementById('stats').textContent =
  `${DATI.righe.length} righe totali — ` +
  Object.entries(conteggi).map(([k,v]) => `${k}: ${v}`).join(' · ');

// filtri
const FILTRI = [
  ['tutte', () => true],
  ['posizione mancante', r => !r.posizione],
  ['da verificare', r => (r.confidenza||'').startsWith('nuovo_da_verificare')],
  ['bassa confidenza', r => (r.confidenza||'').includes('bassa confidenza')],
  ['non verificate', r => !stato[r.id].verificato],
];
const filtriDiv = document.getElementById('filtri');
FILTRI.forEach(([nome, fn], i) => {
  const btn = document.createElement('button');
  btn.textContent = nome;
  if (i===0) btn.classList.add('attivo');
  btn.addEventListener('click', () => {
    filtriDiv.querySelectorAll('button').forEach(b=>b.classList.remove('attivo'));
    btn.classList.add('attivo');
    document.querySelectorAll('.riga').forEach(div => {
      const r = DATI.righe.find(x => x.id === div.dataset.id);
      div.classList.toggle('nascosta', !fn(r));
    });
  });
  filtriDiv.appendChild(btn);
});

aggiornaProgresso();

document.getElementById('esporta').addEventListener('click', () => {
  const perId = {};
  DATI.righe.forEach(r => perId[r.id] = r);
  document.querySelectorAll('.riga').forEach(div => {
    const id = div.dataset.id;
    perId[id].posizione = div.querySelector('[data-role=posizione]').value || null;
    perId[id].codice = div.querySelector('[data-role=codice]').value || null;
    perId[id].descrizione = div.querySelector('[data-role=descrizione]').value || null;
    perId[id].verificato = stato[id].verificato;
    perId[id].modificato = stato[id].modificato;
    delete perId[id].img;
  });

  const out = { ...DATI.meta, righe_revisionate: DATI.righe };
  const blob = new Blob([JSON.stringify(out, null, 2)], {type:'application/json'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = NOME_OUTPUT;
  a.click();
  URL.revokeObjectURL(url);
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Uso: genera_revisione_html.py <estrazione.json> <origine.pdf> <output.html>")
        sys.exit(1)
    genera(sys.argv[1], sys.argv[2], sys.argv[3])
