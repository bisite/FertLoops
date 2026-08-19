"""One verdict block for the NATS load round.

Deliberately narrower than the Mosquitto one. Under the decision criterion
agreed on issue #4, Mosquitto wins unless it fails a threshold, so this run is
comparative rather than exhaustive. It answers the two questions where NATS
could plausibly beat Mosquitto at this horizon:

  1. What does a 15- to 30-day backlog cost the leaf in RSS and in disk?
     JetStream keeps it in a file store on the SSD; Mosquitto keeps it in RAM.
  2. Does the ~26 s mirror resume round one measured with a 1 000-message
     backlog stay constant, or does it scale with the size of the backlog?

The control command path (S4) is NOT measured here and is reported as such:
it would need a second stream mirrored in the opposite direction, which is
exactly the topology cost being weighed, and building it is only worth doing
if Mosquitto fails its own S4.
"""

from __future__ import annotations

import json
import os
import pathlib

RESULTS = pathlib.Path(os.environ.get("RESULTS_DIR", "/results"))
MSGS_15D = int(os.environ.get("MSGS_15D", "259200"))
MSGS_30D = int(os.environ.get("MSGS_30D", "518400"))


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
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
    except FileNotFoundError:
        pass
    return out


def rule(title):
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def main() -> None:
    sim = load("sim-final.json") or {}
    recv = load("recv-final.json") or {}
    checkpoints = load_lines("checkpoints.jsonl")
    mirror = load_lines("mirror-state.jsonl")
    timings = load("timings.json") or {}

    rule("CARGA: qué se publicó y qué llegó")
    flushed = sim.get("acked", 0)
    print(f"  tramas producidas por el simulador : {sim.get('produced', 0)}")
    print(f"  confirmadas por flush en el leaf   : {flushed}")
    print(f"  fallos de publicacion / flush      : {sim.get('refused', 0)}")
    print(f"  estado final del stream de borde   : {sim.get('stream_final')}")
    print(f"  entregas en el hub                 : {recv.get('deliveries', 0)}")
    print(f"  seq unicos en el hub               : {recv.get('unique', 0)}")
    print(f"  duplicados                         : {recv.get('duplicates', 0)}")
    print(f"  entregas fuera de orden            : {recv.get('inversions', 0)}")
    if sim.get("fill_seconds"):
        print(f"  ritmo de llenado                   : {sim['target'] / sim['fill_seconds']:.0f} msg/s")

    gaps = recv.get("gaps") or []
    lost = sum(hi - lo + 1 for lo, hi in gaps if lo <= flushed)
    print(f"\n  PERDIDOS (confirmados por flush y nunca llegados): {lost}")
    if flushed:
        print(f"  porcentaje sobre lo confirmado                  : {100 * lost / flushed:.4f} %")
    for lo, hi in gaps[:10]:
        print(f"      hueco seq {lo}..{hi}  ({hi - lo + 1} tramas)")

    rule("S1: coste de un backlog real en el leaf (RSS y disco)")
    base = next((c for c in checkpoints if c.get("label") == "baseline"), None)
    base_acked = base.get("acked", 0) if base else 0
    print(f"\n  {'etiqueta':<16}{'acked':>10}{'en stream':>11}{'RSS MB':>10}{'store MB':>10}{'B/msg':>9}")
    for c in checkpoints:
        queued = max((c.get("acked") or 0) - base_acked, 0)
        per = f"{(c['rss_kb'] - base['rss_kb']) * 1024 / queued:.0f}" if base and queued > 0 else ""
        print(
            f"  {c['label']:<16}{queued:>10}{(c.get('stream_msgs') if c.get('stream_msgs') is not None else '-'):>11}"
            f"{c['rss_kb'] / 1024:>10.1f}{c['store_bytes'] / 1e6:>10.1f}{per:>9}"
        )

    fill = [c for c in checkpoints if c.get("label", "").startswith("fill")]
    pts = [(max((c.get("acked") or 0) - base_acked, 0), c["rss_kb"] * 1024.0)
           for c in fill if (c.get("acked") or 0) > base_acked]
    disk_pts = [(max((c.get("acked") or 0) - base_acked, 0), float(c["store_bytes"]))
                for c in fill if (c.get("acked") or 0) > base_acked]
    for label, points, unit in (("RSS", pts, "RSS"), ("disco", disk_pts, "store")):
        if len(points) >= 2 and base:
            n = len(points)
            sx = sum(x for x, _ in points); sy = sum(y for _, y in points)
            sxx = sum(x * x for x, _ in points); sxy = sum(x * y for x, y in points)
            denom = n * sxx - sx * sx
            if denom:
                slope = (n * sxy - sx * sy) / denom
                print(f"\n  pendiente de {label:<6} (minimos cuadrados, {n} puntos): {slope:.0f} B por mensaje")
                for horizon, count in (("15 dias", MSGS_15D), ("30 dias", MSGS_30D)):
                    b0 = base["rss_kb"] * 1024 if unit == "RSS" else base["store_bytes"]
                    print(f"    proyeccion {horizon} ({count} msgs): {(b0 + slope * count) / 1e6:.0f} MB")

    rule("S2: reanudacion del espejo con un backlog real")
    if timings.get("catchup_seconds") is not None:
        print(f"  reconexion -> primera entrega nueva: {timings.get('first_delivery_seconds', '?')} s")
        print(f"  reconexion -> hub al dia           : {timings['catchup_seconds']} s")
    if timings.get("drain_backlog"):
        secs = timings.get("catchup_seconds") or 0
        if secs:
            print(f"  ritmo de recuperacion              : {timings['drain_backlog'] / float(secs):.0f} msg/s")
    stalls = [m for m in mirror if m.get("lag")]
    if stalls:
        print(f"  lag maximo observado del espejo    : {max(m['lag'] for m in stalls)}")
    errs = {str(m.get("error")) for m in mirror if m.get("error") and m.get("error") != "None"}
    if errs:
        print(f"  errores reportados por el espejo   : {sorted(errs)[:5]}")

    rule("S3: reinicio del leaf con un file store grande")
    if timings.get("restart_seconds") is not None:
        print(f"  store en el momento del SIGKILL    : {timings.get('store_at_kill_bytes', 0) / 1e6:.1f} MB")
        print(f"  SIGKILL -> leaf listo              : {timings['restart_seconds']} s")

    rule("NO MEDIDO EN ESTA RONDA")
    print("  S4, el camino de vuelta de los comandos. Necesita un segundo stream")
    print("  espejado en sentido inverso, que es precisamente el coste de topologia")
    print("  que se esta evaluando. Solo merece construirse si Mosquitto falla su S4.")
    print("  S5, corte de corriente real: necesita hardware.")


if __name__ == "__main__":
    main()
