"""Compares what the simulator published against what reached the hub.

Reads $RESULTS_DIR/published.jsonl and $RESULTS_DIR/received.jsonl and prints the
verdict block: totals, gaps grouped into ranges and attributed to a phase,
duplicates, reordering, and which end of the queue got discarded on overflow.

Report text is Spanish because it is pasted verbatim into the prototype README,
which the team reads. Identifiers stay English, per AGENTS.md.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time

RESULTS_DIR = pathlib.Path(os.environ.get("RESULTS_DIR", "/results"))
PROFILE = os.environ.get("PROFILE", "unknown")
NANOMQ_IMAGE = os.environ.get("NANOMQ_IMAGE", "unknown")
PHASES = json.loads(os.environ.get("PHASES_JSON") or "[]")


def read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def phase_of(timestamp: float) -> str:
    for phase in PHASES:
        if phase["start"] <= timestamp < phase["end"]:
            return phase["name"]
    if PHASES and timestamp >= PHASES[-1]["end"]:
        return PHASES[-1]["name"]
    return "?"


def group_ranges(values: list[int]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for value in sorted(values):
        if ranges and value == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], value)
        else:
            ranges.append((value, value))
    return ranges


def fmt_range(low: int, high: int) -> str:
    return str(low) if low == high else f"{low}-{high}"


def main() -> int:
    published = read_jsonl(RESULTS_DIR / "published.jsonl")
    received = read_jsonl(RESULTS_DIR / "received.jsonl")

    meta_path = RESULTS_DIR / "simulator.meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}

    attempted = {r["seq"]: r for r in published}
    acked = {seq: r for seq, r in attempted.items() if r.get("acked")}
    no_conn = {seq: r for seq, r in attempted.items() if r.get("rc") == "no_conn"}

    arrivals = [r for r in received if r.get("seq") is not None]
    arrival_order = [r["seq"] for r in arrivals]
    first_recv: dict[int, float] = {}
    counts: dict[int, int] = {}
    for record in arrivals:
        seq = record["seq"]
        counts[seq] = counts.get(seq, 0) + 1
        first_recv.setdefault(seq, record["t_recv"])

    unique_received = set(counts)
    missing = sorted(set(acked) - unique_received)
    unexpected = sorted(unique_received - set(acked))
    duplicated = sorted(seq for seq, n in counts.items() if n > 1)
    extra_copies = sum(n - 1 for n in counts.values() if n > 1)
    dup_flagged = sum(1 for r in arrivals if r.get("dup"))

    inversions = 0
    max_backward_jump = 0
    high_water = None
    for seq in arrival_order:
        if high_water is not None and seq < high_water:
            inversions += 1
            max_backward_jump = max(max_backward_jump, high_water - seq)
        high_water = seq if high_water is None else max(high_water, seq)

    # ------------------------------------------------------------- phase table
    phase_rows = []
    for phase in PHASES:
        name = phase["name"]
        in_phase = [s for s, r in attempted.items() if phase["start"] <= r["t_pub"] < phase["end"]]
        acked_in_phase = [s for s in in_phase if s in acked]
        got = [s for s in acked_in_phase if s in unique_received]
        phase_rows.append(
            {
                "name": name,
                "attempted": len(in_phase),
                "acked": len(acked_in_phase),
                "received": len(got),
                "lost": len(acked_in_phase) - len(got),
            }
        )

    # -------------------------------------------------- overflow / discard end
    # The outage window is everything published between the start of the first
    # phase flagged link_down and the end of the last one. Nothing published in
    # there can reach the hub live, so whatever DID arrive was held by the edge
    # broker -- and where those survivors sit inside the window says which end of
    # the queue was thrown away.
    down_phases = [p for p in PHASES if p.get("link_down")]
    overflow: dict[str, object] = {"evaluable": False}
    if down_phases:
        window_start = min(p["start"] for p in down_phases)
        window_end = max(p["end"] for p in down_phases)
        window = sorted(
            s
            for s, r in attempted.items()
            if window_start <= r["t_pub"] < window_end and s in acked
        )
        survivors = [s for s in window if s in unique_received]
        overflow["window_size"] = len(window)
        overflow["survivors"] = len(survivors)
        if window and survivors and len(survivors) < len(window):
            positions = [window.index(s) for s in survivors]
            half = len(window) / 2.0
            head = sum(1 for p in positions if p < half)
            tail = len(positions) - head
            overflow.update(
                {
                    "evaluable": True,
                    "first_survivor_seq": survivors[0],
                    "last_survivor_seq": survivors[-1],
                    "window_first_seq": window[0],
                    "window_last_seq": window[-1],
                    "survivors_in_first_half": head,
                    "survivors_in_second_half": tail,
                    "verdict": (
                        "descarta los MAS VIEJOS (conserva la cola: lo mas reciente)"
                        if tail > head
                        else "descarta los MAS NUEVOS (conserva la cabeza: lo mas antiguo)"
                    ),
                }
            )
        elif window and not survivors:
            overflow["note"] = "no sobrevivio ningun mensaje del corte: no hay extremo que medir"
        elif window and len(survivors) == len(window):
            overflow["note"] = "sobrevivio todo el corte: la cola no se desbordo"

    # ------------------------------------------------------------------ output
    bar = "=" * 78
    print(bar)
    print(f"VEREDICTO  03-nanomq-mosquitto  perfil={PROFILE}")
    print(f"imagen de borde: {NANOMQ_IMAGE}")
    print(f"generado: {time.strftime('%Y-%m-%d %H:%M:%S%z')}")
    print(bar)
    print()
    print("-- Totales ------------------------------------------------------------")
    print(f"  intentos de publicacion (simulador)      : {len(attempted)}")
    print(f"  aceptados por el broker de borde (PUBACK): {len(acked)}")
    print(f"  nunca entregados a paho (broker caido)   : {len(no_conn)}")
    print(f"  llegados al hub (mensajes, con repetidos): {len(arrivals)}")
    print(f"  llegados al hub (seq unicos)             : {len(unique_received)}")
    if acked:
        rate = 100.0 * len(unique_received & set(acked)) / len(acked)
        print(f"  tasa de entrega sobre lo aceptado        : {rate:.2f} %")
    if meta:
        print(
            f"  cadencia configurada                     : "
            f"{meta.get('rate_per_second')} msg/s durante {meta.get('run_seconds')} s "
            f"(qos={meta.get('qos')})"
        )
    print()

    print("-- Por fase (sobre lo aceptado por el borde) --------------------------")
    if phase_rows:
        print(f"  {'fase':<26} {'intentos':>9} {'aceptados':>10} {'llegados':>9} {'perdidos':>9}")
        for row in phase_rows:
            print(
                f"  {row['name']:<26} {row['attempted']:>9} {row['acked']:>10} "
                f"{row['received']:>9} {row['lost']:>9}"
            )
    else:
        print("  (sin fases declaradas)")
    print()

    print("-- Huecos ------------------------------------------------------------")
    print(f"  seq aceptados que nunca llegaron: {len(missing)}")
    if missing:
        ranges = group_ranges(missing)
        print(f"  rangos ({len(ranges)}):")
        for low, high in ranges[:40]:
            phase = phase_of(attempted[low]["t_pub"])
            size = high - low + 1
            print(f"    {fmt_range(low, high):<20} {size:>6} msg   fase: {phase}")
        if len(ranges) > 40:
            print(f"    ... y {len(ranges) - 40} rangos mas")
    print()

    print("-- Duplicados --------------------------------------------------------")
    print(f"  seq llegados mas de una vez : {len(duplicated)}")
    print(f"  copias extra totales        : {extra_copies}")
    print(f"  mensajes con flag MQTT DUP  : {dup_flagged}")
    if duplicated:
        print(f"  ejemplos: {duplicated[:15]}")
    print()

    print("-- Desorden ----------------------------------------------------------")
    print(f"  llegadas fuera de secuencia : {inversions}")
    print(f"  mayor salto hacia atras     : {max_backward_jump} seq")
    print()

    print("-- Comportamiento al desbordar ---------------------------------------")
    if not down_phases:
        print("  (esta corrida no declara fases con el enlace caido)")
    else:
        print(f"  mensajes aceptados durante el corte : {overflow.get('window_size')}")
        print(f"  de ellos, llegaron al hub           : {overflow.get('survivors')}")
        if overflow.get("evaluable"):
            print(
                f"  ventana del corte  : seq {overflow['window_first_seq']}"
                f"..{overflow['window_last_seq']}"
            )
            print(
                f"  supervivientes     : seq {overflow['first_survivor_seq']}"
                f"..{overflow['last_survivor_seq']}"
            )
            print(
                f"  reparto            : {overflow['survivors_in_first_half']} en la primera "
                f"mitad / {overflow['survivors_in_second_half']} en la segunda"
            )
            print(f"  VERDICTO           : {overflow['verdict']}")
        else:
            print(f"  {overflow.get('note', 'sin datos suficientes')}")
    print()

    if unexpected:
        print("-- Anomalias ---------------------------------------------------------")
        print(f"  seq llegados al hub que el borde nunca confirmo: {len(unexpected)}")
        print(f"  ejemplos: {unexpected[:15]}")
        print()

    print(bar)

    summary = {
        "profile": PROFILE,
        "nanomq_image": NANOMQ_IMAGE,
        "attempted": len(attempted),
        "acked": len(acked),
        "no_conn": len(no_conn),
        "received_messages": len(arrivals),
        "received_unique": len(unique_received),
        "missing": len(missing),
        "missing_ranges": [fmt_range(a, b) for a, b in group_ranges(missing)],
        "duplicated_seqs": len(duplicated),
        "extra_copies": extra_copies,
        "dup_flagged": dup_flagged,
        "out_of_order": inversions,
        "max_backward_jump": max_backward_jump,
        "phases": phase_rows,
        "overflow": overflow,
        "unexpected": len(unexpected),
    }
    (RESULTS_DIR / f"verdict-{PROFILE}.json").write_text(json.dumps(summary, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
