"""Compares what the simulator published against what the hub consumer received.

Reads the JSONL files both sides wrote into the shared results volume plus the
phase boundaries the harness passes in via the PHASES env var, and prints the
verdict block that goes into the README.

Metrics, per the shared protocol in ../README.md:
  * published vs received totals
  * gaps: which seq never arrived, grouped into ranges and attributed to a phase
  * duplicates: seq that arrived more than once
  * reordering: arrivals that went backwards in seq
  * overflow behaviour: whether the oldest or the newest messages were dropped
"""

from __future__ import annotations

import collections
import json
import os

import common

PHASES = json.loads(os.environ.get("PHASES", "[]"))


def phase_of(timestamp: float) -> str:
    for name, start, end in PHASES:
        if start <= timestamp < end:
            return name
    if PHASES and timestamp < PHASES[0][1]:
        return "pre"
    return "post"


def group_ranges(values: list[int]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for value in sorted(values):
        if ranges and value == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], value)
        else:
            ranges.append((value, value))
    return ranges


def main() -> None:
    profile = common.profile()
    published = common.read_jsonl("published.jsonl")
    received = common.read_jsonl("received.jsonl")
    edge_state = common.read_jsonl("edge-stream-state.jsonl")
    mirror_state = common.read_jsonl("mirror-state.jsonl")
    pub_events = common.read_jsonl("publisher-events.jsonl")

    pub_by_seq = {rec["seq"]: rec for rec in published}
    all_seqs = sorted(pub_by_seq)
    # A frame the publisher itself gave up on is not a transport loss, so the
    # two are counted separately everywhere below.
    ok_seqs = [s for s in all_seqs if pub_by_seq[s]["status"] in ("sent", "acked")]
    failed_seqs = [s for s in all_seqs if pub_by_seq[s]["status"] == "failed"]

    recv_seqs = [rec["seq"] for rec in received if rec.get("seq") is not None]
    recv_counts = collections.Counter(recv_seqs)
    recv_unique = set(recv_counts)

    missing = [s for s in ok_seqs if s not in recv_unique]
    duplicates = {s: c for s, c in recv_counts.items() if c > 1}
    unexpected = sorted(recv_unique - set(all_seqs))

    # -- reordering, in arrival order
    out_of_order = 0
    max_backwards = 0
    high_water = 0
    for rec in sorted(received, key=lambda r: r["order"]):
        seq = rec.get("seq")
        if seq is None:
            continue
        if seq < high_water:
            out_of_order += 1
            max_backwards = max(max_backwards, high_water - seq)
        else:
            high_water = seq

    # -- per phase
    phase_names = [p[0] for p in PHASES] + ["pre", "post"]
    per_phase: dict[str, dict[str, int]] = {
        name: {"published": 0, "publish_failed": 0, "received": 0, "lost": 0} for name in phase_names
    }
    for seq in all_seqs:
        phase = phase_of(pub_by_seq[seq]["t_gen"])
        bucket = per_phase.setdefault(
            phase, {"published": 0, "publish_failed": 0, "received": 0, "lost": 0}
        )
        if pub_by_seq[seq]["status"] == "failed":
            bucket["publish_failed"] += 1
            continue
        bucket["published"] += 1
        if seq in recv_unique:
            bucket["received"] += 1
        else:
            bucket["lost"] += 1

    # -- overflow classification, over the whole outage window (P1 + P2)
    outage_phases = {"P1", "P2"}
    window = [s for s in ok_seqs if phase_of(pub_by_seq[s]["t_gen"]) in outage_phases]
    window_missing = [s for s in window if s not in recv_unique]
    overflow_verdict, overflow_detail = classify_overflow(window, window_missing)

    # -- direct edge-stream evidence (jetstream-mirror only)
    edge_evidence = []
    # Samples taken before the first publish report first_seq 0 on an empty
    # stream, which would look like "the oldest message was dropped"; drop them.
    numeric = [r for r in edge_state if r.get("messages")]
    if numeric:
        peak = max(r["messages"] for r in numeric)
        first_seq_hi = max(r["first_seq"] for r in numeric)
        last_seq_hi = max(r["last_seq"] for r in numeric)
        edge_evidence.append(f"edge stream peak messages held : {peak}")
        edge_evidence.append(f"edge stream first_seq reached   : {first_seq_hi}")
        edge_evidence.append(f"edge stream last_seq reached    : {last_seq_hi}")
        if first_seq_hi > 1:
            edge_evidence.append(
                f"first_seq left 1 and reached {first_seq_hi} => the edge stream hit its cap and "
                "dropped its OLDEST messages (discard: old)"
            )
        else:
            edge_evidence.append(
                "first_seq stayed at 1 => the edge stream never hit its cap, nothing dropped at the edge"
            )
    errored = [r for r in edge_state if "err" in r]
    if errored:
        edge_evidence.append(
            f"edge stream_info errors        : {len(errored)} "
            f"({collections.Counter(r['err'] for r in errored).most_common()})"
        )

    mirror_evidence = []
    mirror_numeric = [r for r in mirror_state if "messages" in r]
    if mirror_numeric:
        max_lag = max((r.get("mirror_lag") or 0) for r in mirror_numeric)
        max_idle = max((r.get("mirror_active_ns") or 0) for r in mirror_numeric)
        errors = [r["mirror_error"] for r in mirror_numeric if r.get("mirror_error")]
        mirror_evidence.append(f"mirror max reported lag        : {max_lag} msgs")
        mirror_evidence.append(f"mirror max idle (source quiet) : {max_idle / 1e9:.1f} s")
        mirror_evidence.append(f"mirror errors reported         : {len(errors)}")
        if errors:
            mirror_evidence.append(f"mirror first error             : {errors[0]}")

    attempt_errors = sum(r.get("attempt_errors", 0) for r in pub_events if r.get("kind") == "summary")
    conn_events = collections.Counter(
        r["kind"] for r in pub_events if r.get("kind") in ("disconnected", "reconnected", "error")
    )

    # -- report
    line = "=" * 72
    print(line)
    print(f" FertLoops prototype 01 / native NATS  |  profile = {profile}")
    print(line)
    print(" PHASES")
    if PHASES:
        base = PHASES[0][1]
        for name, start, end in PHASES:
            print(f"   {name:<3} t+{start - base:7.1f}s .. t+{end - base:7.1f}s  ({end - start:.1f}s)")
    print()
    print(" PUBLISHER (edge simulator)")
    print(f"   frames generated                    : {len(all_seqs)}  (seq {all_seqs[0] if all_seqs else 0}..{all_seqs[-1] if all_seqs else 0})")
    print(f"   frames the publisher considers sent : {len(ok_seqs)}")
    print(f"   frames the publisher knows failed   : {len(failed_seqs)}")
    print(f"   individual publish attempts errored : {attempt_errors}")
    print(f"   client connection events            : {dict(conn_events) or 'none'}")
    lags = [r["t_done"] - r["t_gen"] for r in published if "t_done" in r]
    if lags:
        print(f"   max in-process outbox lag           : {max(lags):.1f}s")
        print(f"   frames still in the outbox at the end: {len(all_seqs) - len(lags)}")
    print()
    print(" CONSUMER (hub)")
    print(f"   frames received (raw)               : {len(received)}")
    print(f"   distinct seq received               : {len(recv_unique)}")
    print()
    print(" RESTORE (from the moment the link came back)")
    for detail in restore_evidence(published, received):
        print(f"   {detail}")
    print()
    print(" LOSS")
    pct = (100.0 * len(missing) / len(ok_seqs)) if ok_seqs else 0.0
    print(f"   seq sent but never received         : {len(missing)}  ({pct:.2f}% of sent)")
    ranges = group_ranges(missing)
    print(f"   gap ranges                          : {len(ranges)}")
    for lo, hi in ranges[:20]:
        phases_in_gap = sorted({phase_of(pub_by_seq[s]["t_gen"]) for s in range(lo, hi + 1) if s in pub_by_seq})
        print(f"     seq {lo}-{hi}  ({hi - lo + 1} frames)  generated in {'+'.join(phases_in_gap)}")
    if len(ranges) > 20:
        print(f"     ... and {len(ranges) - 20} more ranges")
    print()
    print(" DUPLICATES")
    print(f"   seq received more than once         : {len(duplicates)}")
    if duplicates:
        sample = sorted(duplicates.items())[:10]
        print(f"   sample (seq: times)                 : {dict(sample)}")
    print(f"   seq received but never published    : {len(unexpected)}")
    # A PubAck with duplicate=true is the server saying "I already have this
    # Nats-Msg-Id"; it is the only direct proof that dedup actually fired rather
    # than simply never being needed.
    dedup_hits = [r["seq"] for r in published if r.get("duplicate")]
    print(f"   acks flagged duplicate by the server: {len(dedup_hits)}")
    retried = [r["seq"] for r in published if r.get("attempts", 1) > 1]
    print(f"   frames that needed a retry          : {len(retried)}")
    print()
    print(" ORDERING")
    print(f"   arrivals out of sequence            : {out_of_order}")
    print(f"   largest backwards jump              : {max_backwards} seq")
    print()
    print(" OVERFLOW BEHAVIOUR")
    print(f"   classification                      : {overflow_verdict}")
    for detail in overflow_detail:
        print(f"   {detail}")
    for detail in edge_evidence:
        print(f"   {detail}")
    for detail in mirror_evidence:
        print(f"   {detail}")
    print()
    print(" PER PHASE")
    print(f"   {'phase':<6}{'sent':>8}{'pub_fail':>10}{'received':>10}{'lost':>8}{'lost%':>8}")
    for name in [p[0] for p in PHASES] + ["pre", "post"]:
        bucket = per_phase.get(name)
        if not bucket or (bucket["published"] == 0 and bucket["publish_failed"] == 0):
            continue
        lost_pct = (100.0 * bucket["lost"] / bucket["published"]) if bucket["published"] else 0.0
        print(
            f"   {name:<6}{bucket['published']:>8}{bucket['publish_failed']:>10}"
            f"{bucket['received']:>10}{bucket['lost']:>8}{lost_pct:>7.1f}%"
        )
    print()
    print(" VERDICT")
    for statement in verdict_lines(
        profile, ok_seqs, missing, failed_seqs, duplicates, out_of_order, overflow_verdict, attempt_errors, conn_events
    ):
        print(f"   {statement}")
    print(line)


def restore_evidence(published: list[dict], received: list[dict]) -> list[str]:
    """How long the hub took to start receiving again after the link came back.

    This is the number that decides whether a mirror is usable in the field: a
    stream that eventually catches up in 20 minutes is a different product from
    one that catches up in 20 seconds.
    """
    p3 = next((p for p in PHASES if p[0] == "P3"), None)
    if p3 is None:
        return ["P3 boundary unknown, cannot measure restore latency"]
    t3 = p3[1]
    after = sorted((r for r in received if r["t_recv"] >= t3), key=lambda r: r["t_recv"])
    if not after:
        return [
            f"nothing arrived at all in the {p3[2] - t3:.0f}s after the link was restored",
        ]
    first = after[0]
    lines = [
        f"first arrival after reconnect       : +{first['t_recv'] - t3:.1f}s (seq {first['seq']})",
        f"frames delivered after reconnect    : {len(after)}",
    ]
    # The backlog is what was published before the link came back but only
    # arrived after; that is the part the edge store actually rescued.
    pub_time = {r["seq"]: r.get("t_done", r["t_gen"]) for r in published}
    backlog = [r for r in after if pub_time.get(r["seq"], t3) < t3]
    if backlog:
        span = backlog[-1]["t_recv"] - backlog[0]["t_recv"]
        rate = len(backlog) / span if span > 0 else float("inf")
        lines.append(f"backlog frames recovered            : {len(backlog)}")
        lines.append(f"backlog drained in                  : {span:.1f}s ({rate:.0f} msg/s)")
        lines.append(
            f"caught up at                        : +{backlog[-1]['t_recv'] - t3:.1f}s after reconnect"
        )
    else:
        lines.append("backlog frames recovered            : 0 (nothing published during the outage came back)")
    return lines


def classify_overflow(window: list[int], window_missing: list[int]) -> tuple[str, list[str]]:
    if not window:
        return "not-measured", ["no frames were generated during the outage window"]
    if not window_missing:
        return "no-overflow", [
            f"all {len(window)} frames generated during P1+P2 arrived at the hub",
        ]
    missing_set = set(window_missing)
    prefix = 0
    for seq in window:
        if seq in missing_set:
            prefix += 1
        else:
            break
    suffix = 0
    for seq in reversed(window):
        if seq in missing_set:
            suffix += 1
        else:
            break
    detail = [
        f"outage window                       : seq {window[0]}..{window[-1]} ({len(window)} frames)",
        f"lost inside the window              : {len(window_missing)}",
        f"lost as a leading run (oldest)      : {prefix}",
        f"lost as a trailing run (newest)     : {suffix}",
    ]
    # Total loss has to be checked first: when nothing survives, the missing set
    # is simultaneously a leading and a trailing run, and calling that
    # "discard-old" would be reading a queue policy into a broker that never
    # queued anything.
    if len(window_missing) == len(window):
        return "no-queue / total-loss (nothing published during the outage survived)", detail
    if prefix == len(window_missing):
        return "discard-old (the oldest queued frames are dropped)", detail
    if suffix == len(window_missing):
        return "discard-new (the newest frames are dropped)", detail
    return "mixed (loss is not a clean run at either end)", detail


def verdict_lines(
    profile: str,
    ok_seqs: list[int],
    missing: list[int],
    failed_seqs: list[int],
    duplicates: dict,
    out_of_order: int,
    overflow_verdict: str,
    attempt_errors: int,
    conn_events: collections.Counter,
) -> list[str]:
    lines = []
    if not missing:
        lines.append("NO LOSS: every frame the publisher accepted reached the hub.")
    else:
        pct = 100.0 * len(missing) / len(ok_seqs) if ok_seqs else 0.0
        lines.append(f"LOSS: {len(missing)} of {len(ok_seqs)} accepted frames never reached the hub ({pct:.2f}%).")
    # The distinction that matters for the open question: a connection-level
    # callback firing is not the same as being told a specific frame was lost.
    if failed_seqs:
        lines.append(
            f"PER-FRAME FEEDBACK: the publisher was told about {len(failed_seqs)} frames it could not place, "
            f"across {attempt_errors} failed publish attempts."
        )
    elif attempt_errors:
        lines.append(
            f"PER-FRAME FEEDBACK: {attempt_errors} publish attempts errored, but every frame was eventually placed."
        )
    else:
        lines.append(
            "PER-FRAME FEEDBACK: none. No publish call ever failed, "
            f"even for the {len(missing)} frames that never arrived."
        )
    lines.append(
        f"CONNECTION-LEVEL FEEDBACK: {dict(conn_events) or 'none'} "
        "(says the broker link moved, says nothing about which frames were lost)"
    )
    lines.append("DUPLICATES: none." if not duplicates else f"DUPLICATES: {len(duplicates)} seq arrived more than once.")
    lines.append("ORDERING: preserved end to end." if out_of_order == 0 else f"ORDERING: broken, {out_of_order} arrivals out of sequence.")
    lines.append(f"OVERFLOW: {overflow_verdict}")
    return lines


if __name__ == "__main__":
    main()
