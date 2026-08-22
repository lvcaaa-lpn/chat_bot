"""
Risoluzione della matricola SDF verso la variante di modello corretta.

Il catalogo elenca le varianti "dalla matricola X in poi": data la matricola
del cliente si sceglie l'intervallo giusto. E' una decisione tecnica, quindi
sta nel codice e non viene lasciata all'LLM.

FORMATO SDF
-----------
Il nome del modello contiene il telaio iniziale:

    ARGON 65 -> NNZJY002W0BS00001
    ARGON 70 -> ZKDEE20200RS50001

L'endpoint chassy/searchByVin restituisce il 'vinpattern' corrispondente:

    NNZJY00##0BS?????     serial = 1
    ZKDEE2###0RS?????     serial = 50001

dove '?' marca le cifre del progressivo e '#' le posizioni che variano da
macchina a macchina. Il progressivo SDF e' quindi di CINQUE cifre, non
quattro come in altri cataloghi: leggerne quattro darebbe 1 per entrambi
gli esempi sopra, sbagliando la seconda variante.

STRATEGIA
---------
1. Telaio completo (>= 15 caratteri): non serve indovinare, l'adapter usa
   chassy/searchByVin che e' esatto.
2. Solo progressivo ("12000", "50120"): si confronta numericamente con i
   progressivi di partenza delle varianti candidate.
Il confronto sul prefisso e' tollerante alle posizioni variabili, perche'
il prefisso del cliente puo' differire da quello del catalogo proprio nei
punti marcati '#'.
"""

import re

CIFRE_PROGRESSIVO = 5


def pulisci(testo):
    return re.sub(r"[\s\-_.]", "", str(testo or "")).upper()


def spezza_matricola(matricola, cifre=CIFRE_PROGRESSIVO):
    """'NNZJY002W0BS00001' -> ('NNZJY002W0BS', 1). Il prefisso puo' essere ''."""
    p = pulisci(matricola)
    if not p:
        return None, None
    coda = p[-cifre:]
    if not coda.isdigit():
        # progressivo piu' corto (il cliente ha scritto solo le cifre utili)
        m = re.match(r"^(?P<pref>.*?)(?P<seq>\d+)$", p)
        if not m or not m.group("seq"):
            return None, None
        return m.group("pref"), int(m.group("seq"))
    return p[:-cifre], int(coda)


def solo_progressivo(matricola):
    """True se il cliente ha dato solo cifre ('12000') e non un telaio intero."""
    return pulisci(matricola).isdigit()


def telaio_completo(matricola):
    """I VIN SDF sono di 17 caratteri; sotto i 15 non e' un telaio."""
    return len(pulisci(matricola)) >= 15


def prefissi_compatibili(a, b, tolleranza=3):
    """
    Confronto posizione per posizione, tollerante alle posizioni variabili
    del vinpattern ('#'). Prefissi di lunghezza diversa non sono compatibili.
    """
    if a is None or b is None:
        return False
    if a == b:
        return True
    if len(a) != len(b):
        return False
    diverse = sum(1 for x, y in zip(a, b) if x != y)
    return diverse <= tolleranza


def estrai_intervalli(matricola_catalogo):
    """
    Una voce di catalogo puo' elencare piu' intervalli con lo stesso prefisso:
        'NNZJY002W0BS00001'          -> [('NNZJY002W0BS', 1)]
        '56450WVT50001 / 60001'      -> [('56450WVT', 50001), ('56450WVT', 60001)]
    """
    intervalli = []
    prefisso_corrente = None
    for pezzo in str(matricola_catalogo or "").split("/"):
        pezzo = pezzo.strip()
        if not pezzo:
            continue
        pref, seq = spezza_matricola(pezzo)
        if seq is None:
            continue
        if pref:
            prefisso_corrente = pref
        elif prefisso_corrente is None:
            continue
        intervalli.append((prefisso_corrente, seq))
    return intervalli


def risolvi_variante(matricola, varianti):
    """
    varianti: lista di dict con almeno le chiavi 'nome' e 'matricola'
              ('matricola' = telaio di partenza dichiarato dal catalogo).

    Restituisce sempre un dict con la chiave 'esito':
      'trovato'     -> indice, variante
      'ambiguo'     -> opzioni fra cui far scegliere il cliente
      'non_trovato' -> motivo leggibile
    """
    pref_cliente, seq_cliente = spezza_matricola(matricola)
    if seq_cliente is None:
        return {"esito": "non_trovato",
                "motivo": f"Matricola '{matricola}' non riconosciuta: attesa "
                          "una sigla seguita da cifre, oppure il solo progressivo."}

    soltanto_cifre = solo_progressivo(matricola)

    candidate = []
    prefissi_noti = set()
    for i, v in enumerate(varianti):
        for pref_v, seq_v in estrai_intervalli(v.get("matricola", "")):
            prefissi_noti.add(pref_v)
            # con il solo progressivo non c'e' prefisso da confrontare
            if soltanto_cifre or prefissi_compatibili(pref_cliente, pref_v):
                candidate.append((i, v, seq_v))

    if not candidate:
        return {"esito": "non_trovato",
                "motivo": f"Nessuna variante compatibile con '{pref_cliente}'.",
                "prefissi_disponibili": sorted(p for p in prefissi_noti if p)}

    valide = [c for c in candidate if c[2] <= seq_cliente]
    if not valide:
        minimo = min(c[2] for c in candidate)
        return {"esito": "non_trovato",
                "motivo": f"Matricola {seq_cliente} precedente all'inizio del "
                          f"catalogo ({minimo}) per questo modello."}

    valide.sort(key=lambda c: c[2])
    inizio = valide[-1][2]
    pari = {c[0]: c for c in valide if c[2] == inizio}

    if len(pari) > 1:
        return {"esito": "ambiguo",
                "motivo": "Piu' varianti compatibili con questa matricola.",
                "opzioni": [{"indice": i, "nome": v["nome"],
                             "matricola_da": v.get("matricola", "")}
                            for i, (_, v, _) in pari.items()]}

    indice, variante, _ = next(iter(pari.values()))
    return {"esito": "trovato", "indice": indice, "variante": variante,
            "matricola_da": variante.get("matricola", ""),
            "progressivo_cliente": seq_cliente}
