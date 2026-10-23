# Composição do Ibovespa: carteiras oficiais quadrimestrais

## Origem

Os arquivos `IBOV_<início>_<fim>.csv` são cópias das carteiras teóricas do Ibovespa publicadas
pela B3, reunidas e distribuídas pelo projeto MIT **ibov-data-from-b3**:

- Repositório: <https://github.com/paccafelipe/ibov-data-from-b3>
- Pasta de origem no repositório: `data/ibov_carteiras/`
- Licença: MIT (texto completo no arquivo `LICENSE` do repositório de origem)
- Autor do repositório: paccafelipe
- Data do download: 2026-10-23

Os dados são da B3. O projeto de origem apenas os organiza; a atribuição acima é mantida como
exigido pela licença MIT.

## Conteúdo

- Um arquivo por carteira quadrimestral, de jan/2021 a set/2024 (12 arquivos).
- Colunas: `Codigo`, `Acao`, `Tipo`, `Qtde Teorica`, `Part %`.
- Os arquivos de 2019 e 2020 não são usados pelo projeto e não foram copiados.

## Como o projeto usa os arquivos

O módulo `tcc_scraper.entrega.composicao_ibov` lê estes arquivos e monta a tabela
`v1-composicao_ibov`. O início de vigência de cada carteira é o primeiro dia útil (segunda a
sexta, sem considerar feriados) do mês do arquivo (jan, mai ou set), salvo as datas conhecidas
abaixo, que valem no lugar da regra:

| Arquivo (mês) | Início de vigência |
|---|---|
| 2023-05 | 2023-05-02 |
| 2023-09 | 2023-09-04 |
| 2024-01 | 2024-01-02 |
| 2024-05 | 2024-05-06 |
| 2024-09 | 2024-09-02 |

As datas no nome do arquivo são aproximadas. A partir de set/2024, a composição é reconstruída
com os rebalanceamentos, os eventos extraordinários e as trocas de código posteriores
(`REBALANCEAMENTOS_IBOV`, `EVENTOS_IBOV_EXTRAORDINARIOS` e `MUDANCAS_TICKER` em `config.py`), e
não por novos arquivos.
