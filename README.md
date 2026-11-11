# mixer-wasabi-sum

Dado um conjunto de transações Bitcoin (JSON no formato blockchain.com, **um arquivo por transação,
sem extensão**, na pasta `transactions/`), infere quais **entradas pertencem ao mesmo dono**.

## Uso

```bash
uv run mixer-wasabi-sum
```

Opções: `-j/--jobs N` (processos em paralelo; default = nº de CPUs), `--max-group N` (máx. de "pedaços" por dono, default 4), `--fee-margin` (folga sobre a taxa média por
entrada, default 3), `--fee-slack` (folga absoluta em sats), `--unit` (força o valor da saída-unidade).

Paralelismo: com muitas transações, um processo por arquivo; com poucas (ex.: uma CoinJoin grande), a busca de
candidatos de cada transação é dividida entre os processos. O resultado é idêntico com qualquer `-j`.

Saídas em `output/`: `ignored.csv`, `groups.csv` (cada entrada, grupo, k, excesso e confiança) e `clusters.json`
(endereços agrupados entre todas as transações).

## Modelo (duas camadas, para o relatório distinguir)

Cada participante de um CoinJoin recebe `k` saídas de valor `unit` (a saída repetida mais comum; níveis
2x/4x/... incluídos) e paga uma pequena parte da taxa.

- **Modelo A, sem troco:** soma das entradas do dono ≈ `k·unit`.
  Exemplo: entradas 4,3,7,6 e saídas 10,10 → `{3,7}` e `{4,6}`.
- **Modelo B, com troco:** soma das entradas − troco ≈ `k·unit`, onde o troco é uma saída de valor único
  e cada troco pertence a no máximo um dono. **Só é tentado se o A não explicar a transação inteira.**

Etapas (em cada transação):
1. **Evidência forte:** entradas com mesmo endereço (ou mesma chave pública) → mesmo dono.
2. **Candidatos:** subconjuntos minimais cuja soma casa com `k·unit` (A) ou `k·unit + troco` (B).
3. **Portão:** existe uma partição que cobre *todas* as entradas (cada troco usado ≤ 1 vez)? Se não existe, ou
   não deu para decidir, **nada é "certo"** nessa transação (o modelo não vale ou é ambíguo demais).
4. **Se o portão passou:** grupos presentes em *todas* as partições e itens com um único grupo candidato
   são "certo" (marcado com o modelo: `sem troco` ou `com troco`).
5. **Parcimônia ("provável"):** entrada isolada que já fecha `k·unit` sem troco fica sozinha. Só afirma que
   aquela entrada, sozinha, bate com `k·unit`; não liga entradas entre si.

O terminal e o `groups.csv` separam tudo por modelo (colunas `confianca`, `modelo`, `troco`), e o final da
execução imprime quantas transações cada modelo explica e quantas entradas ficam em cada nível.

Opções do modelo B: `--no-change` (desliga), `--change-max-group N` (itens por dono, 1-4, default 3),
`--max-change-candidates N` (teto; acima dele o B fica "indeterminado"). Requer `numpy`.

## Limitações

- Troco: o modelo B assume **um** troco por dono e que o troco é uma saída de valor único. Mais de um troco
  por dono, ou troco igual a outra saída, quebra o modelo (o portão evita afirmar "certo" nesses casos).
- Com muito troco ou muitas entradas o modelo B fica ambíguo ou passa do teto de candidatos ("indeterminado").
- `--max-group` precisa ser ≥ ao maior número de pedaços de um dono; maior que o necessário aumenta a
  ambiguidade e reduz o que é confirmado (nos testes sintéticos, 3 recuperou ~90% e 4 quase nada quando os donos
  tinham ≤3 pedaços).
- A janela de tolerância para `k` grande ainda é larga (`k·(unit − eps)` a `k·unit`); apertá-la exigiria
  enumerar combinações dos níveis de saída, o que explode em CoinJoins grandes.
- Soma de subconjuntos é intrinsecamente ambígua quando há muitas entradas de valores parecidos.
- Níveis 2x/4x/... usam o desconto `eps` detectado nas saídas (ex.: 2x = 2·(unit − eps)).
