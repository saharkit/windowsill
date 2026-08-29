"""Coverage badge colour projection — see fix(#3697).

Stdlib only. Exposes ``color_for(value, floor)`` so a CI step can pin the
boundaries the badge lives by, plus a thin CLI for the publish step that
writes the badge JSON. Two callers — the publish step and the boundary
check — share one implementation so a future floor change cannot leave
two colour rules disagreeing.

Bands, low to high:
  - ``red``         — value < floor
  - ``yellow``      — floor <= value < ``min(floor + 5, 100)``
  - ``brightgreen`` — value >= ``min(floor + 5, 100)``

The green threshold is capped at 100 because coverage is a percentage and
stops there while ``floor + 5`` does not. At a floor of exactly 100 the
cap takes precedence over the equal-to-the-floor yellow band, so a
measured 100 renders green and the yellow band closes rather than
swallowing the top of the scale.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ONE implementation, used by the publish step and the boundary check. Two
# copies of a rule are two rules, and the check would stop meaning anything.
def color_for(value: int, floor: int) -> str:
    """Project a measured coverage value against a floor to a shields.io colour."""
    green_threshold = min(floor + 5, 100)
    if value >= green_threshold:
        return "brightgreen"
    if value >= floor:
        return "yellow"
    return "red"


def write_badge(path: str, value: int, floor: int) -> str:
    """Compute the colour via :func:`color_for` and write the badge JSON to ``path``.

    Returns the colour written. The colour is computed by the same function
    the boundary check exercises, so the file written and the check never
    disagree.
    """
    color = color_for(value, floor)
    payload = {
        "schemaVersion": 1,
        "label": "coverage",
        "message": f"{value}%",
        "color": color,
    }
    Path(path).write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return color


# The four boundaries pinned by the shellcheck step. Adding a case is a
# deliberate decision — each row is one place the rule could break, and the
# shellcheck step fails if any of them moves. Do not parametrise away the
# distinction: a "red below the floor" case is a different decision from
# "yellow at the floor" only if the rule can return both — and that is the
# thing this list exists to prove.
_BOUNDARY_CASES: tuple[tuple[int, int, str], ...] = (
    # floor 99 — yellow band still open, the cap kicks in at the perfect score
    (98, 99, "red"),          # below the floor
    (99, 99, "yellow"),       # at the floor
    (100, 99, "brightgreen"),  # a perfect score is green (cap takes over)
    # floor 100 — the cap and the equal-to-the-floor yellow band collide; cap wins
    (100, 100, "brightgreen"),  # a perfect score at the cap stays green
)


def check_boundaries() -> list[str]:
    """Run every boundary assertion. Returns a list of failure descriptions (empty if all pass)."""
    failures: list[str] = []
    for value, floor, expected in _BOUNDARY_CASES:
        actual = color_for(value, floor)
        if actual != expected:
            failures.append(
                f"color_for({value}, {floor}) returned {actual!r}, expected {expected!r}"
            )
    return failures


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else "Coverage badge projection",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    write_parser = subparsers.add_parser(
        "write",
        help="Compute the colour and write the badge JSON to a file.",
    )
    write_parser.add_argument("--output", required=True, help="Path to write the JSON to")
    write_parser.add_argument("--value", required=True, type=int, help="Measured coverage percentage")
    write_parser.add_argument("--floor", required=True, type=int, help="Coverage floor")

    subparsers.add_parser(
        "check",
        help="Run the boundary assertions and exit 1 on any failure.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "write":
        color = write_badge(args.output, args.value, args.floor)
        print(color)
        return 0
    if args.command == "check":
        failures = check_boundaries()
        if failures:
            for failure in failures:
                print(f"FAIL: {failure}", file=sys.stderr)
            return 1
        print("ok")
        return 0
    return 2  # unreachable — argparse `required=True` rejects this


if __name__ == "__main__":
    sys.exit(main())
