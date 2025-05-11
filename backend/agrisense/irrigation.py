"""Transparent irrigation assessment rules.

Decision rule (FAO-56 management-allowed-depletion principle):

    Dr >= RAW             -> IRRIGATE: depletion has reached the stress threshold.
                             Net depth to refill the root zone to field
                             capacity = Dr (mm).
    Dr + ETc >= RAW       -> MONITOR: RAW is expected to be reached within about
                             one day at the current ETc (no rain assumed).
    otherwise             -> NO_IRRIGATION.
    cannot be determined  -> INSUFFICIENT_DATA (no recommendation is forced).

Decision SUPPORT only; never drives a pump. Depth is NET: excludes application
efficiency, leaching requirement and system losses.
"""

from dataclasses import dataclass

IRRIGATE = "IRRIGATE"
MONITOR = "MONITOR"
NO_IRRIGATION = "NO_IRRIGATION"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass(frozen=True)
class Decision:
    recommendation: str
    net_depth_mm: float   # None unless recommendation == IRRIGATE
    reasoning: str


def decide(depletion_mm, raw_mm, taw_mm, etc_mm):
    summary = f"Dr = {depletion_mm:.1f} mm, RAW = {raw_mm:.1f} mm, TAW = {taw_mm:.1f} mm"
    if depletion_mm >= raw_mm:
        depth = round(depletion_mm, 1)
        return Decision(
            IRRIGATE, depth,
            f"{summary}. Depletion has reached or passed RAW (the allowed depletion before "
            f"water stress). Applying a net depth of {depth:.1f} mm would refill the root zone "
            f"to field capacity.",
        )
    if depletion_mm + etc_mm >= raw_mm:
        return Decision(
            MONITOR, None,
            f"{summary}. At the current ETc of {etc_mm:.1f} mm/day (no rain assumed) depletion "
            f"would reach RAW within about one day. Plan irrigation; none is needed yet.",
        )
    if etc_mm > 0:
        days = (raw_mm - depletion_mm) / etc_mm
        timing = f" At ETc = {etc_mm:.1f} mm/day, RAW would be reached in roughly {days:.1f} days " \
                 f"if no rain or irrigation occurs."
    else:
        timing = ""
    return Decision(
        NO_IRRIGATION, None,
        f"{summary}. Depletion is below RAW, so no water stress is expected.{timing}",
    )


def insufficient_data(reason):
    return Decision(
        INSUFFICIENT_DATA, None,
        f"No defensible recommendation: {reason}",
    )
