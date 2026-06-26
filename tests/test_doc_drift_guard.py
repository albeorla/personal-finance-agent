"""Guard against stale "future slice" doc drift regressing into the source.

Background: ``apply_charge_onboarding_candidate`` shipped and is now central, so
docstrings/comments that called the apply step "out of scope for this slice" (or
that listed ``accept`` as unsupported "in this slice") became factually wrong.
After scrubbing them, this test fails if those exact stale phrases reappear in
the modules where they were stale.

Scope notes (intentionally narrow so this stays maintainable):
- We ban only exact stale phrases, NOT the bare word "slice". Accurate uses such
  as ``DEFERRED_TO_LATER_SLICE`` / "later slice" / "separate guarded slice" in
  onboarding.py describe genuinely-unimplemented restructuring (edit/merge/split)
  and must stay green.
- The ban is checked against the whole package source so a phrase cannot sneak
  back into a different module either.
"""

from __future__ import annotations

from pathlib import Path

import financial_agent

# Exact phrases that were stale once apply_charge_onboarding_candidate shipped.
# Keep this list tight: each entry must be wrong NOW, not merely slice-flavored.
BANNED_PHRASES: tuple[str, ...] = (
    "out of scope for this slice",
    "in this slice",
    "this slice",  # superset guard: any "<x> this slice" wording is stale now
)


def _package_source_files() -> list[Path]:
    pkg_dir = Path(financial_agent.__file__).resolve().parent
    return sorted(pkg_dir.glob("*.py"))


def test_no_stale_slice_phrases_in_source() -> None:
    offenders: list[str] = []
    for path in _package_source_files():
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        for phrase in BANNED_PHRASES:
            if phrase in lowered:
                offenders.append(f"{path.name}: {phrase!r}")
    assert not offenders, (
        "Stale doc-drift phrase(s) reappeared in package source: "
        + ", ".join(offenders)
    )
