"""Indice de boilerplate por shingles: trechos repetidos em muitos posts do mesmo site.

Incremental e causal. Um post entra com `adicionar`, em ordem de data, e a janela de cada
post e (data - 30 d, data], incluindo o proprio post. Um shingle de SHINGLE tokens e
boilerplate quando aparece em N_BOILER posts distintos da janela.

Os offsets devolvidos sao do texto normalizado (normaliza_mapa), o mesmo que o casador usa na E4.
Uma instancia por site.
"""
from __future__ import annotations

import re
from collections import Counter, deque
from collections.abc import Hashable
from datetime import datetime, timedelta

from tcc_scraper.enrich.casador import normaliza_mapa

N_BOILER = 5
JANELA_BOILER_DIAS = 30
SHINGLE = 8


def _shingles(bloco: str, k: int) -> list[tuple[tuple[str, ...], int, int]]:
    """[(tokens, ini, fim)] dos shingles de k tokens do bloco normalizado."""
    bn, _ = normaliza_mapa(bloco)
    toks = [(m.group(0), m.start(), m.end()) for m in re.finditer(r"[a-z0-9]+", bn)]
    return [
        (tuple(t[0] for t in toks[i:i + k]), toks[i][1], toks[i + k - 1][2])
        for i in range(len(toks) - k + 1)
    ]


class IndiceShingles:
    """Janela causal de shingles de um site.

    Uso: para cada post candidato, em ordem de data:
        idx.adicionar(post_key, data, blocos)
        trechos = idx.trechos(post_key, blocos)
    """

    def __init__(
        self,
        n_boiler: int = N_BOILER,
        janela_dias: int = JANELA_BOILER_DIAS,
        shingle: int = SHINGLE,
    ) -> None:
        self._n_boiler = n_boiler
        self._janela = timedelta(days=janela_dias)
        self._k = shingle
        # (data, key, shingles), por data
        self._posts: deque[tuple[datetime, Hashable, set]] = deque()
        self._cnt: Counter = Counter()  # shingle -> quantos posts distintos da janela o contem
        self._relogio: datetime | None = None  # tempo da janela (ultimo adicionar ou expirar)
        self._ultimo: tuple[Hashable, datetime] | None = None  # ultimo post adicionado

    def expirar(self, ate_data: datetime) -> None:
        """Tira da janela os posts com data <= ate_data - 30 d.
        A janela passa a ser (ate_data - 30 d, ate_data].
        """
        if self._ultimo is not None and ate_data < self._ultimo[1]:
            raise ValueError("ate_data nao pode ser anterior ao ultimo post adicionado")
        self._relogio = ate_data
        limite = ate_data - self._janela
        while self._posts and self._posts[0][0] <= limite:
            _, _, sh = self._posts.popleft()
            for c in sh:
                self._cnt[c] -= 1
                if self._cnt[c] == 0:
                    del self._cnt[c]

    def adicionar(self, post_key: Hashable, data: datetime, blocos: list[str]) -> None:
        """Registra o post na janela. Posts devem entrar em ordem de data (sem olhar o futuro)."""
        if self._relogio is not None and data < self._relogio:
            raise ValueError("posts devem entrar em ordem de data (janela causal)")
        self.expirar(data)
        sh = {c for bloco in blocos for c, _, _ in _shingles(bloco, self._k)}
        self._posts.append((data, post_key, sh))
        self._cnt.update(sh)
        self._ultimo = (post_key, data)

    def trechos(self, post_key: Hashable, blocos: list[str]) -> dict[int, list[tuple[int, int]]]:
        """{indice_do_bloco: [(ini, fim)]} dos trechos boilerplate do post.

        So vale logo apos adicionar(post_key), porque a janela precisa ser a do dia do post.
        """
        if self._ultimo is None or post_key != self._ultimo[0] or self._relogio != self._ultimo[1]:
            raise ValueError(
                "trechos so vale logo apos adicionar(post_key), com a janela no dia do post"
            )
        saida: dict[int, list[tuple[int, int]]] = {}
        for bi, bloco in enumerate(blocos):
            spans: list[tuple[int, int]] = []
            for c, a, b in _shingles(bloco, self._k):
                if self._cnt[c] >= self._n_boiler:
                    if spans and a <= spans[-1][1]:
                        spans[-1] = (spans[-1][0], max(spans[-1][1], b))
                    else:
                        spans.append((a, b))
            if spans:
                saida[bi] = spans
        return saida
