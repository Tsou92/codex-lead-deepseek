"""Small CLI; stdout is always a compact, machine-readable receipt."""

import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import sys

from . import __version__
from .runner import ROOT, STATE, run_task, run_path, doctor, revise_task
from .workspace import apply_changes
from .journal import record, list_recent


def main(argv=None):
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "activate":
        from .activation import main as activate_main

        return activate_main(raw[1:])
    if raw and raw[0] == "monitor":
        from .monitor_cli import main as monitor_main

        return monitor_main(raw[1:])
    parser = argparse.ArgumentParser(prog="leadseek", description="Codex 统筹，DeepSeek 默认执行；限定任务、精简回传、自动验收返工。")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("activate", help="启动并打开认证监控，关联当前 Codex 任务")
    commands.add_parser("doctor", help="检查本机接入状态，不调用模型")
    run = commands.add_parser("run", help="执行一个 JSON 任务，完整日志留在本地")
    run.add_argument("--task", required=True, help="任务 JSON 文件；- 表示从标准输入读取")
    revise = commands.add_parser("revise", help="按 Codex 验收意见返工，沿用上一轮暂存成果")
    revise.add_argument("run_id")
    revise.add_argument("--instruction", required=True, help="Codex 给出的具体问题、方向和验收要求")
    for name in ("result", "status", "apply"):
        sub = commands.add_parser(name, help={"result": "查看精简结果", "status": "查看任务状态", "apply": "审查后将改动应用到原项目"}[name])
        sub.add_argument("run_id")
        if name == "apply":
            sub.add_argument("--note", required=True, help="Codex 的验收依据，例如差异检查与测试结果")
    route = commands.add_parser("route", help="记录分工或 Codex 接手的具体原因")
    route.add_argument("--workspace", required=True)
    route.add_argument("--owner", choices=("codex", "deepseek"), required=True)
    route.add_argument("--action", required=True)
    route.add_argument("--reason", required=True)
    route.add_argument("--run-id")
    audit = commands.add_parser("audit", help="查看实际委派、返工和验收记录")
    audit.add_argument("--workspace")
    audit.add_argument("--limit", type=int, default=20)
    commands.add_parser("list", help="列出最近十个任务")
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            result = doctor()
        elif args.command == "run":
            raw = sys.stdin.read() if args.task == "-" else Path(args.task).read_text(encoding="utf-8")
            result = run_task(json.loads(raw))
        elif args.command == "revise":
            result = revise_task(args.run_id, args.instruction)
        elif args.command == "route":
            result = record(ROOT, args.workspace, args.owner, args.action, args.reason, args.run_id)
        elif args.command == "audit":
            result = list_recent(ROOT, args.workspace, args.limit)
        elif args.command in ("status", "result"):
            directory = run_path(args.run_id)
            filename = "result.json" if args.command == "result" else "status.json"
            path = directory / filename
            result = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"run_id": args.run_id, "status": "running", "artifacts": str(directory)}
        elif args.command == "apply":
            if not args.note.strip() or len(args.note) > 2000:
                raise ValueError("验收说明必须非空且不超过 2000 字符")
            directory = run_path(args.run_id)
            task = json.loads((directory / "task.json").read_text(encoding="utf-8"))
            key = hashlib.sha256(task["workspace"].encode()).hexdigest()
            locks = STATE / "locks"
            locks.mkdir(parents=True, exist_ok=True)
            with (locks / ("apply-" + key + ".lock")).open("a+") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = apply_changes(directory)
                record(ROOT, task["workspace"], "codex", "accepted_and_applied", args.note, args.run_id)
        else:
            paths = sorted((STATE / "runs").glob("*/status.json"), reverse=True)[:10]
            result = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if isinstance(result, dict) and result.get("status") in {"failed", "timed_out", "cancelled", "scope_violation", "needs_attention", "log_limit_exceeded"}:
            return 1
        return 0
    except (ValueError, OSError) as error:
        print(json.dumps({"status": "error", "message": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
