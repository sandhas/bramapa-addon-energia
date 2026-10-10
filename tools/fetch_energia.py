#!/usr/bin/env python3
"""Fetch the ``energia`` addon series from ANEEL open data and rewrite the addon CSVs.

Public CKAN portal (https://dadosabertos.aneel.gov.br), no key needed, pure
stdlib. Run from the repo root::

    python tools/fetch_energia.py             # download (cached in .cache/), rewrite addons/energia/data/*.csv
    python tools/fetch_energia.py --dry-run   # compute and report, do not write
    python tools/fetch_energia.py --no-download   # fail instead of downloading when .cache/ is incomplete

Datasets used (all ODbL, "Open Data Commons Open Database License"):

* **Tarifas de aplicação das distribuidoras de energia elétrica**
  (``tarifas-distribuidoras-energia-eletrica``, resource
  ``tarifas-homologadas-distribuidoras-energia-eletrica.csv``, ~90 MB, ``;``,
  decimal comma, UTF-8). One row per (distributor, resolução homologatória,
  subgroup, modality, class, subclass, detail, posto). We keep
  ``DscSubGrupo = B1``, ``DscClasse = DscSubClasse = Residencial`` (the plain
  residential tariff: no Baixa Renda / Tarifa Social), ``DscModalidadeTarifaria
  = Convencional``, ``DscBaseTarifaria = Tarifa de Aplicação`` (the tariff
  actually billed, not the "Base Econômica"), ``DscDetalhe = Não se aplica``
  (no SCEE / net-metering line) and ``NomPostoTarifario = Não se aplica``.
  Value = ``VlrTUSD + VlrTE`` in R$/MWh, **without** taxes (ICMS, PIS/COFINS)
  and without bandeiras tarifárias.

* **PDD - Plano de Desenvolvimento da Distribuição**
  (``pdd``, resource ``pdd-distribuicao-aneel.csv``). Used only as the
  distributor registry: ``NumCPFCNPJ -> (SigUF, DscTipoOutorga)``. Each
  distributor has one UF there (the UF of its concession/permission area).
  ``FALLBACK_DISTRIBUIDORAS`` covers the few CNPJs present in the tariff table
  but absent from the PDD (extinct companies).

* **Capacidade Instalada por Unidade da Federação**
  (``capacidade-instalada-por-unidade-da-federacao``, resource
  ``capacidade-instalada-geracao-uf.csv``, latin-1). Generation capacity in kW
  by UF and reference month (December only up to 2011, roughly quarterly since
  2012).

Method (also in README.md):

* ``estado/tarifa_residencial``: for each (distributor, year of
  ``DatInicioVigencia``) take the tariff whose validity starts **last** in that
  year (the one in force at year end; if two resolutions start on the same day,
  their mean). Then, per UF and year, the **simple mean over the
  concessionárias** (``DscTipoOutorga = Concessão``) with a B1 residential
  tariff that year. Permissionárias (rural cooperatives, ~60 small agents in
  RS/SC/SP/PR) are excluded so that a UF with twelve cooperatives is not
  averaged as if it had twelve equal-size distributors. It is not weighted by
  consumers or energy: it is an approximation of "what a residential MWh costs
  in this UF".
* ``pais/tarifa_residencial``: the same simple mean over every concessionária in
  the country.
* ``estado/capacidade_instalada``: the latest reference month of each year
  (December when published; years whose last month is before November are
  skipped as incomplete, i.e. the current year), converted kW -> MW.
* ``pais/capacidade_instalada``: sum of the UFs.
"""

from __future__ import annotations

import argparse
import csv
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "addons" / "energia" / "data"
CACHE_DIR = REPO_ROOT / ".cache"
CSV_HEADER = ["entity", "period_start", "period_end", "value"]

PORTAL = "https://dadosabertos.aneel.gov.br/dataset"
SOURCES = {
    # name in .cache/ : (dataset id, resource id, encoding)
    "tarifas-homologadas-distribuidoras-energia-eletrica.csv": (
        "5a583f3e-1646-4f67-bf0f-69db4203e89e",
        "fcf2906c-7c32-4b9b-a637-054e7a5234f4",
        "utf-8",
    ),
    "pdd-distribuicao-aneel.csv": (
        "6838f13d-7ea5-4482-a2b9-1515fa56ef42",
        "30b8fbb7-4d7a-49d1-8985-97e9bb7d65e0",
        "utf-8",
    ),
    "capacidade-instalada-geracao-uf.csv": (
        "cec20fdd-97e4-40a8-870f-c63339f5d8b7",
        "6fbee0f8-2617-4879-a69a-6b7892f12dad",
        "latin-1",
    ),
}

# CNPJs that appear in the tariff table but not in the PDD registry. UF checked
# against the Receita Federal registry (razão social / município da sede).
FALLBACK_DISTRIBUIDORAS = {
    # Empresa de Distribuição de Energia Vale Paranapanema S.A. (SP), absorbed
    # into Energisa Sul-Sudeste in 2018.
    "07297359000111": ("EDEVP", "SP", "Concessão"),
    # Cooperativa de Distribuição de Energia Elétrica Santa Maria, Benedito Novo (SC).
    "11810343000138": ("CODESAM", "SC", "Permissão"),
}

UF_CODE = {
    "RO": "11", "AC": "12", "AM": "13", "RR": "14", "PA": "15", "AP": "16", "TO": "17",
    "MA": "21", "PI": "22", "CE": "23", "RN": "24", "PB": "25", "PE": "26", "AL": "27", "SE": "28", "BA": "29",
    "MG": "31", "ES": "32", "RJ": "33", "SP": "35",
    "PR": "41", "SC": "42", "RS": "43",
    "MS": "50", "MT": "51", "GO": "52", "DF": "53",
}

TARIFF_FILTER = {
    "DscSubGrupo": "B1",
    "DscClasse": "Residencial",
    "DscSubClasse": "Residencial",
    "DscModalidadeTarifaria": "Convencional",
    "DscBaseTarifaria": "Tarifa de Aplicação",
    "DscDetalhe": "Não se aplica",
    "NomPostoTarifario": "Não se aplica",
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


# -- download ----------------------------------------------------------------


def resource_url(dataset: str, resource: str, name: str) -> str:
    return f"{PORTAL}/{dataset}/resource/{resource}/download/{name}"


def ensure_cached(name: str, cache_dir: Path, download: bool) -> Path:
    path = cache_dir / name
    if path.is_file() and path.stat().st_size > 0:
        log(f"cache hit: {path}")
        return path
    if not download:
        sys.exit(f"error: {path} is missing and --no-download was given")
    dataset, resource, _ = SOURCES[name]
    url = resource_url(dataset, resource, name)
    log(f"GET {url}")
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "bramapa-addon-energia/fetch_energia"})
    try:
        with urllib.request.urlopen(req, timeout=600) as resp, tmp.open("wb") as out:
            while chunk := resp.read(1 << 20):
                out.write(chunk)
    except urllib.error.URLError as exc:
        tmp.unlink(missing_ok=True)
        sys.exit(f"error: download of {name} failed: {exc}")
    tmp.replace(path)
    log(f"saved {path} ({path.stat().st_size} bytes)")
    return path


def read_semicolon_csv(path: Path, encoding: str):
    with path.open(encoding=encoding, newline="") as fh:
        yield from csv.DictReader(fh, delimiter=";")


def decimal(text: str) -> float:
    """ANEEL writes ``1234,56`` and sometimes ``,00``; empty means zero."""
    text = text.strip().replace(".", "").replace(",", ".")
    if text in ("", "."):
        return 0.0
    return float(text)


# -- distributors ------------------------------------------------------------


def load_distribuidoras(path: Path) -> dict[str, tuple[str, str, str]]:
    """``{cnpj: (sigla, uf, outorga)}`` from the PDD registry plus the fallback table."""
    out: dict[str, tuple[str, str, str]] = {}
    for row in read_semicolon_csv(path, SOURCES[path.name][2]):
        cnpj = row["NumCPFCNPJ"].strip().zfill(14)
        uf = row["SigUF"].strip()
        outorga = row["DscTipoOutorga"].strip()
        sigla = row["SigAgente"].strip()
        if uf not in UF_CODE:
            sys.exit(f"error: PDD row with unknown UF {uf!r}: {row}")
        if cnpj in out and out[cnpj][1] != uf:
            sys.exit(f"error: PDD gives two UFs for CNPJ {cnpj}: {out[cnpj][1]} and {uf}")
        out[cnpj] = (sigla, uf, outorga)
    for cnpj, info in FALLBACK_DISTRIBUIDORAS.items():
        out.setdefault(cnpj, info)
    log(f"distribuidoras: {len(out)} CNPJs ({sum(1 for v in out.values() if v[2] == 'Concessão')} concessões)")
    return out


# -- tariffs -----------------------------------------------------------------


def residential_tariffs(path: Path) -> dict[tuple[str, str], float]:
    """``{(cnpj, year): R$/MWh}`` — the B1 residential conventional tariff in force at year end."""
    # (cnpj, year) -> (start date, [values starting on that date])
    latest: dict[tuple[str, str], tuple[str, list[float]]] = {}
    seen = kept = 0
    for row in read_semicolon_csv(path, SOURCES[path.name][2]):
        seen += 1
        if any(row[k].strip() != v for k, v in TARIFF_FILTER.items()):
            continue
        if row["DscUnidadeTerciaria"].strip() != "MWh":
            sys.exit(f"error: B1 row not in MWh: {row}")
        kept += 1
        cnpj = row["NumCNPJDistribuidora"].strip().zfill(14)
        start = row["DatInicioVigencia"].strip()
        key = (cnpj, start[:4])
        value = decimal(row["VlrTUSD"]) + decimal(row["VlrTE"])
        cur = latest.get(key)
        if cur is None or start > cur[0]:
            latest[key] = (start, [value])
        elif start == cur[0]:
            cur[1].append(value)
    ties = sum(1 for _, vals in latest.values() if len(vals) > 1)
    log(f"tarifas: {seen} rows read, {kept} B1 residencial convencional, {len(latest)} distributor-years, {ties} same-day ties averaged")
    return {key: sum(vals) / len(vals) for key, (_, vals) in latest.items()}


def mean_by_uf(
    tariffs: dict[tuple[str, str], float], registry: dict[str, tuple[str, str, str]]
) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], str], list[str]]:
    """Simple mean over concessionárias per (UF, year) and for Brazil; also the unknown CNPJs."""
    by_uf: dict[tuple[str, str], list[float]] = defaultdict(list)
    by_year: dict[str, list[float]] = defaultdict(list)
    unknown: set[str] = set()
    for (cnpj, year), value in tariffs.items():
        info = registry.get(cnpj)
        if info is None:
            unknown.add(cnpj)
            continue
        _, uf, outorga = info
        if outorga != "Concessão":
            continue
        by_uf[(uf, year)].append(value)
        by_year[year].append(value)
    estado = {(f"estado:{UF_CODE[uf]}", year): f"{sum(v) / len(v):.2f}" for (uf, year), v in by_uf.items()}
    pais = {("pais:BR", year): f"{sum(v) / len(v):.2f}" for year, v in by_year.items()}
    return estado, pais, sorted(unknown)


# -- installed capacity ------------------------------------------------------


def installed_capacity(path: Path) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], str]]:
    """``{(entity, year): MW}`` for UFs (last published month of the year) and for Brazil (sum)."""
    last: dict[tuple[str, str], tuple[int, float]] = {}
    for row in read_semicolon_csv(path, SOURCES[path.name][2]):
        uf = row["SigUF"].strip()
        if uf not in UF_CODE:
            sys.exit(f"error: capacity row with unknown UF {uf!r}: {row}")
        year = row["AnoReferencia"].strip()
        month = int(row["MesReferencia"])
        kw = decimal(row["MdaPotenciaInstaladakW"])
        key = (uf, year)
        if key not in last or month > last[key][0]:
            last[key] = (month, kw)
    # A (UF, year) counts as a year-end reading only when its last published
    # month is November or December; earlier months mean the year is still
    # being published (the current year) or that UF skipped the December
    # snapshot that year.
    estado: dict[tuple[str, str], str] = {}
    total: dict[str, float] = defaultdict(float)
    count: dict[str, int] = defaultdict(int)
    dropped: dict[str, list[str]] = defaultdict(list)
    for (uf, year), (month, kw) in sorted(last.items()):
        if month < 11:
            dropped[year].append(f"{uf}@{month:02d}")
            continue
        estado[(f"estado:{UF_CODE[uf]}", year)] = f"{kw / 1000:.3f}"
        total[year] += kw
        count[year] += 1
    for year, what in sorted(dropped.items()):
        log(f"capacidade: {year}: no year-end reading for {', '.join(what)}; dropped")
    # Brazil = sum of the UFs, only when all 27 are present that year.
    pais = {("pais:BR", year): f"{kw / 1000:.3f}" for year, kw in total.items() if count[year] == len(UF_CODE)}
    partial = sorted(y for y in total if count[y] != len(UF_CODE))
    if partial:
        log(f"capacidade: pais:BR omitted for {partial} (not all 27 UFs have a year-end reading)")
    return estado, pais


# -- output ------------------------------------------------------------------


def write_csv(path: Path, data: dict[tuple[str, str], str], dry_run: bool) -> None:
    rows = sorted(data.items())
    years = sorted({y for (_, y) in data})
    entities = {e for (e, _) in data}
    log(
        f"{path.relative_to(REPO_ROOT)}: {len(rows)} rows, {len(entities)} entities, "
        f"years {years[0]}..{years[-1]} ({len(years)} distinct)"
    )
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(CSV_HEADER)
        for (entity, year), value in rows:
            writer.writerow([entity, year, year, value])


def write_registry(path: Path, registry: dict[str, tuple[str, str, str]], used: set[str], dry_run: bool) -> None:
    """The distributor -> UF join actually used, for reviewers (tools/distribuidoras_uf.csv)."""
    rows = sorted((registry[c][1], registry[c][0], c, registry[c][2]) for c in used if c in registry)
    log(f"{path.relative_to(REPO_ROOT)}: {len(rows)} distribuidoras with a B1 residential tariff")
    if dry_run:
        return
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["uf", "sigla", "cnpj", "outorga", "fonte"])
        for uf, sigla, cnpj, outorga in rows:
            fonte = "fallback" if cnpj in FALLBACK_DISTRIBUIDORAS else "pdd"
            writer.writerow([uf, sigla, cnpj, outorga, fonte])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="compute and report; do not write CSVs")
    parser.add_argument("--no-download", action="store_true", help="only use files already in --cache-dir")
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR, help=f"download cache (default {CACHE_DIR})")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR, help=f"output directory (default {DATA_DIR})")
    args = parser.parse_args(argv)

    paths = {name: ensure_cached(name, args.cache_dir, not args.no_download) for name in SOURCES}

    registry = load_distribuidoras(paths["pdd-distribuicao-aneel.csv"])
    tariffs = residential_tariffs(paths["tarifas-homologadas-distribuidoras-energia-eletrica.csv"])
    estado_tarifa, pais_tarifa, unknown = mean_by_uf(tariffs, registry)
    if unknown:
        sys.exit(
            "error: distributor CNPJ(s) with a tariff but no UF; add them to FALLBACK_DISTRIBUIDORAS: "
            + ", ".join(unknown)
        )
    expected = {f"estado:{c}" for c in UF_CODE.values()}
    for year in sorted({y for (_, y) in estado_tarifa}):
        missing = expected - {e for (e, y) in estado_tarifa if y == year}
        if missing:
            log(f"note: tarifa {year} has no concessionária with a B1 tariff in {sorted(missing)}")
    write_csv(args.data_dir / "estado_tarifa_residencial.csv", estado_tarifa, args.dry_run)
    write_csv(args.data_dir / "pais_tarifa_residencial.csv", pais_tarifa, args.dry_run)
    write_registry(REPO_ROOT / "tools" / "distribuidoras_uf.csv", registry, {c for (c, _) in tariffs}, args.dry_run)

    estado_cap, pais_cap = installed_capacity(paths["capacidade-instalada-geracao-uf.csv"])
    write_csv(args.data_dir / "estado_capacidade_instalada.csv", estado_cap, args.dry_run)
    write_csv(args.data_dir / "pais_capacidade_instalada.csv", pais_cap, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
