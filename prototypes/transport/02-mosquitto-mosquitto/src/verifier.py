#!/usr/bin/env python3
"""Compares what the edge broker accepted with what reached the hub.

Reads the four files the simulator and consumer wrote to $RESULTS_DIR and
prints the verdict block. Everything it prints is derived from those files; it
never estimates.

Definitions used throughout, because the difference matters:

  producidos  frames the source generated (published.jsonl).
  aceptados   frames the edge broker PUBACKed (acks.jsonl). This is the
              denominator for loss: the broker took responsibility for these.
  rechazados  frames the source could not hand over because the local broker
              was dead. A real loss, but the *source's* problem, not the
              transport's.
  recibidos   deliveries at the hub consumer (received.jsonl).
"""

import json
import os
import pathlib
import sys

RESULTS = pathlib.Path(os.environ.get("RESULTS_DIR", "/results"))
PROFILE = os.environ.get("FL_PROFILE", "?")

PHASE_LABELS = {
    "PW": "PW pre-warmup   (esperando al bridge, no se mide)",
    "P0": "P0 warmup       (enlace arriba)",
    "P1": "P1 corte        (enlace caido)",
    "P2": "P2 corte de luz (SIGKILL del broker de borde, enlace caido)",
    "P3": "P3 restauracion (enlace arriba, drenando)",
}


def read_jsonl(name):
    path = RESULTS / name
    if not path.exists():
        return []
    rows = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                # Torn last line if a container died mid-write. Ignore it
                # rather than pretend the file is intact.
                print(f"[verifier] WARNING: torn line ignored in {name}", flush=True)
    return rows


def ranges(seqs):
    """Collapse a sorted iterable of ints into (start, end) runs."""
    out = []
    for s in sorted(seqs):
        if out and s == out[-1][1] + 1:
            out[-1][1] = s
        else:
            out.append([s, s])
    return [(a, b) for a, b in out]


def fmt_pct(part, whole):
    return "0.00%" if not whole else f"{100.0 * part / whole:.2f}%"


def main() -> int:
    attempts = read_jsonl("published.jsonl")
    acks = read_jsonl("acks.jsonl")
    received = read_jsonl("received.jsonl")
    phase_marks = read_jsonl("phases.jsonl")

    if not attempts:
        print("[verifier] FATAL: published.jsonl is empty, nothing to verify")
        return 2

    phase_of = {r["seq"]: r["phase"] for r in attempts}
    t_of = {r["seq"]: r["t"] for r in attempts}
    # PW is the pre-warmup window the harness uses to wait for the bridge to
    # come alive. Those frames are excluded from every headline figure -- they
    # were published before the experiment had a working baseline, so counting
    # them as "lost" would be measuring the harness, not the broker.
    pw = [s for s in sorted(phase_of) if phase_of[s] == "PW"]
    produced = [s for s in sorted(phase_of) if phase_of[s] != "PW"]
    measured = set(produced)
    accepted = sorted({r["seq"] for r in acks} & measured)
    accepted_set = set(accepted)
    refused = [s for s in produced if s not in accepted_set]

    received_seqs = [r["seq"] for r in received if r["seq"] in measured]
    received_set = set(received_seqs)
    dup_count = len(received_seqs) - len(received_set)
    dup_seqs = sorted({s for s in received_seqs if received_seqs.count(s) > 1}) if dup_count else []

    lost = [s for s in accepted if s not in received_set]
    # Deliveries of frames the broker never acknowledged: would mean the ack
    # bookkeeping is wrong, so it is worth surfacing.
    unexpected = sorted(received_set - accepted_set)

    out_of_order = 0
    high = 0
    for s in received_seqs:
        if s < high:
            out_of_order += 1
        else:
            high = s

    t0 = phase_marks[0]["t"] if phase_marks else None
    t_by_phase = {}
    for mark in phase_marks:
        t_by_phase.setdefault(mark["phase"], mark["t"])

    w = 78
    print("=" * w)
    print(f"VEREDICTO -- perfil `{PROFILE}`  |  Mosquitto borde --bridge--> Mosquitto hub")
    print("=" * w)

    print("\nFases (reloj del simulador, t0 = arranque)")
    seen_phases = set()
    for mark in phase_marks:
        ph = mark["phase"]
        if ph in seen_phases:
            continue
        seen_phases.add(ph)
        seqs = [s for s in sorted(phase_of) if phase_of[s] == ph]
        rel = f"t+{mark['t'] - t0:6.1f}s" if t0 else "?"
        span = f"seq {seqs[0]}..{seqs[-1]} ({len(seqs)} frames)" if seqs else "(sin frames)"
        print(f"  {rel}  {PHASE_LABELS.get(ph, ph):<58} {span}")

    print("\nTotales (excluida la ventana PW de pre-warmup: "
          f"{len(pw)} frames descartados del calculo)")
    print(f"  Producidos por la fuente ...................... {len(produced):6d}")
    print(f"    aceptados (PUBACK) por el broker de borde ... {len(accepted):6d}")
    print(f"    rechazados (broker de borde inalcanzable) ... {len(refused):6d}")
    print(f"  Recibidos en el hub (entregas) ................ {len(received_seqs):6d}")
    print(f"    seq unicos ................................... {len(received_set):6d}")
    print(f"  Perdidos (aceptados y nunca llegados) ......... {len(lost):6d}"
          f"   ({fmt_pct(len(lost), len(accepted))} de lo aceptado)")
    print(f"  Duplicados (entregas de un seq ya visto) ...... {dup_count:6d}")
    print(f"  Fuera de orden (seq < maximo ya visto) ........ {out_of_order:6d}")
    if unexpected:
        print(f"  ATENCION: {len(unexpected)} seq recibidos sin PUBACK registrado "
              f"(p.ej. {unexpected[:5]})")

    print("\nPerdidas por fase de publicacion")
    for ph in ("PW", "P0", "P1", "P2", "P3"):
        acc = [s for s in accepted if phase_of.get(s) == ph]
        ref = [s for s in refused if phase_of.get(s) == ph]
        lst = [s for s in lost if phase_of.get(s) == ph]
        if not acc and not ref:
            continue
        print(f"  {ph}: aceptados {len(acc):5d}  perdidos {len(lst):5d} "
              f"({fmt_pct(len(lst), len(acc))})  rechazados en origen {len(ref):5d}")

    gap_ranges = ranges(lost)
    print(f"\nRangos de huecos ({len(gap_ranges)} rangos)")
    if not gap_ranges:
        print("  (ninguno)")
    for a, b in gap_ranges[:25]:
        phases_in = sorted({phase_of.get(s, "?") for s in range(a, b + 1)})
        print(f"  seq {a}..{b}  ({b - a + 1} msgs, fase {'+'.join(phases_in)})")
    if len(gap_ranges) > 25:
        print(f"  ... y {len(gap_ranges) - 25} rangos mas")

    # ---- When did things arrive? -------------------------------------------
    #
    # Splitting P1's arrivals at the P3 mark separates two very different
    # things: messages that crossed the wire before `docker network disconnect`
    # actually tore the veth down (a leak, and a threat to the whole
    # experiment's validity) from messages the bridge genuinely queued and
    # drained after the link came back.
    t_restore = t_by_phase.get("P3")
    recv_t = {}
    for r in received:
        recv_t.setdefault(r["seq"], r["t"])
    leaked = drained = []
    if t_restore:
        p1_accepted = [s for s in accepted if phase_of.get(s) == "P1"]
        leaked = [s for s in p1_accepted if s in recv_t and recv_t[s] < t_restore]
        drained = [s for s in p1_accepted if s in recv_t and recv_t[s] >= t_restore]
        print("\nReparto temporal de las llegadas de P1")
        print(f"  llegaron ANTES de restaurar el enlace (fuga del corte) ... {len(leaked):6d}")
        print(f"  llegaron DESPUES de restaurar (drenaje real de la cola) . {len(drained):6d}")
        if leaked:
            print(f"    la fuga son los seq {leaked[0]}..{leaked[-1]}; ultima llegada "
                  f"{recv_t[leaked[-1]] - t_by_phase['P1']:.2f}s despues de la marca P1")
        if drained:
            first = min(recv_t[s] for s in drained) - t_restore
            last = max(recv_t[s] for s in drained) - t_restore
            print(f"    reconexion del bridge: primer mensaje drenado a t+{first:.1f}s "
                  f"de la restauracion; ultimo a t+{last:.1f}s")
        print(f"  -> profundidad de cola observada del bridge: {len(drained)} mensajes")
        # When did live delivery resume? After the SIGKILL the edge broker
        # cannot even resolve the hub's name while the link is down, so it goes
        # into exponential backoff; anything published during that backoff is
        # only saved if the queue is working.
        p3_arrived = [s for s in accepted if phase_of.get(s) == "P3" and s in recv_t]
        p3_all = [s for s in accepted if phase_of.get(s) == "P3"]
        if p3_arrived:
            first = min(recv_t[s] for s in p3_arrived) - t_restore
            print(f"  entrega en vivo reanudada a t+{first:.1f}s de la restauracion "
                  f"({len(p3_arrived)}/{len(p3_all)} frames de P3 llegaron)")
        elif p3_all:
            print(f"  entrega en vivo NO se reanudo dentro de la ventana de medida "
                  f"(0/{len(p3_all)} frames de P3 llegaron)")

    # ---- Overflow shape: which end of the queue is discarded? --------------
    #
    # Measured on the P1 window only, and in *positions* rather than seq
    # numbers. P1 is the one phase where the link is down and the broker is
    # never restarted, so the only thing that can remove a message from the
    # queue is the cap. Positions make the cap legible: if the cap governs the
    # bridge queue, the first gap lands at position 1000 (the default) or 50
    # (the queue-probe-50 control), not at some timing-dependent number.
    window = [s for s in accepted if phase_of.get(s) == "P1"]
    kept = [s for s in window if s in received_set]
    kept_pos = [i + 1 for i, s in enumerate(window) if s in received_set]
    n = len(window)
    print("\nDesbordamiento de cola (solo fase P1: enlace caido, broker de borde vivo)")
    print(f"  aceptados durante P1 ......... {n:6d}")
    print(f"  sobrevivieron ................ {len(kept):6d}")
    if not window:
        verdict_overflow = "sin datos: no se acepto nada durante P1"
    elif not kept:
        verdict_overflow = ("no se encolo NADA de P1: no es desbordamiento, es que la "
                            "cola del bridge nunca existio para este perfil")
    elif len(kept) == n:
        verdict_overflow = ("no hubo desbordamiento: sobrevivio el 100% de lo aceptado "
                            "durante P1")
    else:
        kept_pos_set = set(kept_pos)
        prefix = 0
        while prefix < len(kept_pos) and kept_pos[prefix] == prefix + 1:
            prefix += 1
        suffix = 0
        while suffix < n and (n - suffix) in kept_pos_set:
            suffix += 1
        first_gap = prefix + 1
        print(f"  prefijo intacto .............. {prefix:6d} mensajes")
        print(f"  sufijo intacto ............... {suffix:6d} mensajes")
        print(f"  primer hueco en la posicion .. {first_gap:6d} de {n}")
        if prefix == len(kept):
            verdict_overflow = (
                f"se conserva el PRINCIPIO de P1 (posiciones 1..{prefix}) y se "
                f"descartan los {n - prefix} MAS NUEVOS -> al desbordar se tira lo "
                f"nuevo: se pierde el presente y se salva el historico"
            )
        elif suffix == len(kept):
            verdict_overflow = (
                f"se conserva el FINAL de P1 (ultimas {suffix} posiciones) y se "
                f"descartan los {n - suffix} MAS VIEJOS -> al desbordar se tira lo "
                f"viejo: se pierde el historico y se salva el presente"
            )
        else:
            verdict_overflow = (
                f"patron mixto: prefijo de {prefix} + sufijo de {suffix} sobre {n}, "
                f"primer hueco en la posicion {first_gap}"
            )
    print(f"  -> {verdict_overflow}")
    if drained:
        print(f"  -> de esos supervivientes, {len(drained)} salieron de la cola del "
              f"bridge tras la restauracion: ese es el tope efectivo medido")

    # ---- Cost of the SIGKILL ----------------------------------------------
    print("\nCoste del SIGKILL (fase P2)")
    pre_kill_accepted = [s for s in accepted if phase_of.get(s) in ("P0", "P1")]
    pre_kill_lost = [s for s in pre_kill_accepted if s not in received_set]
    refused_p2 = [s for s in refused if phase_of.get(s) == "P2"]
    print(f"  aceptados antes del SIGKILL (P0+P1) .......... {len(pre_kill_accepted):6d}")
    print(f"  de esos, perdidos ............................ {len(pre_kill_lost):6d}")
    print(f"  rechazados en origen mientras el broker estaba")
    print(f"    muerto (la fuente no tenia donde dejarlos) . {len(refused_p2):6d}")
    t_kill = t_by_phase.get("P2")
    tail = []
    if pre_kill_lost and pre_kill_accepted:
        # A contiguous tail of the pre-kill queue means "everything since the
        # last autosave", which is the figure autosave_interval controls.
        n_tail = len(pre_kill_lost)
        if pre_kill_lost == pre_kill_accepted[-n_tail:]:
            tail = pre_kill_lost
            if t_kill:
                window_s = t_kill - t_of[tail[0]]
                print(f"  la perdida es la COLA contigua de la cola pre-SIGKILL:")
                print(f"    seq {tail[0]}..{tail[-1]}, es decir los ultimos "
                      f"{window_s:.1f} s antes del SIGKILL")
        else:
            print("  la perdida NO es una cola contigua (ver rangos arriba)")

    payload_bytes = [r.get("bytes", 0) for r in received if r.get("bytes")]
    if payload_bytes:
        avg = sum(payload_bytes) / len(payload_bytes)
        print(f"\nTamano medio de la trama recibida: {avg:.0f} bytes "
              f"(los {len(window)} msgs de P1 ~= {len(window) * avg / 1024:.0f} KiB "
              f"de cola en el borde)")

    qos_seen = sorted({r.get("qos") for r in received if "qos" in r})
    dup_flag = sum(1 for r in received if r.get("dup"))
    print(f"QoS de entrega observado en el hub: {qos_seen}   "
          f"entregas con flag DUP: {dup_flag}")
    if dup_seqs:
        print(f"seq duplicados (primeros 10): {dup_seqs[:10]}")

    print("\n" + "-" * w)
    survived = len(received_set & set(accepted_set))
    print(f"RESUMEN  perfil={PROFILE}  aceptados={len(accepted)}  "
          f"llegados={survived}  perdidos={len(lost)}  "
          f"duplicados={dup_count}  desorden={out_of_order}")
    print(f"RESUMEN  desbordamiento: {verdict_overflow}")
    print("-" * w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
