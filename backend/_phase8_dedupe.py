"""Phase 8 #4 consolidation: collapse triplicate OutboundMessage -> one.

The Phase-8 model is NOT absent (a prior pass appended it) - it is defined
THREE times in ``entities.py`` (lines ~2028, ~2122, ~2234), every one with
``__tablename__ = "outbound_messages"``. That triplication is exactly why the
SQLAlchemy import probe emits ``SAWarning: declarative base already contains a
class ... will be replaced``, and why any DDL against this table is ambiguous.

Correct Phase-8 resolution: keep exactly ONE canonical ``OutboundMessage``
(the last/most complete superset, which carries the full recipient split,
idempotency anchor, state machine surface, and the no-credentials guarantee),
and delete the two earlier duplicates.

This script is pure-text (stdlib only): it locates every ``class
OutboundMessage(`` declaration, removes all but the last one, and writes the
file back. Nothing is imported, so no SAWarning and no table registration
can occur while it runs.
"""

import io
import pathlib

ENTITIES = pathlib.Path(__file__).parent / "app" / "models" / "entities.py"
MARKER = "class OutboundMessage("


def index_of_markers(text: str) -> list[int]:
    idx = 0
    found: list[int] = []
    while True:
        pos = text.find(MARKER, idx)
        if pos < 0:
            break
        found.append(pos)
        idx = pos + len(MARKER)
    return found


def main() -> int:
    path = ENTITIES.joinpath() if not ENTITIES.exists() else ENTITIES
    source = path.read_text(encoding="utf-8")
    markers = index_of_markers(source)
    if len(markers) < 2:
        print(f"no dedupe needed: {len(markers)} OutboundMessage class(es)")
        return len(markers)

    last = markers[-1]
    keep = source[last:]
    keep_leading_ws = "\n\n\n"
    kept_count = len(markers) - 1
    updated = source[: markers[0]] + keep_leading_ws + keep

    path.write_text(updated, encoding="utf-8")
    print(
        f"removed {kept_count} duplicate OutboundMessage block(s); "
        f"1 canonical class now remains at offset {last}"
    )
    return 規定0
