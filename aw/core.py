"""Data collection and atomic, targeted user operations for the aw board."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import stat
import tempfile
import time

DECK_HOME = Path(os.environ.get("AGENT_DECK_HOME") or (Path.home() / ".local" / "share" / "agent-deck")).expanduser()
DEFAULT_JOBS = Path(os.environ.get("AW_JOBS_DIR") or os.environ.get("AW_JOBS") or (DECK_HOME / "jobs")).expanduser()
DEFAULT_BOARD = Path(os.environ.get("AW_BOARD_FILE") or os.environ.get("AW_BOARD") or (DECK_HOME / "board.json")).expanduser()
DEFAULT_STATE = DECK_HOME / "agent_watch_state.json"
TAIL_BYTES = 128 * 1024
ACTIVE = {"running", "stalled", "died"}
NEW_MINUTES = 30
DIED_HISTORY_SECONDS = 6 * 60 * 60
STATE_LABELS = {"running": "运行中", "stalled": "卡住", "died": "失联", "ok": "成功", "fail": "失败"}
PROGRESS_STATUSES = {"running": "进行中", "waiting_user": "等你", "blocked": "受阻", "done": "完成"}
TERMINAL = {"ok", "fail", "died"}


def read_text(path, default="", limit=32768):
    try:
        with Path(path).open("rb") as stream:
            return stream.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return default


def read_tail(path):
    """At most 128 KiB, discarding an incomplete leading JSON line."""
    try:
        with Path(path).open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            offset = max(0, size - TAIL_BYTES)
            stream.seek(offset)
            data = stream.read(TAIL_BYTES)
        if offset:
            data = data.partition(b"\n")[2]
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def signature(path):
    try:
        s = Path(path).stat()
        return s.st_ino, s.st_size, s.st_mtime_ns
    except OSError:
        return None


def number(value, default=0):
    try:
        n = float(value)
        return n if n == n and abs(n) != float("inf") else default
    except (TypeError, ValueError):
        return default


def pid_alive(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False


def title_from_prompt(prompt):
    first = next((line.strip() for line in prompt.splitlines() if line.strip()), "")
    match = re.match(r"(?:【标题】\s*|#\s+)(.+)", first)
    if match:
        return match.group(1).strip()
    match = re.search(r"任务卡_[^\s/`\"'（）()]+?\.md", prompt)
    return match.group(0) if match else (first[:30] or "未命名任务")


def one_line(value, limit=180):
    value = " ".join(str(value or "").split())
    return value if len(value) <= limit else value[:limit-1] + "…"


def last_paragraph(value):
    # Adjacent Markdown list items are separate logical paragraphs too.
    paragraphs = re.split(r"\n\s*\n|\n(?=\s*(?:\d+[.)、]|[-*+])\s+)", str(value or "").strip())
    return paragraphs[-1].strip()


def command_preview(command):
    """Unwrap shell launchers without executing or rewriting the stored command."""
    command = command.strip()
    for _ in range(4):
        try:
            parts = shlex.split(command)
        except ValueError:
            break
        if (len(parts) >= 3 and Path(parts[0]).name in {"sh", "bash", "zsh", "dash", "ksh", "fish"}
                and parts[1].startswith("-") and "c" in parts[1]):
            command = parts[2].strip()
        else:
            break
    return command.splitlines()[0] if command else ""


def plain_text(value):
    """Remove Markdown decoration, keeping link labels and sentence punctuation."""
    value = re.sub(r"!?\[([^\]]+)\]\([^)]+\)", r"\1", str(value or ""))
    value = re.sub(r"(?m)^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)、]\s+)", "", value)
    value = value.replace("**", "").replace("__", "")
    value = re.sub(r"(?<!\w)[*_]([^\n*_]+)[*_](?!\w)", r"\1", value)
    return value.replace("**", "").replace("__", "").replace("`", "").replace("~~", "").strip()


def first_sentence(value):
    value = " ".join(plain_text(value).split())
    sentences = re.split(r"(?<=[。！？!?])\s*|(?<=\.)\s+", value)
    return one_line(next((s.strip() for s in sentences if s.strip()), ""))


def task_cards(prompt, cwd):
    """Resolve only referenced cards, relative to job cwd or directories in prompt."""
    base = Path.cwd()
    cwd = Path(cwd or base).expanduser()
    if not cwd.is_absolute():
        cwd = base / cwd
    token = r"[\w./~\-]+"
    refs = re.findall(token + r"任务卡_[^\s/`\"'（）()<>]+?\.md|任务卡_[^\s/`\"'（）()<>]+?\.md", prompt)
    dirs = [cwd, base]
    for directory in re.findall(token + r"/", prompt):
        path = Path(directory).expanduser()
        dirs.append(path if path.is_absolute() else cwd / path)
    found = []
    for ref in dict.fromkeys(refs):
        path = Path(ref).expanduser()
        candidates = [path] if path.is_absolute() else [d / path for d in dict.fromkeys(dirs)]
        for candidate in candidates:
            if candidate.is_file():
                resolved = candidate.resolve()
                if resolved not in found:
                    found.append(resolved)
                break
    return found


def purpose_from_prompt(prompt, cards=()):
    match = re.search(r"(?m)^\s*【一句话】\s*(.+)$", prompt)
    if match:
        return one_line(plain_text(match.group(1)))
    for card in cards:
        match = re.search(r"(?m)^##\s+目标\s*\n([\s\S]*?)(?=^#{1,6}\s|\Z)", read_text(card))
        if match:
            line = next((line for line in match.group(1).splitlines() if line.strip()), "")
            if line:
                return one_line(plain_text(line))
    return ""


def progress_sentence(cards, started):
    candidates = {}
    for card in cards:
        for path in card.parent.glob("进度_*.md"):
            try:
                mtime = path.stat().st_mtime
                if mtime > started:
                    candidates[path] = mtime
            except OSError:
                pass
    for path in sorted(candidates, key=candidates.get, reverse=True):
        # Tail-read Markdown without dropping its first complete record.
        try:
            with path.open("rb") as stream:
                size = stream.seek(0, os.SEEK_END)
                stream.seek(max(0, size - 32768))
                text = stream.read(32768).decode("utf-8", errors="replace")
            lines = [line.strip() for line in text.splitlines()
                     if line.strip() and not line.lstrip().startswith(("#", "```"))]
            if lines:
                line = plain_text(lines[-1])
                line = re.sub(r"^(?:\[[^\]]+\]|\d{4}-\d{2}-\d{2}[ T][\d:]+)\s*[：:·-]?\s*", "", line)
                if line:
                    return first_sentence(line)
        except OSError:
            pass
    return ""


def human_command(command):
    command = command_preview(command)
    if re.search(r"\b(?:unittest|pytest)\b", command):
        return "跑测试"
    if re.search(r"(?:^|[\s/;|])git(?:\s|$)", command):
        return "git 操作"
    if re.search(r"\b(?:ssh|scp)\b", command):
        return "连虚拟机"
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    files = [Path(x.rstrip(";)")).name for x in parts
             if not x.startswith("-") and re.search(r"[\w-]+\.[\w]+$", x)]
    patch_path = re.search(r"(?:Update|Add|Delete) File:\s*(\S+)", command)
    filename = " " + Path(patch_path.group(1)).name if patch_path else (" " + files[0] if files else "")
    if "apply_patch" in command or re.search(r"(?:write_to_file|write_text|write_bytes|\btee\b|>{1,2}\s*[^&])", command):
        return "改文件" + filename
    if re.search(r"(?:^|[\s/;|])(?:rg|grep|sed|cat|head)(?:\s|$)", command):
        return "读代码" + filename
    return "运行命令"


def latest_step(steps):
    for step in reversed(steps[-3:]):
        if step.startswith("回复："):
            return first_sentence(step[3:])
    if not steps:
        return "等待任务输出"
    step = steps[-1]
    if step.startswith("命令："):
        return human_command(step[3:])
    if step.startswith(("文件：", "读取：")):
        return ("改文件 " if step.startswith("文件：") else "读代码 ") + Path(step[3:]).name
    return "运行命令"


def status_reason(job):
    if job["state"] == "stalled":
        return "超过 10 分钟没有新输出，请检查任务是否卡住"
    if job["state"] == "died":
        return "进程已退出且心跳失联，请检查任务是否中断"
    return job.get("current", job.get("latest", "等待任务输出"))


def ending_age(job, now):
    return f"{max(0, int((now - (job.get('ended') or job['started'])) / 60))} 分钟前结束"


def is_new(job, now=None):
    """Unread terminal evidence, with the original 30-minute fallback."""
    if job.get("read") or job.get("hidden"):
        return False
    if job["state"] not in ("ok", "fail", "died"):
        return False
    ended = job.get("ended")
    if ended is None and job["state"] == "died":
        ended = job.get("last_activity")
    now = time.time() if now is None else now
    return ended is not None and 0 <= now - ended < NEW_MINUTES * 60


def is_active(job):
    return job["state"] in ACTIVE and not job.get("archived", False)


def parse_output(text, agent):
    """Both JSONL dialects, plus older agy single-result JSON. No tool output dump."""
    steps = OrderedDict()
    result = ""
    tokens = None
    duration = None
    failure_reason = ""
    codex_tokens = 0

    def put(key, value):
        if value:
            steps[key] = value
            steps.move_to_end(key)

    for index, line in enumerate(text.splitlines()):
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") in ("error", "turn.failed") or event.get("event") == "error":
            error = event.get("error") or event.get("message") or ""
            failure_reason = str(error.get("message") or error) if isinstance(error, dict) else str(error)
        if agent == "codex":
            item = event.get("item") or {}
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            key = item.get("id", index)
            if kind == "command_execution":
                put(key, "命令：" + str(item.get("command") or ""))
            elif kind == "file_change":
                changes = item.get("changes") or []
                put(key, "文件：" + "、".join(str(c.get("path", "")) for c in changes if isinstance(c, dict)))
            elif kind == "agent_message":
                message = str(item.get("text") or "")
                put(key, "回复：" + message)
                result = message
            elif kind in ("mcp_tool_call", "web_search"):
                put(key, "工具：" + one_line(item.get("tool") or item.get("query") or kind))
            if event.get("type") == "turn.completed":
                usage = event.get("usage") or {}
                codex_tokens += int(number(usage.get("input_tokens"))) + int(number(usage.get("output_tokens")))
                tokens = codex_tokens
        else:
            step = event.get("step_update") or {}
            if not isinstance(step, dict):
                continue
            key = step.get("step_index", index)
            info = step.get("tool_info") or {}
            if isinstance(info, dict):
                params = info.get("parameters") or {}
                if not isinstance(params, dict):
                    params = {}
                name = str(step.get("tool_name") or info.get("name") or "")
                command = params.get("CommandLine") or params.get("command")
                path = params.get("TargetFile") or params.get("AbsolutePath") or params.get("FilePath") or params.get("Path") or params.get("path")
                if command:
                    put(key, "命令：" + str(command))
                elif path:
                    put(key, ("文件：" if any(x in name for x in ("write", "replace", "edit")) else "读取：") + one_line(path))
                elif name:
                    put(key, "工具：" + name)
            if step.get("text_delta"):
                previous = steps.get(key, "回复：")
                put(key, previous + str(step["text_delta"]))
            elif step.get("text"):
                put(key, "回复：" + str(step["text"]))
            data = event.get("result") if isinstance(event.get("result"), dict) else event
            if data.get("error"):
                error = data["error"]
                failure_reason = str(error.get("message") or error) if isinstance(error, dict) else str(error)
            if "response" in data or event.get("event") == "result":
                result = str(data.get("response") or "")
                if result:
                    put("result", "回复：" + last_paragraph(result))
                usage = data.get("usage") or {}
                tokens = int(number(usage.get("total_tokens"))) if "total_tokens" in usage else None
                duration = number(data.get("duration_seconds"), None)
        # Bound accumulation even for unusually many tiny events.
        while len(steps) > 30:
            steps.popitem(last=False)
    return {"steps": list(steps.values())[-15:],
            "result": result, "tokens": tokens, "duration_seconds": duration, "failure_reason": failure_reason}


def load_board(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != 1:
            raise ValueError("需 schema=1 的对象")
        for key in ("decisions", "progress", "archived"):
            if key == "archived" and key not in data:
                continue
            if not isinstance(data.get(key), list) or any(not isinstance(x, dict) for x in data[key]):
                raise ValueError(key + " 应为对象列表")
        return data, None
    except (OSError, ValueError) as exc:
        return {"schema": 1, "decisions": [], "progress": []}, "Claude 看板读取失败：" + str(exc)


@contextmanager
def directory_lock(path):
    """No lock sidecar: serialize aw writers using the containing directory inode."""
    fd = os.open(str(Path(path).parent), os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def atomic_update(path, mutate, default=None):
    """Re-read on conflict; preserve unrelated fields and original file permissions.

    Atomic rename prevents torn JSON. A writer ignoring our lock can still race
    between the last signature check and rename (no filesystem compare-and-swap).
    """
    path = Path(path)
    if default is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    with directory_lock(path):
        for _ in range(8):
            before = signature(path)
            if before is None and default is not None:
                data = json.loads(json.dumps(default))
            else:
                data = json.loads(path.read_text(encoding="utf-8"))
            if signature(path) != before:
                continue
            mutate(data)
            fd, name = tempfile.mkstemp(prefix=".aw-", suffix=".tmp", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(data, stream, ensure_ascii=False, indent=1)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                if before is not None:
                    os.chmod(name, stat.S_IMODE(path.stat().st_mode))
                if signature(path) != before:
                    continue
                os.replace(name, path)
                return
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        raise RuntimeError("看板正在被其他程序连续更新，请稍后重试")


def user_stamp(entry):
    entry.update(updated=datetime.now().strftime("%Y-%m-%dT%H:%M"), updated_by="user")


def board_entries(data, section):
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise ValueError("看板格式错误")
    entries = data.get(section, [])
    if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
        raise ValueError(section + " 应为对象列表")
    return entries


def progress_entry(data, section, project):
    # Existing progress entries have no id; project must uniquely identify them.
    entries = board_entries(data, section)
    matches = [e for e in entries if e.get("project") == project]
    if len(matches) != 1:
        raise ValueError("项目已不存在或名称重复，请刷新后检查")
    return matches[0]


def update_progress(path, project, fields):
    if not fields or set(fields) - {"status", "stage", "next"}:
        raise ValueError("只能修改状态、阶段和下一步")
    if any(not isinstance(v, str) for v in fields.values()):
        raise ValueError("进度内容应为文字")
    def change(data):
        entry = progress_entry(data, "progress", project)
        statuses = set(PROGRESS_STATUSES) | {e.get("status") for e in board_entries(data, "progress")}
        if "status" in fields and fields["status"] not in statuses:
            raise ValueError("请选择已有状态")
        entry.update(fields)
        user_stamp(entry)
    atomic_update(path, change)


def archive_progress(path, project, restore=False):
    source, target = ("archived", "progress") if restore else ("progress", "archived")
    def change(data):
        entry = progress_entry(data, source, project)
        destination = board_entries(data, target)
        if any(e.get("project") == project for e in destination):
            raise ValueError("目标区已有同名项目，请先检查")
        data[source].remove(entry)
        data.setdefault(target, []).append(entry)
        user_stamp(entry)
    atomic_update(path, change)


def decide(path, decision_id, choice, note="", option_index=None):
    if not isinstance(choice, str) or not choice.strip():
        raise ValueError("请先选择选项或填写答案")
    def change(data):
        for entry in board_entries(data, "decisions"):
            if entry.get("id") == decision_id:
                if entry.get("status") != "open":
                    raise ValueError("这条决策已拍板，请刷新")
                if option_index is not None:
                    options = entry.get("options", [])
                    if option_index < 0 or option_index >= len(options) or options[option_index] != choice:
                        raise ValueError("选项已变化，请刷新后重新拍板")
                entry.update(status="decided", choice=choice, note=note.strip(),
                             decided_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                             decided_by="user@aw", seen_by_claude=False)
                user_stamp(entry)
                return
        raise ValueError("这条决策已不存在，请刷新")
    atomic_update(path, change)


def undo_decision(path, decision_id):
    def change(data):
        for entry in board_entries(data, "decisions"):
            if entry.get("id") == decision_id:
                if entry.get("status") != "decided":
                    raise ValueError("这条决策尚未拍板，请刷新")
                entry["status"] = "open"
                for field in ("choice", "note", "decided_at", "decided_by", "seen_by_claude"):
                    entry.pop(field, None)
                user_stamp(entry)
                return
        raise ValueError("这条决策已不存在，请刷新")
    atomic_update(path, change)


def decisions_unseen(decisions):
    """Explicit false only: old records without the handshake flag stay historical."""
    return sorted((d for d in decisions if d.get("status") == "decided" and d.get("seen_by_claude") is False),
                  key=lambda d: str(d.get("decided_at", "")), reverse=True)


def project_decisions(project, decisions):
    """Only actionable decisions belonging to this named project."""
    return [d for d in decisions if project and d.get("project") == project and d.get("status") == "open"]


def progress_label(progress, decisions=()):
    count = len(project_decisions(progress.get("project"), decisions))
    if count:
        return f"等你拍板 ×{count}"
    status = progress.get("status", "")
    return PROGRESS_STATUSES.get(status, str(status))


def board_age(value, now=None):
    """Return (elapsed seconds, date-only precision), or None for invalid input.

    Naive ISO datetimes and date-only midnight use the local timezone; aware
    datetimes retain their offset. Future times are clamped to zero.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))
    if not date_only and not re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", value):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        return max(0, (time.time() if now is None else now) - stamp), date_only
    except (ValueError, OverflowError, OSError):
        return None


def board_age_text(age):
    """Use only the precision actually present in the board field."""
    seconds, date_only = age
    if date_only:
        return f"{int(seconds // 86400)} 天" if seconds >= 86400 else "今天"
    if seconds >= 86400:
        return f"{int(seconds // 86400)} 天"
    if seconds >= 3600:
        return f"{int(seconds // 3600)} 小时"
    return f"{int(seconds // 60)} 分钟"


def pet_status(snapshot, agent):
    """Per-column state and short summary, independent of animation and UI."""
    if agent == "claude":
        opened = sum(d.get("status") == "open" for d in snapshot["decisions"])
        unseen = len(decisions_unseen(snapshot["decisions"]))
        progress = snapshot["progress"]
        state = ("error" if any(p.get("status") == "blocked" for p in progress) else
                 "busy" if opened or any(p.get("status") == "running" for p in progress) else
                 "happy" if unseen else "idle")
        label = "等你" if state == "busy" and opened else PET_LABELS[state]
        return state, label, f"待拍板 {opened} · 新拍板 {unseen}"
    jobs = visible_jobs(snapshot, agent)
    active = [j for j in jobs if is_active(j)]
    recent = [j for j in snapshot["jobs"] if j["agent"] == agent and is_new(j, snapshot["now"])]
    state = ("error" if any(j["state"] in ("stalled", "died") for j in active) or
             any(j["state"] in ("fail", "died") for j in recent) else
             "busy" if any(j["state"] == "running" for j in active) else
             "happy" if any(j["state"] == "ok" for j in recent) else "idle")
    running = sum(j["state"] in ("running", "stalled") for j in active)
    summary = f"运行 {running}" + (f" · 新完成 {len(recent)}" if recent else "")
    return state, PET_LABELS[state], summary


PET_LABELS = {"error": "出事", "busy": "忙碌", "happy": "刚完成", "idle": "空闲"}

class Store:
    def __init__(self, jobs=DEFAULT_JOBS, board=DEFAULT_BOARD, state=DEFAULT_STATE):
        self.jobs = Path(jobs)
        self.board = Path(board)
        self.state = Path(state)  # Legacy constructor compatibility only; never read or written.
        self.user_state = self.board.with_name("aw_user_state.json")
        self._cache = {}

    def job_key(self, job):
        return str(self.jobs.resolve() / job["id"])

    @staticmethod
    def read_token(job):
        return [job["state"], job["started"], job.get("ended"), job.get("last_activity") if job["state"] == "died" else None]

    @staticmethod
    def state_entries(data):
        if (not isinstance(data, dict) or data.get("schema") != 1 or
                not isinstance(data.get("jobs"), dict) or
                any(not isinstance(v, dict) for v in data["jobs"].values())):
            raise ValueError("aw 用户状态格式错误")
        return data["jobs"]

    def load_user_state(self):
        if not self.user_state.exists():
            return {}, None
        try:
            return self.state_entries(json.loads(self.user_state.read_text(encoding="utf-8"))), None
        except (OSError, ValueError) as exc:
            return {}, "aw 用户状态读取失败：" + str(exc)

    def mark_read(self, job):
        if job["state"] not in TERMINAL or job.get("read"):
            return
        def change(data):
            entry = self.state_entries(data).setdefault(self.job_key(job), {})
            entry["read_token"] = self.read_token(job)
            user_stamp(entry)
        atomic_update(self.user_state, change, default={"schema": 1, "jobs": {}})

    def hide_jobs(self, job_ids, restore=False):
        # Validate live evidence again on each retry; never hide an active job.
        job_ids = tuple(dict.fromkeys(job_ids))
        if not job_ids:
            raise ValueError("没有可操作的已结束任务")
        if any(not name or Path(name).name != name or name in (".", "..") for name in job_ids):
            raise ValueError("任务编号无效")
        def change(data):
            entries = self.state_entries(data)
            for name in job_ids:
                job = self.job_info(self.jobs / name, time.time())
                if not restore and job["state"] not in TERMINAL:
                    raise ValueError("任务仍在运行，请刷新后重试")
                entry = entries.setdefault(self.job_key(job), {})
                entry["hidden"] = not restore
                if not restore:
                    entry["read_token"] = self.read_token(job)
                user_stamp(entry)
        atomic_update(self.user_state, change, default={"schema": 1, "jobs": {}})

    def job_info(self, directory, now):
        meta_path = directory / "meta.json"
        try:
            meta = json.loads(read_text(meta_path) or "{}")
            if not isinstance(meta, dict):
                meta = {}
        except ValueError:
            meta = {}
        started = number(meta.get("started"), directory.stat().st_mtime)
        output = directory / "output.txt"
        out_sig = signature(output)
        out_time = out_sig[2] / 1e9 if out_sig else None
        out_ago = max(0, now - out_time) if out_sig else None
        hb = read_text(directory / "heartbeat").strip()
        hb_time = int(hb) if hb.isdigit() else None
        hb_ago = max(0, now - hb_time) if hb_time is not None else None
        last_activity = max((t for t in (hb_time, out_time) if t is not None), default=started)
        exit_path = directory / "exit_code"
        exit_sig = signature(exit_path)
        rc = read_text(exit_path).strip() if exit_sig else None
        ended = exit_path.stat().st_mtime if exit_sig else None
        if rc is not None:
            state = "ok" if rc == "0" else "fail"
        else:
            pid = read_text(directory / "pid").strip()
            if pid and not pid_alive(pid) and (hb_ago is None or hb_ago > 120):
                state = "died"
            elif out_ago is not None and out_ago > 600:
                state = "stalled"
            else:
                state = "running"
        archived = state == "died" and now - last_activity > DIED_HISTORY_SECONDS
        if state == "died":
            ended = last_activity
        agent = str(meta.get("agent") or "?")
        cache_key = (out_sig, signature(directory / "last_message.txt"), agent)
        cached = self._cache.get(str(directory))
        terminal = rc is not None or state == "died"
        if terminal and cached and cached[0] == cache_key:
            parsed = cached[1]
        else:
            parsed = parse_output(read_tail(output), agent)
            if agent == "codex" and terminal:
                final = read_text(directory / "last_message.txt", limit=TAIL_BYTES).strip()
                if final:
                    parsed["result"] = final
            if terminal:
                self._cache[str(directory)] = (cache_key, parsed)
        prompt = read_text(directory / "prompt.txt")
        cards = task_cards(prompt, meta.get("cwd"))
        duration = parsed["duration_seconds"]
        elapsed = ((last_activity - started) / 60 if state == "died" else
                   duration / 60 if ended is not None and duration is not None else
                   ((ended if ended is not None else now) - started) / 60)
        return dict(id=directory.name, agent=agent, model=meta.get("model") or "未知模型",
                    effort=meta.get("effort") or "默认", started=started, ended=ended,
                    last_activity=last_activity, archived=archived,
                    elapsed_min=round(max(0, elapsed), 1), limit_min=number(meta.get("minutes"), None),
                    hb_ago=None if hb_ago is None else round(hb_ago), rc=rc, state=state,
                    title=title_from_prompt(prompt), prompt="\n".join(prompt.splitlines()[:40]),
                    purpose=purpose_from_prompt(prompt, cards),
                    current=(progress_sentence(cards, started) if not terminal else "") or latest_step(parsed["steps"]),
                    latest=latest_step(parsed["steps"]),
                    result_summary=first_sentence(last_paragraph(parsed["result"]) if agent != "codex" else parsed["result"]),
                    **parsed)

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        jobs, errors = [], []
        try:
            directories = list(self.jobs.iterdir())
        except OSError as exc:
            directories = []
            errors.append("任务目录读取失败：" + str(exc))
        for directory in directories:
            if not directory.is_dir():
                continue
            try:
                jobs.append(self.job_info(directory, now))
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                errors.append(directory.name + "：" + str(exc))
        existing = {str(d) for d in directories}
        self._cache = {k: v for k, v in self._cache.items() if k in existing}
        board, error = load_board(self.board)
        if error:
            errors.append(error)
        user_state, error = self.load_user_state()
        if error:
            errors.append(error)
        for job in jobs:
            entry = user_state.get(self.job_key(job), {})
            job["read"] = entry.get("read_token") == self.read_token(job)
            job["hidden"] = entry.get("hidden") is True and job["state"] in TERMINAL
        jobs.sort(key=lambda j: j["ended"] or j["started"], reverse=True)
        decisions = sorted(board["decisions"], key=lambda d: ({"high": 0, "normal": 1, "low": 2}.get(d.get("priority"), 1), str(d.get("created", ""))))
        return {"now": now, "jobs": jobs, "decisions": decisions, "progress": board["progress"],
                "archived": board.get("archived", []),
                "board_updated": board.get("updated"), "errors": errors,
                "counts": {"codex": sum(j["agent"] == "codex" and j["state"] in ("running", "stalled") for j in jobs),
                           "antigravity": sum(j["agent"] == "antigravity" and j["state"] in ("running", "stalled") for j in jobs),
                           "decisions": sum(d.get("status") == "open" for d in decisions)}}


def visible_jobs(snapshot, agent, history=False, hidden=False):
    jobs = [j for j in snapshot["jobs"] if j["agent"] == agent and bool(j.get("hidden")) == hidden]
    active = sorted((j for j in jobs if is_active(j)), key=lambda j: j["started"], reverse=True)
    done = sorted((j for j in jobs if not is_active(j)), key=lambda j: j["ended"] or j["started"], reverse=True)
    return active + (done if history or hidden else done[:2])


def dump_snapshot(snapshot, history=False):
    counts = snapshot["counts"]
    lines = [datetime.fromtimestamp(snapshot["now"]).strftime("%Y-%m-%d %H:%M:%S") +
             f" · GPT 运行中 {counts['codex']} · Gemini 运行中 {counts['antigravity']} · 待拍板 {counts['decisions']}", "", "Claude · 待你拍板"]
    for d in snapshot["decisions"]:
        if d.get("status") == "open":
            lines.append(f"{'●' if d.get('priority') == 'high' else '·'} {d.get('title', '')}")
            lines.append(f"  {d.get('project', '')} · 推荐：{d.get('recommend') or '无'}\n")
    lines.append("项目进度")
    for p in snapshot["progress"]:
        lines.append(f"[{p.get('status', '')}] {p.get('project', '')}：{p.get('stage', '')} → {p.get('next', '')}")
    lines.append("刚拍板（Claude 未读）")
    unseen = decisions_unseen(snapshot["decisions"])
    for d in unseen:
        lines.append(f"· {d.get('project', '')}：{d.get('title', '')} [{d.get('id', '')}]")
        lines.append("  选择：" + str(d.get("choice", "")) + " · 备注：" + str(d.get("note", "")))
    if not unseen:
        lines.append("暂无")
    for agent, label in (("codex", "GPT"), ("antigravity", "Gemini")):
        lines += ["", label]
        for j in visible_jobs(snapshot, agent, history):
            active = is_active(j)
            label = STATE_LABELS[j["state"]]
            timing = (f"{j['elapsed_min']:g}/{j['limit_min'] or '?':g} 分钟" if j["limit_min"] else
                      f"{j['elapsed_min']:g}/? 分钟") if active else ending_age(j, snapshot["now"])
            new = " · NEW" if is_new(j, snapshot["now"]) else ""
            lines.append(f"{label} {j['title']} · {timing}{new}")
            if active:
                if j["purpose"]:
                    lines.append("  目的：" + j["purpose"])
                lines.append("  现在：" + status_reason(j))
            else:
                lines.append("  结果：" + (j["result_summary"] or "暂无最终回复"))
            lines.append("")
    lines += snapshot["errors"]
    return "\n".join(lines)


class Changes:
    """One notification per id per session; no startup or reappearance replay."""
    def __init__(self):
        self.initialized = False
        self.terminal_seen = set()
        self.open_seen = set()

    def poll(self, snapshot):
        terminal = {j["id"] for j in snapshot["jobs"] if j["state"] in ("ok", "fail", "died")}
        opened = {d.get("id") for d in snapshot["decisions"] if d.get("status") == "open"}
        messages = []
        if self.initialized:
            for job in snapshot["jobs"]:
                if job["id"] in terminal - self.terminal_seen:
                    messages.append(("任务结束 · " + {"ok": "成功", "fail": "失败", "died": "进程失联"}[job["state"]], job["title"]))
            for d in snapshot["decisions"]:
                if d.get("status") == "open" and d.get("id") not in self.open_seen:
                    messages.append(("有新的待拍板事项", str(d.get("title", ""))))
        self.initialized = True
        self.terminal_seen.update(terminal)
        self.open_seen.update(opened)
        return messages
