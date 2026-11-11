"""Inferência de propriedade comum das entradas de uma transação (CoinJoin).

Cada participante recebe `k` saídas de valor `unit` (ou níveis múltiplos) e, opcionalmente, UMA saída
de troco. Logo as entradas de um mesmo dono somam  k*unit [+ troco] + parte da taxa.

Duas camadas de modelo (o relatório distingue uma da outra):
  A. SEM troco: soma das entradas ~ k*unit.
  B. COM troco: soma das entradas - troco ~ k*unit, cada troco usado por no máximo um dono.
B só é tentado se A não explicar a transação inteira.

Etapas:
  1. Evidência forte: entradas com mesmo endereço / chave pública => mesmo dono ("itens").
  2. Candidatos: subconjuntos minimais de itens cuja soma casa com algum k*unit (+ troco).
  3. PORTÃO: existe uma partição que cobre TODOS os itens? Se não, nada é "certo" (o modelo não vale).
  4. Se o portão passou: grupos presentes em todas as partições e itens com um único grupo candidato
     são "certos". "Provável": entrada isolada que já fecha k*unit sem troco (parcimônia).
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from math import ceil

from .loader import Tx, TxInput

NOCHANGE = -1
Cand = tuple  # (frozenset[int] de itens, índice do troco ou -1)


# --------------------------------------------------------------------------- parâmetros
@dataclass
class Params:
    max_group: int = 4          # máx. de itens por dono (modelo A)
    fee_margin: float = 3.0     # folga sobre a taxa média por entrada
    fee_slack: int = 0          # folga absoluta extra (sats) por grupo
    unit: int | None = None     # saída-unidade (auto: saída repetida mais comum)
    eps: int | None = None      # desconto por unidade nos níveis 2x,4x.. (auto)
    max_enum_candidates: int = 200_000  # só enumera partições se houver <= isso de candidatos
    node_cap: int = 50_000      # limite de nós na busca de partições
    solution_cap: int = 5_000   # limite de partições enumeradas
    jobs: int = 1               # processos para a busca de candidatos dentro de UMA transação
    parsimony: bool = True      # entrada isolada que já fecha k*unit fica sozinha ("provável")
    change: bool = True         # tenta o modelo B (com troco) quando o A não explica a tx
    change_max_group: int = 3   # máx. de itens por dono no modelo B (1..4)
    max_change_candidates: int = 400_000  # acima disso o modelo B fica "indeterminado"


@dataclass
class Item:
    """Conjunto de entradas já sabidamente do mesmo dono (etapa 1)."""
    inputs: list[TxInput]
    reason: str  # "endereço", "chave pública" ou "isolada"

    @property
    def value(self) -> int:
        return sum(i.value for i in self.inputs)


@dataclass
class Group:
    items: list[Item]
    k: int                  # nº de unidades cobertas
    excess: int | None      # soma - troco - k*unit (None se o troco não é determinado)
    status: str             # confiança + como foi inferido
    change: int | None = None   # valor do troco atribuído (None = sem troco ou indeterminado)
    model: str = "sem troco"    # "sem troco" ou "com troco"

    @property
    def inputs(self) -> list[TxInput]:
        return [i for it in self.items for i in it.inputs]


@dataclass
class TxResult:
    txid: str
    unit: int
    eps: int
    items: list[Item]
    groups: list[Group]
    unresolved: list[Item]         # sem candidato algum
    ambiguous: list[Item] = field(default_factory=list)
    n_candidates: int = 0
    partitions: int | None = None
    truncated: bool = False
    model_explains: bool | None = None         # modelo A (sem troco)
    model_explains_change: bool | None = None  # modelo B (com troco); None = não tentado/indeterminado
    change_overflow: bool = False              # B abortado: candidatos demais
    change_tried: bool = False                 # o modelo B foi executado


# --------------------------------------------------------------------------- denominações
def detect_unit(outputs: list[int]) -> tuple[int, int]:
    """(unit, eps). unit = valor repetido mais comum. eps = desconto por unidade nos níveis múltiplos."""
    counts = Counter(outputs)
    repeated = [(c, -v) for v, c in counts.items() if c >= 2]
    if not repeated:
        raise ValueError("nenhuma saída repetida: não há denominação para inferir")
    _, negv = max(repeated)
    unit = -negv
    eps = 0
    for v, cnt in counts.items():
        if cnt < 2:
            continue
        k = round(v / unit)
        if k >= 2 and abs(v - k * unit) <= 0.005 * k * unit:
            eps = max(eps, ceil((k * unit - v) / k))
    return unit, eps


def detect_changes(outputs: list[int]) -> list[int]:
    """Saídas de valor único (não repetido): possíveis trocos."""
    counts = Counter(outputs)
    return sorted(v for v, c in counts.items() if c == 1)


# --------------------------------------------------------------------------- etapa 1
def build_items(inputs: list[TxInput]) -> list[Item]:
    parent = list(range(len(inputs)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    why: dict[int, set[str]] = {}
    for key_name, key_of in (("endereço", lambda i: i.addr), ("chave pública", lambda i: i.pubkey)):
        seen: dict[str, int] = {}
        for idx, inp in enumerate(inputs):
            key = key_of(inp)
            if key is None:
                continue
            if key in seen:
                a, b = find(idx), find(seen[key])
                if a != b:
                    parent[a] = b
                why.setdefault(find(idx), set()).add(key_name)
            else:
                seen[key] = idx
    buckets: dict[int, list[TxInput]] = {}
    for idx, inp in enumerate(inputs):
        buckets.setdefault(find(idx), []).append(inp)
    items = []
    for root, members in buckets.items():
        reasons = set()
        for r, s in why.items():
            if find(r) == root:
                reasons |= s
        items.append(Item(members, " + ".join(sorted(reasons)) if len(members) > 1 else "isolada"))
    return items


# --------------------------------------------------------------------------- janelas de soma
class _Window:
    """Janela de somas aceitas para k unidades usando n entradas brutas."""

    def __init__(self, unit: int, eps: int, fee_per_input: float, p: Params):
        self.unit, self.eps, self.fpi, self.p = unit, eps, fee_per_input, p

    def lo(self, k: int) -> int:
        return k * (self.unit - self.eps) if k >= 2 else self.unit

    def hi(self, k: int, n_inputs: int) -> int:
        return k * self.unit + ceil(self.p.fee_margin * self.fpi * n_inputs) + self.p.fee_slack

    def match(self, total: int, n_inputs: int) -> int | None:
        k = max(1, round(total / self.unit))
        for kk in (k - 1, k, k + 1):
            if kk >= 1 and self.lo(kk) <= total <= self.hi(kk, n_inputs):
                return kk
        return None


# --------------------------------------------------------------------------- candidatos SEM troco
def _search_chunk(args) -> list[tuple[tuple[int, ...], int]]:
    """Trabalhador: explora os prefixos que começam nos itens `firsts` (índices na ordem por valor)."""
    firsts, vals, nins, order, unit, eps, fpi, margin, slack, max_group = args
    win = _Window(unit, eps, fpi, Params(fee_margin=margin, fee_slack=slack))
    N = len(vals)
    sufmax = [0] * (N + 1)
    for i in range(N - 1, -1, -1):
        sufmax[i] = max(nins[i], sufmax[i + 1])
    found: list[tuple[tuple[int, ...], int]] = []

    def rec(start: int, chosen: list[int], total: int, n_in: int):
        if start >= N:
            return
        vmin, vmax = vals[start], vals[-1]
        k = max(1, (total + vmin) // win.unit - 1)
        extra = sufmax[start]
        while True:
            lo_s = win.lo(k) - total
            if lo_s > vmax:
                break
            hi_s = win.hi(k, n_in + extra) - total
            for j in range(bisect_left(vals, lo_s, start), bisect_right(vals, hi_s, start)):
                if win.lo(k) <= total + vals[j] <= win.hi(k, n_in + nins[j]):
                    found.append((tuple(sorted(order[c] for c in chosen + [j])), k))
            k += 1
        if len(chosen) + 2 <= max_group:
            for j in range(start, N):
                rec(j + 1, chosen + [j], total + vals[j], n_in + nins[j])

    for j in firsts:
        rec(j + 1, [j], vals[j], nins[j])
    return found


def _sorted_arrays(items: list[Item]):
    order = sorted(range(len(items)), key=lambda i: items[i].value)
    return order, [items[i].value for i in order], [len(items[i].inputs) for i in order]


def _candidates(items: list[Item], win: _Window, max_group: int, jobs: int = 1) -> dict[tuple, int]:
    """Candidatos SEM troco: {(frozenset(itens), -1): k}."""
    order, vals, nins = _sorted_arrays(items)
    N = len(order)
    found: list[tuple[tuple[int, ...], int]] = []
    for j in range(N):
        k = win.match(vals[j], nins[j])
        if k is not None:
            found.append(((order[j],), k))
    if max_group >= 2:
        common = (vals, nins, order, win.unit, win.eps, win.fpi, win.p.fee_margin, win.p.fee_slack, max_group)
        if jobs > 1 and N >= 40:
            from concurrent.futures import ProcessPoolExecutor

            chunks = [list(range(w, N, jobs * 4)) for w in range(jobs * 4)]
            with ProcessPoolExecutor(max_workers=jobs) as ex:
                for part in ex.map(_search_chunk, [(c, *common) for c in chunks if c]):
                    found.extend(part)
        else:
            found.extend(_search_chunk((list(range(N)), *common)))
    uniq: dict[tuple[int, ...], int] = {}
    for g, k in found:
        uniq.setdefault(g, k)
    return {(frozenset(g), NOCHANGE): k for g, k in sorted(uniq.items())}


# --------------------------------------------------------------------------- candidatos COM troco
def _change_chunk(args):
    """Trabalhador (numpy): para cada (troco, k) procura subconjuntos de até m_c itens cuja soma
    cai em [k*unit + troco, + taxa]. Retorna lista de ((itens), ci, k) ou None se estourou o teto."""
    import numpy as np

    cis, vals, nins, order, unit, eps, fpi, margin, slack, changes, m_c, cap = args
    win = _Window(unit, eps, fpi, Params(fee_margin=margin, fee_slack=slack))
    v = np.asarray(vals, dtype=np.int64)
    nn = np.asarray(nins, dtype=np.int64)
    N = len(v)
    n_cap = m_c * int(nn.max())
    total_all = int(v.sum())
    if m_c >= 2 and N >= 2:
        iu, ju = np.triu_indices(N, 1)
        ps = v[iu] + v[ju]
        srt = np.argsort(ps, kind="stable")
        ps, pi, pj = ps[srt], iu[srt], ju[srt]
        pn = (nn[iu] + nn[ju])[srt]
    found: dict[tuple[tuple[int, ...], int], int] = {}

    def add(idxs, ci, k, S, n_in):
        c = changes[ci]
        if win.lo(k) + c <= S <= win.hi(k, n_in) + c:
            found.setdefault((tuple(sorted(order[int(j)] for j in idxs)), ci), k)

    for ci in cis:
        c = changes[ci]
        kmax = (total_all - c) // max(1, unit - eps)
        for k in range(1, kmax + 1):
            lo_t, hi_t = win.lo(k) + c, win.hi(k, n_cap) + c
            for j in range(int(np.searchsorted(v, lo_t, "left")), int(np.searchsorted(v, hi_t, "right"))):
                add((j,), ci, k, int(v[j]), int(nn[j]))
            if m_c >= 2 and N >= 2:
                for t in range(int(np.searchsorted(ps, lo_t, "left")), int(np.searchsorted(ps, hi_t, "right"))):
                    add((pi[t], pj[t]), ci, k, int(ps[t]), int(pn[t]))
            if m_c >= 3 and N >= 3:
                l = np.searchsorted(ps, lo_t - v, "left")
                r = np.searchsorted(ps, hi_t - v, "right")
                for i in np.nonzero(r > l)[0]:
                    for t in range(int(l[i]), int(r[i])):
                        if pi[t] > i:
                            add((i, pi[t], pj[t]), ci, k, int(v[i] + ps[t]), int(nn[i] + pn[t]))
            if m_c >= 4 and N >= 4:
                l = np.searchsorted(ps, lo_t - ps, "left")
                r = np.searchsorted(ps, hi_t - ps, "right")
                for a in np.nonzero(r > l)[0]:
                    ia, ja = int(pi[a]), int(pj[a])
                    for t in range(int(l[a]), int(r[a])):
                        ib, jb = int(pi[t]), int(pj[t])
                        if ia < ib and ja != ib and ja != jb:
                            add((ia, ja, ib, jb), ci, k, int(ps[a] + ps[t]), int(pn[a] + pn[t]))
            if len(found) > cap:
                return None
    return list(found.items())


def _change_candidates(items, win, changes, m_c, cap, jobs):
    """{(frozenset(itens), ci): k} ou None se estourou `cap`."""
    if not changes or m_c < 1 or not items:
        return {}
    order, vals, nins = _sorted_arrays(items)
    common = (vals, nins, order, win.unit, win.eps, win.fpi, win.p.fee_margin, win.p.fee_slack, changes, m_c, cap)
    ci_all = list(range(len(changes)))
    if jobs > 1 and len(ci_all) >= 8:
        from concurrent.futures import ProcessPoolExecutor

        chunks = [ci_all[w::jobs * 2] for w in range(jobs * 2)]
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            parts = list(ex.map(_change_chunk, [(c, *common) for c in chunks if c]))
    else:
        parts = [_change_chunk((ci_all, *common))]
    out: dict[tuple, int] = {}
    for part in parts:
        if part is None:
            return None
        for (g, ci), k in part:
            out.setdefault((frozenset(g), ci), k)
        if len(out) > cap:
            return None
    return out


# --------------------------------------------------------------------------- minimais
def _minimal(cands: dict[tuple, int]) -> dict[tuple, int]:
    """Descarta grupos que são a união de dois candidatos disjuntos (usando <= 1 troco)."""
    keep = {}
    for (g, c), k in cands.items():
        if len(g) < 2:
            keep[(g, c)] = k
            continue
        members = sorted(g)
        splittable = False
        for r in range(1, len(members)):
            for sub in combinations(members, r):
                a = frozenset(sub)
                b = g - a
                if c == NOCHANGE:
                    ok = (a, NOCHANGE) in cands and (b, NOCHANGE) in cands
                else:
                    ok = ((a, c) in cands and (b, NOCHANGE) in cands) or ((a, NOCHANGE) in cands and (b, c) in cands)
                if ok:
                    splittable = True
                    break
            if splittable:
                break
        if not splittable:
            keep[(g, c)] = k
    return keep


# --------------------------------------------------------------------------- pool de candidatos
class _Pool:
    def __init__(self, cands: dict[tuple, int]):
        self.k = dict(cands)
        self.by_item: dict[int, set] = defaultdict(set)
        self.by_change: dict[int, set] = defaultdict(set)
        for cand in self.k:
            g, c = cand
            for i in g:
                self.by_item[i].add(cand)
            if c != NOCHANGE:
                self.by_change[c].add(cand)

    def __len__(self):
        return len(self.k)

    def _drop(self, cand):
        g, c = cand
        del self.k[cand]
        for i in g:
            self.by_item[i].discard(cand)
        if c != NOCHANGE:
            self.by_change[c].discard(cand)

    def remove_conflicting(self, g: frozenset, c: int):
        """Remove candidatos que tocam algum item de g ou (se c conhecido) usam o mesmo troco."""
        doomed = set()
        for i in g:
            doomed |= self.by_item[i]
        if c != NOCHANGE:
            doomed |= self.by_change[c]
        for cand in doomed:
            if cand in self.k:
                self._drop(cand)


def _enumerate_partitions(rem: set[int], pool: _Pool, p: Params):
    """Cobertura exata de `rem` com candidatos disjuntos, cada troco usado no máx. 1 vez.
    Retorna (nº de soluções, nº de soluções em que cada grupo aparece, truncado?)."""
    by_item = {i: [c for c in pool.by_item[i] if c[0] <= rem] for i in rem}
    n_static = {i: len(v) for i, v in by_item.items()}
    appear: Counter = Counter()
    state = {"sols": 0, "nodes": 0, "trunc": False}

    def dfs(left: frozenset[int], used: frozenset[int], chosen: list):
        if state["trunc"]:
            return
        state["nodes"] += 1
        if state["nodes"] > p.node_cap or state["sols"] >= p.solution_cap:
            state["trunc"] = True
            return
        if not left:
            state["sols"] += 1
            appear.update({g for g, _ in chosen})
            return
        best = min(left, key=lambda i: n_static[i])
        for cand in by_item[best]:
            g, c = cand
            if g <= left and (c == NOCHANGE or c not in used):
                dfs(left - g, used | {c} if c != NOCHANGE else used, chosen + [cand])

    dfs(frozenset(rem), frozenset(), [])
    return state["sols"], appear, state["trunc"]


# --------------------------------------------------------------------------- principal
def analyze_tx(tx: Tx, params: Params | None = None) -> TxResult:
    p = params or Params()
    if p.unit is not None:
        unit, eps = p.unit, (p.eps if p.eps is not None else 0)
    else:
        unit, eps = detect_unit(tx.outputs)
        if p.eps is not None:
            eps = p.eps
    items = build_items(tx.inputs)
    fpi = tx.fee / max(1, len(tx.inputs))
    win = _Window(unit, eps, fpi, p)
    changes = detect_changes(tx.outputs)

    cands_a = _minimal(_candidates(items, win, p.max_group, p.jobs))
    n_cands = len(cands_a)

    groups: list[Group] = []
    remaining = set(range(len(items)))
    info = {"partitions": None, "truncated": False}
    all_items = set(range(len(items)))

    state = {"pool": _Pool(cands_a), "model": "sem troco"}

    def trocos_de(g: frozenset) -> list[int]:
        """Índices de troco (ou -1) com que o grupo g ainda é candidato."""
        return [c for gg, c in state["pool"].by_item[min(g)] if gg == g]

    def commit(g: frozenset, c: int | None, status: str):
        """c: índice do troco (>=0), NOCHANGE, ou None se o troco ficou indeterminado."""
        pool = state["pool"]
        its = [items[i] for i in sorted(g)]
        total = sum(i.value for i in its)
        k = pool.k[(g, c if c is not None else trocos_de(g)[0])]
        if c is None:
            groups.append(Group(its, k, None, status, None, state["model"]))
        elif c == NOCHANGE:
            groups.append(Group(its, k, total - k * unit, status, None, state["model"]))
        else:
            groups.append(Group(its, k, total - changes[c] - k * unit, status, changes[c], state["model"]))
        remaining.difference_update(g)
        pool.remove_conflicting(g, c if c is not None else NOCHANGE)

    def commit_unique(g: frozenset, status: str):
        cl = trocos_de(g)
        commit(g, cl[0] if len(cl) == 1 else None, status)

    def propagate(label: str):
        """Item cujos candidatos apontam para um único grupo => esse grupo é o dono."""
        pool = state["pool"]
        changed = True
        while changed:
            changed = False
            for i in sorted(remaining, key=lambda x: len(pool.by_item[x])):
                gs = {g for g, _ in pool.by_item[i]}
                if len(gs) == 1:
                    commit_unique(next(iter(gs)), f"{label} ({state['model']}; candidato único)")
                    changed = True
                    break

    def commit_in_all(n: int, appear: Counter, label: str):
        pool = state["pool"]
        for g, cnt in list(appear.items()):
            if cnt == n and trocos_de(g):
                commit_unique(g, f"{label} ({state['model']}; presente em todas as {n} partições)")

    def enumerate_cover(label: str):
        pool = state["pool"]
        if not (remaining and len(pool) and len(pool) <= p.max_enum_candidates):
            return
        in_c = {i for i in remaining if pool.by_item[i]}
        n, appear, trunc = _enumerate_partitions(in_c, pool, p)
        if n and not trunc:
            commit_in_all(n, appear, label)

    def commit_singles():
        pool = state["pool"]
        for cand in [c for c in list(pool.k) if len(c[0]) == 1 and c[1] == NOCHANGE]:
            if cand in pool.k:
                commit(cand[0], NOCHANGE, "provável (entrada isolada já fecha k·unidade)")

    def gate(pool: _Pool):
        """(True/False/None, nº partições, appear, truncado): existe partição que cobre todas as entradas?"""
        if not len(pool):
            return False, 0, Counter(), False
        if len(pool) > p.max_enum_candidates:
            return None, 0, Counter(), False
        n, appear, trunc = _enumerate_partitions(set(all_items), pool, p)
        if n > 0:
            return True, n, appear, trunc
        return (None if trunc else False), n, appear, trunc

    # ---- modelo A (sem troco)
    model_a, n, appear, trunc = gate(state["pool"])
    info["partitions"], info["truncated"] = n, trunc
    model_b: bool | None = None
    overflow = False
    tried = False

    def finish_certain(n, appear, trunc):
        if not trunc:
            commit_in_all(n, appear, "certo")
        propagate("certo")
        if p.parsimony:
            commit_singles()
            propagate("provável")
            enumerate_cover("provável")

    if model_a:
        finish_certain(n, appear, trunc)
    else:
        # ---- modelo B (com troco): só se o A não explicou a transação inteira
        if p.change and changes and items:
            tried = True
            extra = _change_candidates(items, win, changes, max(1, min(4, p.change_max_group)),
                                       p.max_change_candidates, p.jobs)
            if extra is None:
                overflow = True
            else:
                union = _minimal({**cands_a, **extra})
                state["pool"], state["model"] = _Pool(union), "com troco"
                n_cands = len(union)
                model_b, n, appear, trunc = gate(state["pool"])
                info["partitions"], info["truncated"] = n, trunc
                if model_b:
                    finish_certain(n, appear, trunc)
        if not model_b and p.parsimony:
            # só afirma "esta entrada, sozinha, fecha k·unidade"; não propaga nada
            state["model"] = "sem troco"
            state["pool"] = _Pool({c: k for c, k in cands_a.items() if c[0] <= remaining})
            commit_singles()

    pool = state["pool"]
    ambiguous_idx = {i for i in remaining if pool.by_item[i]}
    ambiguous = [items[i] for i in sorted(ambiguous_idx)]
    unresolved = [items[i] for i in sorted(remaining - ambiguous_idx)]
    return TxResult(tx.txid, unit, eps, items, groups, unresolved, ambiguous, n_cands,
                    info["partitions"], info["truncated"], model_a, model_b, overflow, tried)
