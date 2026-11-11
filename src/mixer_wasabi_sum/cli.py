"""CLI: lê transactions/, infere donos comuns das entradas e agrupa endereços."""

from __future__ import annotations

import argparse
import csv
import json
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from collections import defaultdict
from pathlib import Path

from .loader import load_transactions
from .solver import Params, TxResult, analyze_tx


class UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _run(args):
    tx, params = args
    try:
        return tx, analyze_tx(tx, params), None
    except ValueError as e:
        return tx, None, str(e)


def analyze_all(txs, params, jobs):
    """Muitas transações: um processo por transação. Poucas: sequencial, paralelizando por dentro."""
    if jobs > 1 and len(txs) >= jobs:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            yield from ex.map(_run, [(t, replace(params, jobs=1)) for t in txs])
    else:
        for t in txs:
            yield _run((t, replace(params, jobs=jobs)))


def short(h: str, n: int = 10) -> str:
    return h[:n] + "…" if len(h) > n else h


def _tri(v):
    return {True: "sim", False: "não", None: "indeterminado"}[v]


def summarize(res: TxResult, verbose: bool) -> str:
    multi = [g for g in res.groups if sum(len(i.inputs) for i in g.items) > 1]
    n_in = sum(len(i.inputs) for i in res.items)
    certos = [g for g in res.groups if g.status.startswith("certo")]
    prov = [g for g in res.groups if g.status.startswith("provável")]
    if res.change_overflow:
        b = "indeterminado (candidatos demais)"
    elif res.change_tried:
        b = _tri(res.model_explains_change)
    else:
        b = "não tentado" + (" (o sem-troco já explicou)" if res.model_explains else "")
    lines = [
        f"tx {short(res.txid, 16)}  unidade={res.unit} sats  eps={res.eps}  "
        f"entradas={n_in}  itens(etapa 1)={len(res.items)}  candidatos={res.n_candidates}",
        f"  modelo SEM troco explica a tx: {_tri(res.model_explains)} | modelo COM troco explica: {b}",
        f"  donos inferidos: {len(res.groups)} (certos: {len(certos)} [com troco: {sum(g.model == 'com troco' for g in certos)}], "
        f"prováveis: {len(prov)}; com >1 entrada: {len(multi)}) "
        f"| ambíguos: {len(res.ambiguous)} itens | sem solução: {len(res.unresolved)} itens",
    ]
    if verbose:
        for g in multi:
            parts = " + ".join(str(i.value) for it in g.items for i in it.inputs)
            ex = "?" if g.excess is None else f"{g.excess:+}"
            ch = f" troco={g.change}" if g.change else ""
            lines.append(f"    [{g.status}] k={g.k:<3} excesso={ex}{ch}  {parts}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "folder",
        nargs="?",
        default="transactions",
        help="pasta com as transações (JSON, sem extensão)",
    )
    ap.add_argument("-o", "--out", default="output", help="pasta de saída")
    ap.add_argument(
        "--max-group",
        type=int,
        default=4,
        help="máx. de itens por dono na busca de soma (default 4)",
    )
    ap.add_argument(
        "--fee-margin",
        type=float,
        default=3.0,
        help="folga sobre taxa média por entrada (default 3)",
    )
    ap.add_argument(
        "--fee-slack", type=int, default=0, help="folga absoluta extra em sats"
    )
    ap.add_argument("--unit", type=int, help="força o valor da saída-unidade (sats)")
    ap.add_argument(
        "--no-change",
        action="store_true",
        help="NÃO tenta o modelo com troco (só o modelo sem troco)",
    )
    ap.add_argument(
        "--change-max-group",
        type=int,
        default=3,
        help="máx. de itens por dono no modelo com troco (1-4, default 3)",
    )
    ap.add_argument(
        "--max-change-candidates",
        type=int,
        default=400_000,
        help="teto de candidatos do modelo com troco",
    )
    ap.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=os.cpu_count() or 1,
        help="processos em paralelo (default: nº de CPUs)",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    params = Params(
        max_group=a.max_group,
        fee_margin=a.fee_margin,
        fee_slack=a.fee_slack,
        unit=a.unit,
        change=not a.no_change,
        change_max_group=a.change_max_group,
        max_change_candidates=a.max_change_candidates,
    )
    txs, skipped = load_transactions(a.folder)
    n_files = len(txs) + len(skipped)
    if not txs:
        raise SystemExit(f"nenhuma transação encontrada em {a.folder}/")

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    uf = UnionFind()
    evidence: dict[tuple[str, str], set[str]] = defaultdict(set)
    rows = []
    model_stats: dict = defaultdict(int)
    level_stats: dict = defaultdict(int)

    for tx, res, err in analyze_all(txs, params, a.jobs):
        if err:
            skipped.append((tx.txid, err))
            continue
        model_stats[(res.model_explains, res.model_explains_change)] += 1
        for g in res.groups:
            level_stats[(g.status.split(" (")[0], g.model)] += sum(
                len(it.inputs) for it in g.items
            )
        level_stats[("ambíguo", "-")] += sum(len(it.inputs) for it in res.ambiguous)
        level_stats[("sem solução", "-")] += sum(
            len(it.inputs) for it in res.unresolved
        )
        print(summarize(res, a.verbose))
        for inp in tx.inputs:
            uf.find(inp.addr)
        for item in res.items:  # endereço/pubkey comum (evidência forte)
            for other in item.inputs[1:]:
                uf.union(item.inputs[0].addr, other.addr)
        for label, pend in (
            ("ambíguo (vários grupos possíveis)", res.ambiguous),
            ("sem solução no modelo (troco/fora do modelo)", res.unresolved),
        ):
            for item in pend:
                for inp in item.inputs:
                    rows.append(
                        {
                            "txid": tx.txid,
                            "grupo": "",
                            "input_index": inp.index,
                            "endereco": inp.addr,
                            "valor": inp.value,
                            "k_unidades": "",
                            "excesso": "",
                            "tamanho_grupo": "",
                            "confianca": label,
                            "modelo": "-",
                            "troco": "",
                        }
                    )
        for gi, g in enumerate(res.groups):
            ins = g.inputs
            for other in ins[1:]:
                uf.union(ins[0].addr, other.addr)
            for inp in ins:
                rows.append(
                    {
                        "txid": tx.txid,
                        "grupo": gi,
                        "input_index": inp.index,
                        "endereco": inp.addr,
                        "valor": inp.value,
                        "k_unidades": g.k,
                        "excesso": "" if g.excess is None else g.excess,
                        "tamanho_grupo": len(ins),
                        "confianca": g.status,
                        "modelo": g.model,
                        "troco": g.change if g.change else "",
                    }
                )

    clusters: dict[str, set[str]] = defaultdict(set)
    for addr in list(uf.parent):
        clusters[uf.find(addr)].add(addr)
    multi = sorted(
        (sorted(c) for c in clusters.values() if len(c) > 1), key=len, reverse=True
    )

    with open(out / "groups.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["txid"])
        w.writeheader()
        w.writerows(rows)
    (out / "clusters.json").write_text(json.dumps({"clusters": multi}, indent=2))
    with open(out / "ignored.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["arquivo", "motivo"])
        w.writerows(skipped)
    print(
        f"\nArquivos lidos: {n_files} | analisados: {n_files - len(skipped)} | ignorados: {len(skipped)}"
    )
    for name, why in skipped:
        print(f"  ignorado: {short(name, 16)} — {why}")
    ntx = sum(model_stats.values())
    a_sim = sum(v for (a, b), v in model_stats.items() if a)
    b_sim = sum(v for (a, b), v in model_stats.items() if not a and b)
    nada = ntx - a_sim - b_sim
    print(
        "\n=== Transações explicadas por cada modelo (existe partição de todas as entradas) ==="
    )
    print(f"  modelo SEM troco: {a_sim}/{ntx}")
    print(f"  modelo COM troco (só as que o sem-troco não explicou): {b_sim}/{ntx}")
    print(f"  nenhum dos dois (não explica ou indeterminado): {nada}/{ntx}")
    print("=== Entradas por nível de confiança e modelo ===")
    total_in = sum(level_stats.values())
    for (lvl, mod), n_in in sorted(level_stats.items(), key=lambda kv: -kv[1]):
        print(
            f"  {lvl:<12} {mod:<10} {n_in:>8} entradas ({100 * n_in / max(1, total_in):.1f}%)"
        )
    print(
        f"{len(multi)} clusters com >1 endereço (maior: {len(multi[0]) if multi else 0}). Saída em {out}/"
    )


if __name__ == "__main__":
    main()
