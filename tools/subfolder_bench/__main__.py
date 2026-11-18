"""Print the score of the rules on both sets of cases (``python -m tools.subfolder_bench``).

Replaces the former ``treeaudit subfolders`` command (D-066). Every name
printed comes from the made-up cases: no real file is read.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Sequence

from tools.subfolder_bench.cases import HELDOUT_CASES
from tools.subfolder_bench.cases_earlier import EARLIER_CASES
from tools.subfolder_bench.scoring import BenchResult, case_moves, run_rules_bench


def main() -> int:
    """Score the proposed sub-folders on the two sets of made-up cases."""
    for label, cases in (
        ("règles, cas mis de côté", HELDOUT_CASES),
        ("règles, cas plus anciens", EARLIER_CASES),
    ):
        started = time.monotonic()
        results = run_rules_bench(cases)
        print_results(results, label, time.monotonic() - started)
    return 0


def print_results(results: Sequence[BenchResult], label: str, seconds: float) -> None:
    """One line per case, its sub-folders with their members, then the totals."""
    for result in results:
        if result.suggestions is None:
            print(f"✘ {result.case.name} : erreur : {result.error}")
            continue
        found = result.suggestions
        print(
            f"{'✔' if result.names_valid else '✘ NOMS INVALIDES'} {result.case.name} : "
            f"ensemble {result.together[0]}/{result.together[1]}, "
            f"séparés {result.apart[0]}/{result.apart[1]}, score {result.score:.0%}, "
            f"classés {result.covered[0]}/{result.covered[1]} ({result.coverage:.0%})"
            + (f", noms génériques : {', '.join(result.generic)}" if result.generic else "")
        )
        names = {m.entry_id: m.source.rsplit("/", 1)[-1] for m in case_moves(result.case)[0]}
        for sub in found.subfolders:
            members = [names[e] for e, chosen in sorted(found.by_entry.items()) if chosen == sub]
            print(f"    {sub} : {' | '.join(members)}")
        left = [name for e, name in names.items() if e not in found.by_entry]
        print(f"    (sans sous-dossier) : {' | '.join(left) or '-'}")
    scores = [r.score for r in results]
    coverages = [r.coverage for r in results if r.covered[1]]
    print(
        f"BILAN rangement {label} : score moyen {sum(scores) / len(scores):.0%}, "
        f"classement moyen {sum(coverages) / max(len(coverages), 1):.0%}, "
        f"noms valides {sum(r.names_valid for r in results)}/{len(results)}, "
        f"{seconds:.3f} s\n"
    )


if __name__ == "__main__":
    sys.exit(main())
