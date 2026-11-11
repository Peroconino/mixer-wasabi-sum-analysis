"""Leitura das transações (JSON no formato blockchain.com, arquivos sem extensão)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class TxInput:
    txid: str
    index: int
    addr: str
    value: int  # satoshis
    pubkey: str | None = None  # chave pública comprimida (hex), se extraível


@dataclass
class Tx:
    txid: str
    fee: int
    inputs: list[TxInput]
    outputs: list[int]  # valores em satoshis
    out_addrs: list[str] = field(default_factory=list)


def _read_varint(b: bytes, pos: int) -> tuple[int, int]:
    first = b[pos]
    if first < 0xFD:
        return first, pos + 1
    if first == 0xFD:
        return int.from_bytes(b[pos + 1 : pos + 3], "little"), pos + 3
    if first == 0xFE:
        return int.from_bytes(b[pos + 1 : pos + 5], "little"), pos + 5
    return int.from_bytes(b[pos + 1 : pos + 9], "little"), pos + 9


def extract_pubkey(witness_hex: str) -> str | None:
    """Última peça da witness, se for uma chave pública comprimida (33 bytes, 02/03)."""
    if not witness_hex:
        return None
    try:
        raw = bytes.fromhex(witness_hex)
        n, pos = _read_varint(raw, 0)
        last = None
        for _ in range(n):
            ln, pos = _read_varint(raw, pos)
            last = raw[pos : pos + ln]
            pos += ln
    except (ValueError, IndexError):
        return None
    if last is not None and len(last) == 33 and last[0] in (2, 3):
        return last.hex()
    return None


def parse_tx(data: dict) -> Tx:
    inputs = []
    for i in data["inputs"]:
        po = i["prev_out"]
        inputs.append(
            TxInput(
                txid=data["hash"],
                index=i.get("index", len(inputs)),
                addr=po.get("addr") or f"unknown:{data['hash']}:{i.get('index', len(inputs))}",
                value=int(po["value"]),
                pubkey=extract_pubkey(i.get("witness", "")),
            )
        )
    outs = data.get("out", data.get("outputs", []))
    return Tx(
        txid=data["hash"],
        fee=int(data.get("fee", 0)),
        inputs=inputs,
        outputs=[int(o["value"]) for o in outs],
        out_addrs=[o.get("addr", "") for o in outs],
    )


def load_transactions(folder: str | Path) -> tuple[list[Tx], list[tuple[str, str]]]:
    """Retorna (transações, ignorados) onde ignorados = [(nome do arquivo, motivo)]."""
    folder = Path(folder)
    txs: list[Tx] = []
    skipped: list[tuple[str, str]] = []
    for path in sorted(p for p in folder.iterdir() if p.is_file()):
        try:
            txs.append(parse_tx(json.loads(path.read_text())))
        except json.JSONDecodeError as e:
            skipped.append((path.name, f"JSON inválido: {e}"))
        except (KeyError, TypeError, UnicodeDecodeError) as e:
            skipped.append((path.name, f"formato inesperado: {e!r}"))
    return txs, skipped
