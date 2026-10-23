"""Offline catalog-quality CLI. No server discovery, tool execution or host writes."""

import argparse
import json
from pathlib import Path

from .catalog_quality import (
    MAX_BYTES,
    analyze_catalog,
    check_choices,
    compare_catalogs,
    prepare_choices,
    prepare_review,
    score_review,
    snapshot,
)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("quality files must not contain duplicate JSON keys")
        value[key] = item
    return value


def _read(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("quality file exceeds 8 MiB")
    return snapshot(json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "lint",
            "review-request",
            "review-result",
            "choices-request",
            "choices-result",
            "compare",
        ),
    )
    parser.add_argument("catalog", type=Path, help="complete captured tools/list JSON")
    parser.add_argument("--profile", required=True, help="selected capabilities and boundaries")
    parser.add_argument("--evaluator", help="model/reviewer version AND decoding/settings revision")
    parser.add_argument("--response", type=Path)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--before", type=Path)
    args = parser.parse_args(argv)
    try:
        catalog = _read(args.catalog)
        options = {"profile": args.profile, "evaluator": args.evaluator}
        if args.command == "lint":
            result = analyze_catalog(catalog, profile=args.profile)
        elif args.command == "compare":
            if not args.before:
                raise ValueError("compare requires --before")
            result = compare_catalogs(_read(args.before), catalog, profile=args.profile)
        elif args.command.startswith("review"):
            if args.command == "review-request":
                result = prepare_review(catalog, **options)
            else:
                if not args.response:
                    raise ValueError("review-result requires --response")
                result = score_review(catalog, _read(args.response), **options)
        else:
            if not args.cases:
                raise ValueError("choice commands require --cases")
            cases = _read(args.cases)
            if args.command == "choices-request":
                result = prepare_choices(catalog, cases, **options)
            else:
                if not args.response:
                    raise ValueError("choices-result requires --response")
                result = check_choices(catalog, cases, _read(args.response), **options)
    except (ValueError, OSError, RecursionError) as error:
        parser.exit(2, f"catalog quality: {error}\n")
    print(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False))
    return 1 if result.get("ok") is False or result.get("passed") is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
