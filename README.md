# bramapa-addon-energia

Addon de dados do [Bramapa](https://github.com/sandhas/bramapa) com **tarifa residencial de
energia elétrica** e **capacidade instalada de geração** por UF e para o Brasil, a partir dos
dados abertos da ANEEL. É o primeiro addon da comunidade mantido **fora** do
[`bramapa-data`](https://github.com/sandhas/bramapa-data): mesmo contrato
(`spec_version = 0`, [`docs/spec/addon.md`](https://github.com/sandhas/bramapa/blob/main/docs/spec/addon.md)),
mesmo validador, só que no seu próprio repositório. Sem código no addon: `manifest.toml` + CSVs.

| Propriedade | O que é | Unidade | Período | Linhas |
|---|---|---|---|---|
| `energia/estado/tarifa_residencial` | Tarifa B1 residencial convencional (TUSD + TE, sem tributos), média simples das concessionárias da UF | R$/MWh | 2010–2026, anual | 454 |
| `energia/pais/tarifa_residencial` | Idem, média simples de todas as concessionárias do país | R$/MWh | 2010–2026, anual | 17 |
| `energia/estado/capacidade_instalada` | Potência instalada de geração na UF, posição de dezembro | MW | 2006–2025, anual | 538 |
| `energia/pais/capacidade_instalada` | Soma das 27 UFs | MW | 2006–2025, anual | 18 |

A `tarifa_residencial` da UF é a regra de exibição (`[[render]]`, choropleth linear); a
capacidade instalada tem choropleth em escala log.

## Fonte e licença

Tudo vem do portal de dados abertos da ANEEL (<https://dadosabertos.aneel.gov.br>, API CKAN,
sem chave), três conjuntos, todos publicados sob **ODbL 1.0** (Open Data Commons Open Database
License) — é o que está em `addon.license` e em [`LICENSE`](LICENSE). As ferramentas deste repo
(`tools/`) são MIT.

| Conjunto ANEEL | Recurso | Vira |
|---|---|---|
| [Tarifas de aplicação das distribuidoras de energia elétrica](https://dadosabertos.aneel.gov.br/dataset/tarifas-distribuidoras-energia-eletrica) | `tarifas-homologadas-distribuidoras-energia-eletrica.csv` (~90 MB) | `tarifa_residencial` |
| [PDD - Plano de Desenvolvimento da Distribuição](https://dadosabertos.aneel.gov.br/dataset/pdd) | `pdd-distribuicao-aneel.csv` | cadastro distribuidora → UF (join) |
| [Capacidade Instalada por Unidade da Federação](https://dadosabertos.aneel.gov.br/dataset/capacidade-instalada-por-unidade-da-federacao) | `capacidade-instalada-geracao-uf.csv` | `capacidade_instalada` |

## Método

Os CSVs em `addons/energia/data/` são **gerados** por `tools/fetch_energia.py` (stdlib, sem
dependências). Não edite à mão; rode o script de novo:

```sh
python tools/fetch_energia.py              # baixa (cache em .cache/) e reescreve os CSVs
python tools/fetch_energia.py --dry-run    # só calcula e relata
```

**Tarifa residencial** (é uma aproximação, leia antes de citar):

1. Da tabela de tarifas homologadas ficam só as linhas `B1` / classe e subclasse `Residencial`
   (ou seja, sem Baixa Renda e Tarifa Social) / modalidade `Convencional` / base
   `Tarifa de Aplicação` (a que é cobrada, não a "Base Econômica") / detalhe e posto
   `Não se aplica` (sem a linha de compensação SCEE). Valor = `VlrTUSD + VlrTE`, em R$/MWh.
   **Sem ICMS, PIS/COFINS e sem bandeiras tarifárias** — a conta de luz é maior que isso.
2. O ano é o de `DatInicioVigencia`. Quando uma distribuidora tem mais de uma resolução no
   ano (reajuste + revisão extraordinária), fica a **última a entrar em vigor** — a tarifa
   vigente no fim do ano. Duas resoluções no mesmo dia (5 casos) são tiradas na média.
3. Cada distribuidora é ligada a uma UF pelo CNPJ via o cadastro do PDD (que traz `SigUF` e
   `DscTipoOutorga`). Dois CNPJs extintos que faltam lá (EDEVP/SP e CODESAM/SC) estão na
   tabela `FALLBACK_DISTRIBUIDORAS` do script, conferidos pela Receita Federal. O join usado
   fica versionado em [`tools/distribuidoras_uf.csv`](tools/distribuidoras_uf.csv) (115
   distribuidoras com tarifa B1 residencial).
4. A UF recebe a **média simples das concessionárias** (`DscTipoOutorga = Concessão`) com
   tarifa naquele ano. As permissionárias (cooperativas de eletrificação rural, ~60 agentes
   pequenos em RS, SC, SP e PR) ficam de fora: senão o RS, com 12 cooperativas e 7
   concessionárias, viraria uma média de cooperativas. Não é ponderada por consumidores nem
   por energia. É um "quanto custa o MWh residencial nesta UF", não um número oficial da ANEEL.
5. `pais:BR` é a mesma média simples sobre todas as concessionárias do país — também não é
   ponderada, não é a tarifa média nacional publicada pela ANEEL.

Lacunas: Amapá não tem concessionária com tarifa B1 iniciando em 2010, 2014, 2023 e 2025 (a
CEA/Equatorial AP teve vigências que pularam o ano-calendário) e o Ceará não tem em 2011; esses
pares UF-ano simplesmente não existem no CSV. A queda geral de 2013 é real (Lei 12.783/2013).

**Capacidade instalada**: para cada UF e ano, a posição do **último mês publicado no ano**
(dezembro; a ANEEL publica só dezembro até 2011 e trimestralmente desde 2012). Se o último mês
disponível é anterior a novembro, o par UF-ano é descartado como incompleto — é o caso do ano
corrente (2026) e de PE em 2013 e RO em 2014. kW → MW. `pais:BR` é a soma das UFs, só nos anos
em que as 27 estão presentes (por isso 2013 e 2014 não têm Brasil). Valores como a ANEEL
publica, sem ajuste (há saltos entre anos que vêm da fonte).

## Validar e buildar

Clone o `bramapa-data` ao lado deste repositório e use o validador de lá; `validate` e `build`
aceitam vários diretórios de addons e tratam todos como um conjunto só (ids únicos entre eles):

```sh
cd ../bramapa-data
uv run bramapa-data validate ../bramapa-addon-energia/addons          # só este addon
uv run bramapa-data validate addons ../bramapa-addon-energia/addons   # junto com os de lá
uv run bramapa-data build addons ../bramapa-addon-energia/addons --out ../bramapa-data-build
uv run bramapa-data inspect ../bramapa-data-build/energia/estado/tarifa_residencial.bmd
```

O build escreve `energia/estado/tarifa_residencial.bmd` etc. ao lado dos `.bmd` do `ibge-core`
e lista tudo num `index.toml` só. Abrindo o motor com esse diretório —
`cargo run --release -- --level ../bramapa-level1 --tileset ../bramapa-tileset --data ../bramapa-data-build`
— o painel da entidade selecionada (clique num estado) mostra, além das propriedades do
`ibge-core`, "Tarifa residencial (B1 convencional)" e "Capacidade instalada de geração": último
valor, sparkline e tabela por ano, com o seletor de intervalo de anos. A choropleth de
`estado/tarifa_residencial` pinta o mapa do nível 1 pela tarifa do ano escolhido.

## Layout

```
addons/energia/manifest.toml       # id, licença, 4 propriedades, 2 regras de render
addons/energia/data/*.csv          # entity,period_start,period_end,value (gerados)
tools/fetch_energia.py             # baixa da ANEEL e recalcula tudo
tools/distribuidoras_uf.csv        # join CNPJ → UF/outorga realmente usado (gerado)
LICENSE                            # ODbL-1.0 (dados) + MIT (ferramentas)
```

Pra atualizar: rode o script, confira o `--dry-run`, suba `version` no manifesto (patch se só
mudou número, minor se entrou ano ou propriedade) e valide contra o `bramapa-data`.
