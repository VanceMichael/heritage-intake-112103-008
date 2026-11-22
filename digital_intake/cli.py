"""Command line entry point for the intake and catalogue workbench.

Examples::

    python -m digital_intake ingest fixtures/sources.json
    python -m digital_intake provenance fixtures/sources.json ASSET-3
    python -m digital_intake release fixtures/sources.json ASSET-3 online-exhibition --on 2026-11-22
    python -m digital_intake recheck fixtures/sources.json EX-SYN-9 --on 2026-11-22
    python -m digital_intake completeness fixtures/sources.json

Use ``--store workbench.json`` on any command to keep importing across
sessions: the named file is loaded first, and ``ingest`` appends to it.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .errors import IntakeError
from .store import Workbench


def _prepare(path: str | None, store: str | None) -> Workbench:
    workbench = Workbench.load(store) if store else Workbench()
    if path:
        workbench.ingest_file(path)
    return workbench


def _emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="digital_intake",
        description="数字资料接收与编目工作台",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def with_doc(name: str, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("document", nargs="?", help="来源清单 JSON 文件（可省略，配合 --store）")
        p.add_argument("--store", help="持久化工作台文件，跨会话累积导入")
        return p

    with_doc("ingest", "导入来源清单，报告接收与拒绝的条目")

    p = with_doc("resolve", "按文件名或校验值查找数字记录，不自动合并")
    p.add_argument("--file-name")
    p.add_argument("--sha256")
    p.add_argument("--asset-ref")

    p = with_doc("provenance", "查看一条数字记录的来源链与缺失字段")
    p.add_argument("asset_ref")

    p = with_doc("release", "检查某条数字记录能否用于指定渠道")
    p.add_argument("asset_ref")
    p.add_argument("channel")
    p.add_argument("--on", dest="on_date")

    p = with_doc("rights", "列出某条数字记录在各渠道的许可矩阵")
    p.add_argument("asset_ref")
    p.add_argument("--on", dest="on_date")

    p = with_doc("recheck", "重新检查展览中待发布条目的关联与许可")
    p.add_argument("exhibition_ref")
    p.add_argument("--on", dest="on_date")

    with_doc("completeness", "按每条数字记录列出缺失的来源信息")

    return parser


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        workbench = _prepare(args.document, args.store)
    except (OSError, json.JSONDecodeError, IntakeError) as exc:
        print(f"无法读取来源清单: {exc}", file=sys.stderr)
        return 2

    if not workbench.accepted_source_ids() and args.command != "ingest":
        print("工作台中没有来源记录，请先运行 ingest", file=sys.stderr)
        return 2

    if args.command == "ingest":
        _emit(
            {
                "accepted": workbench.accepted_source_ids(),
                "rejected": workbench.rejected,
            }
        )
        if args.store:
            workbench.save(args.store)
    elif args.command == "resolve":
        try:
            _emit(
                workbench.resolve_asset(
                    file_name=args.file_name,
                    sha256=args.sha256,
                    asset_ref=args.asset_ref,
                )
            )
        except IntakeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    elif args.command == "provenance":
        _emit(workbench.asset_report(args.asset_ref))
    elif args.command == "release":
        _emit(workbench.check_release(args.asset_ref, args.channel, args.on_date))
    elif args.command == "rights":
        _emit(workbench.rights_matrix(args.asset_ref, args.on_date))
    elif args.command == "recheck":
        _emit(workbench.recheck_exhibition(args.exhibition_ref, args.on_date))
    elif args.command == "completeness":
        _emit(workbench.completeness_report())
    return 0


def main() -> None:  # pragma: no cover - thin wrapper
    raise SystemExit(run())


if __name__ == "__main__":  # pragma: no cover
    main()
