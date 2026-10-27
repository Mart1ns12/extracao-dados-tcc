"""Casador de tickers por texto, versao 3: ocorrencias validas, minimo no corpo e motivos de
invalidacao.

Porte de validar_wp.py (tickers_locais_v3). Funcoes puras: sem banco e sem rede.
Cada mencao invalida recebe um motivo; vale o primeiro que casar, na ordem E1..E6:

  E1 contexto: EXCLUSOES_NOME (VIVT3 "ao vivo", BPAC11 como fonte, B3SA3 "na B3"),
     RAIL3 "sem rumo" e B3SA3 "bolsa (b3)"                         titulo e corpo
  E2 analista ou casa: BPAC11, ITUB4, BBDC3, BBDC4, SANB11, BBAS3   titulo e corpo
  E3 palavra comum: nome ou apelido de ate 5 letras em minuscula    titulo e corpo
  E4 boilerplate: a mencao cai num trecho do IndiceShingles         so corpo
  E5 panorama: bloco com K_BLOCO tickers distintos em ocorrencia    so corpo
  E6 cotacao: codigo seguido de numero (ex.: "PETR4 +2,5%")         so corpo
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace

from tcc_scraper.config import (
    APELIDOS,
    IBOV_TICKERS,
    NOMES_EMPRESA,
    SEARCH_QUALIFIERS,
    TICKER_CODE_CVM,
)

MIN_CORPO = 2
K_BLOCO = 4
MAX_LETRAS_TERMO_CAPITAL = 5
JANELA_TRECHO = 80
PRIORIDADE_TIPO = {"codigo": 0, "nome": 1, "apelido": 2}
ANALISTA_TICKERS = ("BPAC11", "ITUB4", "BBDC3", "BBDC4", "SANB11", "BBAS3")
# E1, E3 e E5 sao exclusoes deterministicas de sentido: nao viram revisavel
# (a juiza nao recebe "ao vivo").
MOTIVOS_REVISAVEIS_TITULO = frozenset({"E2"})
MOTIVOS_REVISAVEIS_CORPO = frozenset({"E2", "E4", "E6"})

# Apostrofos (ASCII e tipograficos) somem, sem deixar espaco: "Rede D'Or" vira "rede dor",
# igual ao nome "Rede DOr" do config. Com espaco viraria "rede d or" e nao casaria.
_APOSTROFOS = dict.fromkeys(map(ord, "'’‘`"))

# Qualificadores locais extras, somados ao SEARCH_QUALIFIERS do config: nome e apelido
# so valem se algum termo aparecer no texto (titulo + corpo).
QUALIFICADORES_EXTRA = {
    "VIVT3": "telefonica OR telecom OR operadora OR telefonia OR 5g OR banda larga",
    "VAMO3": "locacao OR caminhoes OR maquinas OR simpar OR locadora",
}

# Exclusoes de contexto: mencao de NOME ou APELIDO que casar um destes padroes nao conta.
# Codigo continua contando.
EXCLUSOES_NOME = {
    "VIVT3": [
        r"ao vivo",
        r"(esta|estao|continua|continuam|mantem|manter|segue|seguem) vivos?",
    ],
    "BPAC11": [
        (
            r"(segundo|para|diz|dizem|avalia|avaliam|recomenda|recomendam|projeta|projetam|"
            r"estima|estimam|ve|veem|calcula|calculam) (o )?btg"
        ),
        (
            r"(analistas?|analise|relatorio|carteira|equipe|research|estrategistas?|"
            r"economistas?|gestores?|time) (do |da )?btg"
        ),
        (
            r"btg( pactual)? (recomenda|eleva|corta|rebaixa|reitera|inicia|mantem|"
            r"projeta|estima|avalia|ve|diz|afirma|calcula|aponta|espera|preve)"
        ),
    ],
    "B3SA3": [
        r"(na|pela|listad[ao]s? na|negociad[ao]s? na|estreia na|ipo na) b3",
        r"bolsa de valores \(b3\)",
        r"bolsa (brasileira )?\(b3\)",
        # B3 como bolsa de onde a empresa sai: "pode sair da B3", "deixa a B3"
        (
            r"(sair|saida|deixar|deixa|deixou|deixara|deslistar|deslistada|deslistagem"
            r"|fechar o capital) (da |a )?b3"
        ),
    ],
    "RAIL3": [
        r"sem rumo",
        r"rumo (a|ao|aos|as)\b",
        r"(novo|mesmo|outro|o|de|no) rumo\b",
    ],
}

_CACHE_CHAR: dict[str, str] = {}


def sem_acento(s: str) -> str:
    """Minusculas e sem acento. Apostrofos somem. Mesma funcao para o texto e para os nomes."""
    s = unicodedata.normalize("NFKD", s.translate(_APOSTROFOS))
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def normaliza_mapa(s: str) -> tuple[str, list[int]]:
    """sem_acento caractere a caractere, com indice de volta ao texto original
    (trecho com acento)."""
    chars: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(s):
        nc = _CACHE_CHAR.get(ch)
        if nc is None:
            nc = _CACHE_CHAR[ch] = sem_acento(ch)
        for c in nc:
            chars.append(c)
            idx.append(i)
    return "".join(chars), idx


def _pat(termo: str) -> tuple[str, re.Pattern]:
    s = sem_acento(termo).strip()
    return s, re.compile(r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9])")


def _quals(termos: str) -> list[tuple[str, re.Pattern]]:
    return [_pat(x.strip().strip('"')) for x in termos.split(" OR ") if x.strip()]


def _padroes_analista(ticker: str, termos: list[str]) -> list[re.Pattern]:
    """E2: padroes de analista e casa, com {n} = cada nome ou apelido normalizado."""
    pats: list[str] = []
    for n in dict.fromkeys(termos):
        e = re.escape(n)
        pats += [
            rf"{e} (bba|bbi|investimentos|asset|research|corretora|securities|wealth)",
            rf"(segundo|de acordo com|na avaliacao d[oa]|conforme|para) (o |a )?{e}",
            (
                rf"(analistas?|relatorio|carteira|equipe|research|estrategistas?|"
                rf"economistas?|time) (do |da )?{e}"
            ),
            rf"no {e}( pactual)?, (a|o) (equipe|time|analista)",
        ]
    if ticker == "BPAC11":
        pats += [r"pesquisa btg", r"btg( pactual)?/nexus", r"mynt", *EXCLUSOES_NOME["BPAC11"]]
    return [re.compile(x) for x in pats]


def construir_regras() -> dict:
    """Regras de casamento por ticker. Universo = IBOV_TICKERS (85 ativos)."""
    regras = {}
    for t in IBOV_TICKERS:
        nomes = {}
        for n in dict.fromkeys([IBOV_TICKERS.get(t), NOMES_EMPRESA.get(t)]):
            if n:
                s, p = _pat(n)
                nomes[s] = p
        apelidos = [_pat(a) for a in APELIDOS.get(t, [])]
        qual = None  # None = sem qualificador: nome e apelido sempre valem
        if t in SEARCH_QUALIFIERS or t in QUALIFICADORES_EXTRA:
            termos = " OR ".join(
                x for x in (SEARCH_QUALIFIERS.get(t), QUALIFICADORES_EXTRA.get(t)) if x
            )
            qual = _quals(termos)
        regras[t] = {
            "cod": _pat(t),
            "nomes": list(nomes.items()),
            "apelidos": apelidos,
            "qual": qual,
            "excl": [re.compile(x) for x in EXCLUSOES_NOME.get(t, [])],
            "analista": (
                _padroes_analista(t, [s for s in nomes] + [s for s, _ in apelidos])
                if t in ANALISTA_TICKERS else []
            ),
        }
    return regras


@dataclass(frozen=True)
class EvidenciaPar:
    """Evidencia de um par (post, ticker).
    Contagens de validas e de revisaveis (invalidas por E1-E3/E4/E6)."""

    ticker: str
    n_titulo: int                # ocorrencias validas no titulo
    n_corpo: int                 # ocorrencias validas no corpo
    n_titulo_revisavel: int      # n_titulo + invalidadas por E2 no titulo
    # n_corpo + invalidadas por E2/E4/E6 no corpo (E1, E3 e E5 ficam de fora)
    n_corpo_revisavel: int
    primeiro_bloco: int | None   # primeiro bloco com mencao valida no corpo
    # empresas distintas (codigo CVM) com mencao valida no titulo, fora a do par
    outras_empresas_titulo: int
    invalidas: dict[str, int]    # motivo -> quantidade

    @property
    def vincula(self) -> bool:
        return self.n_titulo >= 1 or self.n_corpo >= MIN_CORPO

    @property
    def revisavel(self) -> bool:
        """Nao vincula, mas vincularia sem E1-E4/E6: vai para a juiza."""
        return not self.vincula and (
            self.n_titulo_revisavel >= 1 or self.n_corpo_revisavel >= MIN_CORPO
        )


def _spans(norm: str, s: str, pat) -> list[tuple[int, int]]:
    if s not in norm:  # filtro barato antes do regex
        return []
    return [(m.start(), m.end()) for m in pat.finditer(norm)]


def _achar(norm: str, s: str, pat):
    if s not in norm:
        return None
    return pat.search(norm)


def _sobrepoe(a: int, b: int, intervalos: list[tuple[int, int]]) -> bool:
    return any(a < y and x < b for x, y in intervalos)


def _brutas(norm: str, r: dict, qual_ok: bool) -> list[tuple[int, int, str]]:
    """Ocorrencias brutas, antes das invalidacoes: codigo sempre;
    nome e apelido so com qualificador ok."""
    out = [(a, b, "codigo") for a, b in _spans(norm, *r["cod"])]
    if qual_ok:
        for tipo, lista in (("nome", r["nomes"]), ("apelido", r["apelidos"])):
            for s, pat in lista:
                out.extend((a, b, tipo) for a, b in _spans(norm, s, pat))
    return out


def _escolhe(brutas: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """Mencoes sobrepostas contam uma vez: a mais longa; codigo > nome > apelido."""
    escolhidas: list[tuple[int, int, str]] = []
    for a, b, tipo in sorted(brutas, key=lambda x: (-(x[1] - x[0]), x[0], PRIORIDADE_TIPO[x[2]])):
        if not any(a < fb and fa < b for fa, fb, _ in escolhidas):
            escolhidas.append((a, b, tipo))
    escolhidas.sort()
    return escolhidas


def _avalia(nt, orig, idx, r, brutas, corpo: bool, boil, panorama: bool) -> list:
    """Ocorrencias escolhidas de um ticker num segmento: [(a, b, tipo, motivo | None)]."""
    escolhidas = _escolhe(brutas)
    if not escolhidas:
        return []
    ex = [(m.start(), m.end()) for rx in r["excl"] for m in rx.finditer(nt)]
    an = [(m.start(), m.end()) for rx in r["analista"] for m in rx.finditer(nt)]
    out = []
    for a, b, tipo in escolhidas:
        nome = tipo != "codigo"
        motivo = None
        if nome and _sobrepoe(a, b, ex):
            motivo = "E1"
        elif nome and _sobrepoe(a, b, an):
            motivo = "E2"
        elif (
            nome
            and len(nt[a:b].replace(" ", "")) <= MAX_LETRAS_TERMO_CAPITAL
            and not orig[idx[a]].isupper()
        ):
            motivo = "E3"
        elif corpo and boil and any(a < fb and fa < b for fa, fb in boil):
            motivo = "E4"
        elif corpo and panorama:
            motivo = "E5"
        elif corpo and not nome and re.match(r"\s+[-+]?\d", nt[b:]):
            motivo = "E6"
        out.append((a, b, tipo, motivo))
    return out


def evidencias(
    titulo: str,
    blocos: list[str],
    regras: dict,
    boiler: dict[int, list[tuple[int, int]]] | None = None,
) -> dict[str, EvidenciaPar]:
    """{ticker: EvidenciaPar} para todo ticker com alguma ocorrencia bruta (valida ou nao).

    boiler = {indice_do_bloco: [(ini, fim)]}, offsets do texto normalizado (normaliza_mapa),
    vindo de IndiceShingles.trechos. None desliga a E4.
    """
    boiler = boiler or {}
    tn, tidx = normaliza_mapa(titulo)
    segs = []
    for i, b in enumerate(blocos):
        bn, bidx = normaliza_mapa(b)
        if bn.strip():
            segs.append((i, bn, bidx, b))
    qtexto = " ".join([tn] + [s[1] for s in segs])
    brut: dict[tuple[str, int], list] = {}
    for t, r in regras.items():
        qual_ok = r["qual"] is None or any(_achar(qtexto, *q) for q in r["qual"])
        brut[(t, -1)] = _brutas(tn, r, qual_ok)
        for i, bn, _, _ in segs:
            brut[(t, i)] = _brutas(bn, r, qual_ok)
    # E5: bloco com K_BLOCO ou mais tickers distintos em ocorrencia bruta
    panorama = {i for i, _, _, _ in segs if sum(1 for t in regras if brut[(t, i)]) >= K_BLOCO}

    ev: dict[str, EvidenciaPar] = {}
    for t, r in regras.items():
        if not brut[(t, -1)] and not any(brut[(t, i)] for i, _, _, _ in segs):
            continue
        n_titulo = n_titulo_rev = n_corpo = n_corpo_rev = 0
        primeiro = None
        invalidas: dict[str, int] = {}
        for _, _, _, mot in _avalia(tn, titulo, tidx, r, brut[(t, -1)], False, None, False):
            if mot is None:
                n_titulo += 1
                n_titulo_rev += 1
            else:
                invalidas[mot] = invalidas.get(mot, 0) + 1
                if mot in MOTIVOS_REVISAVEIS_TITULO:
                    n_titulo_rev += 1
        for i, bn, bidx, orig in segs:
            for _, _, _, mot in _avalia(
                bn, orig, bidx, r, brut[(t, i)], True, boiler.get(i), i in panorama
            ):
                if mot is None:
                    n_corpo += 1
                    n_corpo_rev += 1
                    if primeiro is None:
                        primeiro = i
                else:
                    invalidas[mot] = invalidas.get(mot, 0) + 1
                    if mot in MOTIVOS_REVISAVEIS_CORPO:
                        n_corpo_rev += 1
        ev[t] = EvidenciaPar(
            ticker=t,
            n_titulo=n_titulo,
            n_corpo=n_corpo,
            n_titulo_revisavel=n_titulo_rev,
            n_corpo_revisavel=n_corpo_rev,
            primeiro_bloco=primeiro,
            outras_empresas_titulo=0,
            invalidas=invalidas,
        )

    # Empresa = codigo CVM (sem codigo, o proprio ticker). PETR3 e PETR4 sao a mesma empresa.
    empresa = {t: str(TICKER_CODE_CVM.get(t, t)) for t in ev}
    com_titulo = {empresa[t] for t, e in ev.items() if e.n_titulo >= 1}
    return {
        t: replace(e, outras_empresas_titulo=len(com_titulo - {empresa[t]}))
        for t, e in ev.items()
    }


def tem_ocorrencia_bruta(titulo: str, texto: str, regras: dict) -> bool:
    """Pre-filtro de armazenamento: ha alguma ocorrencia bruta (antes de E1-E6)
    no titulo ou no texto."""
    tn, _ = normaliza_mapa(titulo)
    xn, _ = normaliza_mapa(texto)
    qtexto = f"{tn} {xn}"
    for r in regras.values():
        qual_ok = r["qual"] is None or any(_achar(qtexto, *q) for q in r["qual"])
        if _brutas(tn, r, qual_ok) or _brutas(xn, r, qual_ok):
            return True
    return False


def trecho_mencoes(texto: str, ticker: str, regras: dict, max_chars: int) -> str:
    """Janelas do texto original em volta das mencoes validas do ticker (E1-E3 e E6), para a juiza.

    Janelas que se sobrepoem sao fundidas. Vazio se nao houver mencao valida. Corta em max_chars.
    """
    r = regras.get(ticker)
    if r is None:
        return ""
    xn, idx = normaliza_mapa(texto)
    qual_ok = r["qual"] is None or any(_achar(xn, *q) for q in r["qual"])
    intervalos = []
    for a, b, _, mot in _avalia(xn, texto, idx, r, _brutas(xn, r, qual_ok), True, None, False):
        if mot is None:
            ini, fim = idx[a], idx[b - 1] + 1
            intervalos.append((max(0, ini - JANELA_TRECHO), min(len(texto), fim + JANELA_TRECHO)))
    if not intervalos:
        return ""
    fundidos: list[tuple[int, int]] = []
    for ini, fim in sorted(intervalos):
        if fundidos and ini <= fundidos[-1][1]:
            fundidos[-1] = (fundidos[-1][0], max(fundidos[-1][1], fim))
        else:
            fundidos.append((ini, fim))
    saida = " ... ".join(re.sub(r"\s+", " ", texto[ini:fim]).strip() for ini, fim in fundidos)
    if len(saida) > max_chars:
        saida = saida[: max(0, max_chars - 3)].rstrip() + "..."
    return saida
