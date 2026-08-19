#!/usr/bin/env python3
"""One verdict block for the load round.

Reads what the simulator says it got PUBACKed, what the consumer says arrived,
what the commander sent, what the edge received, and the RSS/disk checkpoints
run-load.sh sampled from the host. Prints the four numbers the pre-committed
decision criterion in issue #4 asks for:

  1. RSS slope per queued message, and the 15-day projection.
  2. Whether anything was lost while the backlog drained.
  3. How long the broker took to come back with a large persistence file.
  4. Whether commands issued during the outage arrived, once, in order.
"""

from __future__ import annotations

import json
import os
import pathlib

RESULTS = pathlib.Path(os.environ.get("RESULTS_DIR", "/results"))
MSGS_15D = int(os.environ.get("MSGS_15D", "259200"))
MSGS_30D = int(os.environ.get("MSGS_30D", "518400"))
RSS_BUDGET_MB = float(os.environ.get("RSS_BUDGET_MB", "1024"))


def load(name, default=None):
    try:
        with open(RESULTS / name) as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return default


def load_lines(name):
    out = []
    try:
        with open(RESULTS / name) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    except FileNotFoundError:
        pass
    return out


def expand(ranges):
    out = set()
    for lo, hi in ranges or []:
        out.update(range(lo, hi + 1))
    return out


def phase_of(seq, marks):
    label = "?"
    for m in marks or []:
        if seq >= m["first_seq"]:
            label = m["phase"]
    return label


def rule(title):
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def main() -> None:
    sim = load("sim-final.json") or {}
    recv = load("recv-final.json") or {}
    checkpoints = load_lines("checkpoints.jsonl")
    cmds_sent = load("cmds-sent.json") or {}
    cmds_recv = load("cmds-received.json") or {}
    timings = load("timings.json") or {}

    rule("CARGA: qué se publicó y qué llegó")
    acked = expand(sim.get("acked_ranges"))
    # The consumer reports gaps rather than ranges: with a working transport the
    # gap list is a handful of entries while the range list would be one.
    max_seq = recv.get("max_seq", 0)
    gaps = expand(recv.get("gaps"))
    received = {s for s in range(1, max_seq + 1)} - gaps

    produced = sim.get("produced", 0)
    print(f"  tramas producidas por el simulador : {produced}")
    print(f"  PUBACKeadas por el broker de borde : {len(acked)}")
    print(f"  rechazadas en origen (broker caido): {sim.get('refused', 0)}")
    print(f"  entregas en el hub                 : {recv.get('deliveries', 0)}")
    print(f"  seq unicos en el hub               : {recv.get('unique', 0)}")
    print(f"  duplicados                         : {recv.get('duplicates', 0)}")
    print(f"  entregas fuera de orden            : {recv.get('inversions', 0)}")
    print(f"  QoS observado en el hub            : {recv.get('qos_seen')}")
    if sim.get("fill_seconds"):
        rate = sim.get("target", 0) / sim["fill_seconds"]
        print(f"  ritmo de llenado                   : {rate:.0f} msg/s")

    lost = sorted(acked - received)
    print(f"\n  PERDIDOS (PUBACKeados y nunca llegados): {len(lost)}")
    if acked:
        print(f"  porcentaje sobre lo PUBACKeado         : {100 * len(lost) / len(acked):.4f} %")

    marks = sim.get("marks", [])
    if lost:
        by_phase = {}
        for s in lost:
            by_phase.setdefault(phase_of(s, marks), 0)
            by_phase[phase_of(s, marks)] += 1
        print(f"  reparto por fase                       : {by_phase}")
        # Show the first few contiguous runs so the shape is visible.
        runs, start, prev = [], lost[0], lost[0]
        for s in lost[1:]:
            if s != prev + 1:
                runs.append((start, prev))
                start = s
            prev = s
        runs.append((start, prev))
        print(f"  huecos (primeros 10 de {len(runs)})            :")
        for lo, hi in runs[:10]:
            print(f"      seq {lo}..{hi}  ({hi - lo + 1} tramas, fase {phase_of(lo, marks)})")

    rule("S1: pendiente de RSS por mensaje encolado")
    fill = [c for c in checkpoints if c.get("label", "").startswith("fill")]
    base = next((c for c in checkpoints if c.get("label") == "baseline"), None)
    if base:
        print(f"  RSS en vacio (cola 0)              : {base['rss_kb'] / 1024:.1f} MB")
        print(f"  mosquitto.db en vacio              : {base['db_bytes'] / 1e6:.2f} MB")
    base_acked = base.get("acked", 0) if base else 0
    # `acked` is cumulative PUBACKs since the baseline. During FILL, with the
    # link cut, nothing leaves the broker, so it IS the queue depth -- which is
    # why the slope only uses fill checkpoints. After the link returns it is
    # just a running total and says nothing about what is still queued; the
    # broker's own `en store` column is the one to read there.
    print(f"\n  {'etiqueta':<16}{'acked':>10}{'en store':>10}{'RSS MB':>10}{'db MB':>10}{'B/msg':>9}")
    for c in checkpoints:
        queued = max((c.get("acked") or 0) - base_acked, 0)
        store = c.get("store_count")
        per = f"{(c['rss_kb'] - base['rss_kb']) * 1024 / queued:.0f}" if base and queued > 0 else ""
        print(
            f"  {c['label']:<16}{queued:>10}{(store if store is not None else '-'):>10}"
            f"{c['rss_kb'] / 1024:>10.1f}{c['db_bytes'] / 1e6:>10.1f}{per:>9}"
        )

    # Least squares over every fill checkpoint, not just the endpoints: the
    # broker's allocator grows in steps, so a two-point slope lands wherever
    # the last sample happened to fall between two jumps. Both are printed.
    slope = None
    pts = [(max((c.get("acked") or 0) - base_acked, 0), c["rss_kb"] * 1024.0)
           for c in fill if (c.get("acked") or 0) > base_acked]
    if len(pts) >= 2 and base:
        n = len(pts)
        sx = sum(x for x, _ in pts)
        sy = sum(y for _, y in pts)
        sxx = sum(x * x for x, _ in pts)
        sxy = sum(x * y for x, y in pts)
        denom = n * sxx - sx * sx
        if denom:
            slope = (n * sxy - sx * sy) / denom
            intercept = (sy - slope * sx) / n
            a, b = pts[0], pts[-1]
            two_point = (b[1] - a[1]) / (b[0] - a[0]) if b[0] != a[0] else float("nan")
            print(f"\n  puntos de ajuste                   : {n} (de {a[0]} a {b[0]} mensajes en cola)")
            print(f"  pendiente por minimos cuadrados    : {slope:.0f} B de RSS por mensaje encolado")
            print(f"  pendiente extremo a extremo        : {two_point:.0f} B/mensaje")
            print(f"  ordenada en el origen del ajuste   : {intercept / 1e6:.1f} MB (RSS medido en vacio: {base['rss_kb'] / 1024:.1f} MB)")
            for label, count in (("15 dias", MSGS_15D), ("30 dias", MSGS_30D)):
                proj = (base["rss_kb"] * 1024 + slope * count) / 1e6
                print(f"    proyeccion {label} ({count} msgs): {proj:.0f} MB de RSS")
            proj15 = (base["rss_kb"] * 1024 + slope * MSGS_15D) / 1e6
            verdict = "PASA" if proj15 < RSS_BUDGET_MB else "FALLA"
            print(f"\n  UMBRAL 1 (<{RSS_BUDGET_MB:.0f} MB a 15 dias): {verdict}")

    rule("S2: perdida durante el drenaje")
    drain_lost = [s for s in lost if phase_of(s, marks) == "DRAIN"]
    print(f"  tramas perdidas publicadas en DRAIN: {len(drain_lost)}")
    if timings.get("drain_seconds") is not None:
        print(f"  duracion del drenaje               : {timings['drain_seconds']:.1f} s")
    if timings.get("drain_backlog"):
        n = timings["drain_backlog"]
        secs = timings.get("drain_seconds") or 0
        if secs:
            print(f"  ritmo de drenaje                   : {n / secs:.0f} msg/s")
    print(f"\n  UMBRAL 2 (0 perdidas en DRAIN): {'PASA' if not drain_lost else 'FALLA'}")

    rule("S3: reinicio con una base de persistencia grande")
    if timings.get("restart_seconds") is not None:
        print(f"  db en el momento del SIGKILL       : {timings.get('db_at_kill_bytes', 0) / 1e6:.1f} MB")
        print(f"  SIGKILL -> healthy                 : {timings['restart_seconds']:.1f} s")
        print(f"  perdidas atribuibles al SIGKILL    : {len([s for s in lost if phase_of(s, marks) == 'HOLD'])}")
        print(f"\n  UMBRAL 3 (arranque operable): {'PASA' if timings['restart_seconds'] < 120 else 'FALLA'}")
    else:
        print("  (no medido en esta pasada)")

    rule("S4: camino de vuelta de los comandos")
    sent = cmds_sent.get("count", 0)
    arrivals = cmds_recv.get("arrivals", [])
    ids = [a["cmd_id"] for a in arrivals]
    unique_ids = set(ids)
    print(f"  comandos publicados en el hub      : {sent}")
    print(f"  PUBACKeados por el hub             : {len(cmds_sent.get('acked_by_hub', []))}")
    print(f"  llegados a la Pi                   : {len(arrivals)}")
    print(f"  distintos                          : {len(unique_ids)}")
    print(f"  duplicados                         : {len(ids) - len(unique_ids)}")
    missing = sorted(set(range(1, sent + 1)) - unique_ids)
    print(f"  no llegados                        : {len(missing)}{(' ' + str(missing[:20])) if missing else ''}")
    ordered = ids == sorted(ids)
    print(f"  en orden de emision                : {'si' if ordered else 'NO'}")
    if arrivals:
        print(f"  QoS de entrega                     : {sorted({a['qos'] for a in arrivals})}")
        print(f"  marcados dup                       : {sum(1 for a in arrivals if a['dup'])}")
    ok4 = sent > 0 and not missing and len(ids) == len(unique_ids) and ordered
    print(f"\n  UMBRAL 4 (llegan, una vez, en orden): {'PASA' if ok4 else 'FALLA'}")

    rule("VEREDICTO")
    checks = []
    if slope is not None and base:
        proj15 = (base["rss_kb"] * 1024 + slope * MSGS_15D) / 1e6
        checks.append((f"1 RSS a 15 dias ({proj15:.0f} MB)", proj15 < RSS_BUDGET_MB))
    checks.append(("2 sin perdida al drenar", not drain_lost))
    if timings.get("restart_seconds") is not None:
        checks.append(("3 arranque operable", timings["restart_seconds"] < 120))
    checks.append(("4 comandos de vuelta", ok4))
    for name, ok in checks:
        print(f"  {'PASA ' if ok else 'FALLA'}  {name}")
    print(f"\n  {'TODOS LOS UMBRALES PASAN' if all(o for _, o in checks) else 'AL MENOS UN UMBRAL FALLA'}")


if __name__ == "__main__":
    main()
