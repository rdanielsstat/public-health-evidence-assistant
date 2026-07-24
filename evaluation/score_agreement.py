# evaluation/score_agreement.py
"""Compare manual relevance grades against the automated grader.

Reads manual grades from a plain text file (one integer per line, or the
completed agreement_sample.txt with GRADE: lines filled in) and scores them
against agreement_key.json.

    uv run python -m evaluation.score_agreement evaluation/my_grades.txt
    uv run python -m evaluation.score_agreement evaluation/agreement_sample.txt
"""

import argparse
import collections
import json
import re
import sys
from pathlib import Path

KEY = Path("evaluation/agreement_key.json")


def read_grades(path: Path) -> list[int]:
    """Accept either a bare list of integers or a filled-in sample file."""
    text = path.read_text()

    filled = re.findall(r"GRADE:\s*([012])\b", text)
    if filled:
        return [int(g) for g in filled]

    return [
        int(line.strip())
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("grades_file", type=Path)
    args = parser.parse_args()

    key = json.loads(KEY.read_text())
    mine = read_grades(args.grades_file)

    if len(mine) != len(key):
        sys.exit(
            f"got {len(mine)} manual grades but the key has {len(key)} items"
        )

    model = [k["model_grade"] for k in key]
    n = len(key)

    exact = sum(1 for m, g in zip(mine, model) if m == g)
    within1 = sum(1 for m, g in zip(mine, model) if abs(m - g) <= 1)
    stricter = sum(1 for m, g in zip(mine, model) if m < g)
    looser = sum(1 for m, g in zip(mine, model) if m > g)

    print(f"exact agreement:  {exact}/{n} = {exact / n:.0%}")
    print(f"within one grade: {within1}/{n} = {within1 / n:.0%}")
    print(f"manual stricter:  {stricter}")
    print(f"manual looser:    {looser}")
    print()

    print("per model grade")
    for g in (0, 1, 2):
        idx = [i for i, mg in enumerate(model) if mg == g]
        if not idx:
            continue
        agree = sum(1 for i in idx if mine[i] == g)
        print(f"  model={g}: {agree}/{len(idx)} ({agree / len(idx):.0%})")
    print()

    print("confusion (model -> manual)")
    conf = collections.Counter(zip(model, mine))
    print("        manual=0  manual=1  manual=2")
    for g in (0, 1, 2):
        cells = "".join(f"{conf.get((g, m), 0):10d}" for m in (0, 1, 2))
        print(f"model={g}{cells}")
    print()

    disagreements = [
        (i + 1, mine[i], model[i], key[i]["question_id"])
        for i in range(n)
        if mine[i] != model[i]
    ]
    if disagreements:
        print("disagreements")
        for pos, m, g, qid in disagreements:
            print(f"  item {pos:3d}  [{qid}]  manual={m}  model={g}")


if __name__ == "__main__":
    main()
