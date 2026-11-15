"""CLI keeps --dump/--json free of any Textual imports."""
import argparse
import json

from .core import DEFAULT_BOARD, DEFAULT_JOBS, Store, decisions_unseen, dump_snapshot, load_board


def main():
    parser = argparse.ArgumentParser(description="aw · Claude / GPT / Gemini 终端看板")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--dump", action="store_true", help="打印纯文本快照")
    output.add_argument("--json", action="store_true", help="打印 JSON 快照")
    output.add_argument("--decisions-unseen", action="store_true", help="只输出 Claude 未读拍板 JSON")
    parser.add_argument("--no-anim", action="store_true", help="关闭栏头宠物动画")
    parser.add_argument("--jobs", default=DEFAULT_JOBS, help="任务目录")
    parser.add_argument("--board", default=DEFAULT_BOARD, help="Claude 看板 JSON")
    parser.add_argument("--no-notify", action="store_true", help="关闭 macOS 通知")
    parser.add_argument("--recent", type=int, help="只读输出最近启动的 N 个任务（用于逐卡核查）")
    args = parser.parse_args()
    if args.recent is not None and (args.recent < 1 or not (args.dump or args.json)):
        parser.error("--recent 需正整数，且与 --dump / --json 一起使用")
    store = Store(args.jobs, args.board)
    if args.decisions_unseen:
        board, error = load_board(args.board)
        if error:
            parser.exit(1, error + "\n")
        print(json.dumps(decisions_unseen(board["decisions"]), ensure_ascii=False, indent=2))
    elif args.dump or args.json:
        data = store.snapshot()
        if args.recent:
            data["jobs"] = sorted(data["jobs"], key=lambda j: j["started"], reverse=True)[:args.recent]
        print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else dump_snapshot(data, history=bool(args.recent)))
    else:
        try:
            from .tui import WatchApp
        except ImportError as exc:
            parser.exit(1, f"请先运行 ./install.sh 安装界面依赖：{exc}\n")
        WatchApp(store, args.no_notify, args.no_anim).run(mouse=True)


if __name__ == "__main__":
    main()
