#!/usr/bin/env python3
"""Local stdio MCP server that lets Claude dispatch work to local agent CLIs.

Supported CLIs: Google Antigravity CLI (`agy`, headless `agy -p`) and OpenAI Codex CLI
(`codex exec`). Each CLI keeps its own official login; this server only starts the local
binary. Standard library only.

State and logs live under `AGENT_DECK_HOME` (default `~/.local/share/agent-deck/`):
- `logs/agents_calls.jsonl` and `logs/agents_errors.log`
- `jobs/<job_id>/` (prompt.txt, meta.json, output.txt, exit_code, ...)
- `agents_defaults.json` (overridable via `AGENTS_DEFAULTS_FILE`)
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home()
DECK_HOME = Path(os.environ.get("AGENT_DECK_HOME") or (HOME / ".local" / "share" / "agent-deck")).expanduser()
LOG_DIR = DECK_HOME / "logs"
JOB_DIR = Path(os.environ["AGENTS_JOB_DIR"]).expanduser() if os.environ.get("AGENTS_JOB_DIR") else (DECK_HOME / "jobs")
DEFAULTS = (
    Path(os.environ["AGENTS_DEFAULTS_FILE"]).expanduser()
    if os.environ.get("AGENTS_DEFAULTS_FILE")
    else (DECK_HOME / "agents_defaults.json")
)


def _load_allowed_roots():
    raw = os.environ.get("AGENTS_ALLOWED_ROOTS", "").strip()
    if not raw:
        return [HOME.resolve()]
    roots = [Path(p).expanduser().resolve() for p in raw.split(os.pathsep) if p.strip()]
    return roots or [HOME.resolve()]


# Agents may only be started inside these folders.
ALLOWED_ROOTS = _load_allowed_roots()
MAX_OUT = 40000
EXTRA_PATH = [
    str(HOME / ".local" / "bin"),
    str(HOME / ".npm-global" / "bin"),
    str(HOME / ".bun" / "bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
] + sorted(str(p) for p in (HOME / ".nvm" / "versions" / "node").glob("*/bin"))
ENV = {**os.environ, "PATH": os.pathsep.join(EXTRA_PATH + [os.environ.get("PATH", "/usr/bin:/bin")])}


def agy_bin():
    return shutil.which("agy", path=ENV["PATH"])


def codex_bin():
    return shutil.which("codex", path=ENV["PATH"])


TOOLS = [
    {
        "name": "agent_check",
        "description": "Check which agent CLIs are installed and logged in (agy --version; optional one-line probe).",
        "inputSchema": {"type": "object", "properties": {"probe": {"type": "boolean"}}},
    },
    {
        "name": "agent_models",
        "description": "List models: agy via `agy models`; codex via `codex debug models` if supported, plus ~/.codex/config.toml.",
        "inputSchema": {"type": "object", "properties": {"agent": {"type": "string", "enum": ["antigravity", "codex"]}}},
    },
    {
        "name": "agent_defaults_get",
        "description": "Read default model/effort per agent and the user-owned allowlists (AGENT_DECK_HOME/agents_defaults.json).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "agent_defaults_set",
        "description": (
            "Change one agent's default model and/or effort. ONLY call after the user explicitly approved this exact "
            "change in chat; put their words in user_approval. Values must be inside the user's allowlists "
            "(allowed_models / allowed_efforts, which only the user edits). Backs up the file and logs the change."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "enum": ["antigravity", "codex"]},
                "model": {"type": "string", "description": "New default model; \"\" clears it (CLI's own default)"},
                "effort": {"type": "string", "description": "New default effort; \"\" clears it"},
                "user_approval": {"type": "string", "description": "The user's approving message, quoted"},
            },
            "required": ["agent", "user_approval"],
        },
    },
    {
        "name": "agent_run",
        "description": (
            "Start a task in the background on agy (antigravity) or Codex (`codex exec`, workspace-write sandbox "
            "limited to cwd, network enabled for remote commands). Live progress via agent_status. "
            "cwd must be inside AGENTS_ALLOWED_ROOTS (defaults to $HOME). Returns job_id. Ask the agent to write results to a file."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "enum": ["antigravity", "codex"]},
                "prompt": {"type": "string"},
                "cwd": {"type": "string"},
                "model": {"type": "string", "description": "Model slug; omit to use agent_defaults. Must be in that agent's allowed_models if the user set a list"},
                "effort": {"type": "string", "description": "Thinking effort (e.g. low/medium/high; codex also minimal/xhigh); omit to use agent_defaults"},
                "subagent": {"type": "string", "description": "Optional named agy agent (--agent)"},
                "json_schema": {"type": "string", "description": "Optional --json-schema (schema string, file path or type)"},
                "minutes": {"type": "integer", "description": "Time limit, default 30, max 180"},
                "sandbox": {"type": "boolean", "description": "agy only: add --sandbox"},
                "continue_job": {"type": "string", "description": "Earlier job_id whose conversation to continue"},
            },
            "required": ["agent", "prompt", "cwd"],
        },
    },
    {
        "name": "agent_status",
        "description": "Job status: finished?, last steps (tool names / paths), step count, token usage so far, final result.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "steps": {"type": "integer", "description": "How many recent steps to show, default 8"},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "agent_diagnose",
        "description": (
            "Debug a job: liveness (running/finished/died/stalled, signal), events.log, stderr, last raw "
            "output lines, wrapper errors and MCP server error log."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "lines": {"type": "integer"},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "agent_stop",
        "description": "Stop a running job.",
        "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
    },
    {
        "name": "agent_jobs",
        "description": "List recent jobs with state, model, duration and token totals (usage accounting).",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}},
    },
    {
        "name": "agent_config_get",
        "description": "Read agy settings (~/.gemini/antigravity-cli/settings.json).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "agent_config_set",
        "description": (
            "Set one agy setting by dotted key (e.g. 'model.name' or 'ui.theme') to a JSON value. "
            "Keys under 'permissions' and auth/provider keys are refused: those stay with the user."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value_json": {"type": "string", "description": "JSON literal, e.g. \"\\\"x\\\"\" or true or 3"},
            },
            "required": ["key", "value_json"],
        },
    },
]


def log(record):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (LOG_DIR / "agents_calls.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"log failed: {exc}", file=sys.stderr)


def clip(text):
    return text if len(text) <= MAX_OUT else text[: MAX_OUT // 2] + "\n...[clipped]...\n" + text[-MAX_OUT // 2:]


def check_cwd(raw):
    path = Path(raw).resolve()
    if not path.is_dir():
        raise ValueError(f"cwd does not exist: {path}")
    for base in ALLOWED_ROOTS:
        if path == base or base in path.parents:
            return path
    raise ValueError("cwd must stay inside: " + ", ".join(str(b) for b in ALLOWED_ROOTS))


def run(cmd, timeout, cwd=REPO_ROOT):
    try:
        p = subprocess.run(cmd, cwd=cwd, env=ENV, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return {"exit_code": p.returncode, "stdout": clip(p.stdout), "stderr": clip(p.stderr)}
    except subprocess.TimeoutExpired:
        return {"exit_code": "timeout"}
    except OSError as exc:
        return {"exit_code": "oserror", "stderr": str(exc)}


def agent_check(args):
    c = codex_bin()
    res = {"codex": {"binary": c}}
    if c:
        res["codex"]["version"] = run([c, "--version"], 30)
        res["codex"]["login"] = run([c, "login", "status"], 30)
    b = agy_bin()
    res["antigravity"] = {"binary": b}
    if b:
        res["antigravity"]["version"] = run([b, "--version"], 30)
        if args.get("probe"):
            res["antigravity"]["probe"] = run([b, "-p", "Reply with the single word OK.",
                                               "--output-format", "json", "--print-timeout", "2m"], 180)
    return res


SETTINGS = HOME / ".gemini" / "antigravity-cli" / "settings.json"
PROTECTED = ("permissions", "auth", "modelProvider", "gcp", "security")


DEFAULTS_INIT = {
    "_说明": "Claude 调度 agy/Codex 时的默认模型与思考程度。default_* 由 Claude 在你批准后用 agent_defaults_set 修改；"
             "allowed_* 只由你手改（空列表=不限制），Claude 单次任务指定的模型/思考程度也必须在名单内。改完即生效，无需重启。",
    "antigravity": {"default_model": "", "default_effort": "",
                    "allowed_models": [], "allowed_efforts": []},
    "codex": {"default_model": "", "default_effort": "",
              "allowed_models": [], "allowed_efforts": []},
}


def load_defaults():
    if not DEFAULTS.exists():
        DEFAULTS.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        DEFAULTS.write_text(json.dumps(DEFAULTS_INIT, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return json.loads(DEFAULTS.read_text(encoding="utf-8"))


def pick(agent, args):
    """Resolve model/effort for a run: explicit arg, else default; enforce the user's allowlists."""
    d = load_defaults().get(agent, {})
    model = args.get("model") or d.get("default_model") or None
    effort = args.get("effort") or d.get("default_effort") or None
    if model and d.get("allowed_models") and model not in d["allowed_models"]:
        raise ValueError(f"model '{model}' not in {agent}.allowed_models {d['allowed_models']} (user-managed)")
    if effort and d.get("allowed_efforts") and effort not in d["allowed_efforts"]:
        raise ValueError(f"effort '{effort}' not in {agent}.allowed_efforts {d['allowed_efforts']} (user-managed)")
    return model, effort


def agent_defaults_get(args):
    return {"path": str(DEFAULTS), "defaults": load_defaults()}


def agent_defaults_set(args):
    agent = args["agent"]
    approval = (args.get("user_approval") or "").strip()
    if not approval:
        return {"error": "refused: need the user's approving message in user_approval"}
    d = load_defaults()
    a = d.setdefault(agent, {})
    changes = {}
    for key, field, allow in (("model", "default_model", "allowed_models"), ("effort", "default_effort", "allowed_efforts")):
        if key in args:
            v = args[key].strip()
            if v and a.get(allow) and v not in a[allow]:
                return {"error": f"refused: '{v}' not in {agent}.{allow} {a[allow]} (only the user edits allowlists)"}
            changes[field] = {"old": a.get(field, ""), "new": v}
            a[field] = v
    if not changes:
        return {"error": "nothing to change: give model and/or effort"}
    backup = DEFAULTS.with_name(DEFAULTS.name + time.strftime(".bak_%Y%m%d_%H%M%S"))
    backup.write_text(DEFAULTS.read_text(encoding="utf-8"), encoding="utf-8")
    DEFAULTS.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(LOG_DIR / "agents_defaults_changes.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "agent": agent, "changes": changes,
                            "user_approval": approval}, ensure_ascii=False) + "\n")
    return {"agent": agent, "changes": changes, "backup": str(backup)}


def job_path(job_id):
    name = Path(job_id).name
    path = JOB_DIR / name
    if path.is_dir():
        return path
    raise ValueError("unknown job")


def job_conversation_id(job_id):
    """Read the cached ID or parse it without modifying historical evidence."""
    path = job_path(job_id)
    cached = path / "conversation_id"
    if cached.is_file() and cached.read_text().strip():
        return cached.read_text().strip()
    out = path / "output.txt"
    return parse_output(out.read_text(errors="replace"))["conversation_id"] if out.is_file() else None


def agent_models(args):
    if args.get("agent") == "codex":
        b = codex_bin()
        if not b:
            return {"error": "codex not found"}
        res = {"debug_models": run([b, "debug", "models"], 60)}
        cfg = HOME / ".codex" / "config.toml"
        res["config_toml"] = cfg.read_text(encoding="utf-8")[:4000] if cfg.exists() else None
        res["defaults"] = load_defaults().get("codex")
        return res
    b = agy_bin()
    return {"agy_models": run([b, "models"], 60), "defaults": load_defaults().get("antigravity")} if b else {"error": "agy not found"}


def agent_run(args):
    agent = args["agent"]
    cwd = check_cwd(args["cwd"])
    try:
        model, effort = pick(agent, args)
    except ValueError as exc:
        return {"error": str(exc)}
    args = {**args, "model": model, "effort": effort}
    minutes = max(1, min(int(args.get("minutes", 30)), 180))
    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    path = JOB_DIR / job_id
    conv = None
    if args.get("continue_job"):
        conv = job_conversation_id(args["continue_job"])
    if agent == "antigravity":
        b = agy_bin()
        if not b:
            return {"error": "agy not found"}
        cmd = [b, "-p", args["prompt"], "--output-format", "stream-json", "--print-timeout", f"{minutes}m"]
        for key, flag in (("model", "--model"), ("effort", "--effort"), ("subagent", "--agent"),
                          ("json_schema", "--json-schema")):
            if args.get(key):
                cmd += [flag, str(args[key])]
        if args.get("sandbox"):
            cmd += ["--sandbox"]
        if conv:
            cmd += ["--conversation", conv]
        shown = cmd[:1] + cmd[3:]
    elif agent == "codex":
        b = codex_bin()
        if not b:
            return {"error": "codex not found in PATH (" + ENV["PATH"] + ")"}
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        opts = ["--json", "--skip-git-repo-check", "-C", str(cwd), "-s", "workspace-write",
                "-c", "sandbox_workspace_write.network_access=true",
                "-o", str(path / "last_message.txt")]
        if args.get("model"):
            opts += ["-m", args["model"]]
        if args.get("effort"):
            opts += ["-c", f'model_reasoning_effort="{args["effort"]}"']
        # exec-level options must precede the `resume` subcommand (clap rejects -C after it)
        cmd = [b, "exec"] + opts + (["resume", conv] if conv else []) + [args["prompt"]]
        shown = cmd[:-1] + ["<prompt>"]
    else:
        return {"error": f"unknown agent {agent}"}
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    (path / "prompt.txt").write_text(args["prompt"], encoding="utf-8")
    meta = {"agent": agent, "cwd": str(cwd), "cmd": shown, "started": time.time(), "minutes": minutes,
            "model": model, "effort": effort}
    (path / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
    # wrapper: hard time limit (perl alarm), events.log, 30 s heartbeat, ignores HUP, records signals,
    # exit code, and a macOS notification when the job ends
    script = r"""
ev() { echo "$(date +%Y-%m-%dT%H:%M:%S) $*" >>"$JOB/events.log"; }
trap 'ev "wrapper got HUP (ignored)"' HUP
trap 'ev "wrapper got TERM, forwarding"; kill -TERM $child 2>/dev/null' TERM
trap 'ev "wrapper got INT, forwarding"; kill -INT $child 2>/dev/null' INT
ev "start limit=${LIMIT}s"
nohup perl -e 'alarm shift; exec @ARGV' "$LIMIT" "$@" >"$JOB/output.txt" 2>"$JOB/stderr.txt" &
child=$!; echo $child >"$JOB/child_pid"
( while kill -0 $child 2>/dev/null; do date +%s >"$JOB/heartbeat"; sleep 30; done ) &
wait $child; rc=$?
while kill -0 $child 2>/dev/null; do wait $child; rc=$?; done
ev "exit rc=$rc"
echo $rc >"$JOB/exit_code"
osascript -e "display notification \"$NAME 结束，exit=$rc\" with title \"agents-bridge\"" >/dev/null 2>&1 || true
"""
    env = {**ENV, "JOB": str(path), "LIMIT": str(minutes * 60 + 120), "NAME": f"{agent} {job_id}"}
    proc = subprocess.Popen(["/bin/sh", "-c", script, "job"] + cmd, cwd=cwd, env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=open(path / "wrapper_stderr.txt", "w"), start_new_session=True)
    (path / "pid").write_text(str(proc.pid))
    return {"job_id": job_id, "agent": agent, "cwd": str(cwd), "minutes": minutes, "model": model, "effort": effort, "cmd": shown}


def add_usage(tot, u):
    for k, v in (u or {}).items():
        if isinstance(v, (int, float)):
            tot[k] = tot.get(k, 0) + v


def parse_output(text):
    """Works for both stream-json (NDJSON) and single-envelope json output."""
    info = {"steps": [], "usage": {}, "final": None, "conversation_id": None}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = ev.get("event")
        if kind == "init":
            info["conversation_id"] = ev.get("conversation_id")
        elif kind == "step_update":
            su = ev.get("step_update", {})
            info["conversation_id"] = info["conversation_id"] or su.get("conversation_id")
            if su.get("state") == "DONE":
                add_usage(info["usage"], su.get("usage"))
            ti = su.get("tool_info") or {}
            params = ti.get("parameters") or {}
            brief = {k: str(v)[:160] for k, v in params.items()
                     if k in ("Path", "CommandLine", "Command", "AbsolutePath", "DirectoryPath", "Query", "Url")}
            info["steps"].append({"i": su.get("step_index"), "state": su.get("state"),
                                  "type": su.get("step_type"), "tool": su.get("tool_name"), **brief})
        elif ev.get("type") == "thread.started":
            info["conversation_id"] = ev.get("thread_id")
        elif ev.get("type") in ("item.started", "item.completed"):
            it = ev.get("item") or {}
            st = {"i": it.get("id"), "state": "DONE" if ev["type"] == "item.completed" else "ACTIVE",
                  "type": it.get("type")}
            if it.get("command"):
                st["CommandLine"] = str(it["command"])[:160]
            if it.get("changes"):
                st["Path"] = ", ".join(str(c.get("path")) for c in it["changes"])[:160]
            if it.get("type") == "agent_message":
                info["last_text"] = it.get("text")
            info["steps"].append(st)
        elif ev.get("type") == "turn.completed":
            add_usage(info["usage"], ev.get("usage"))
            info["final"] = {"status": "SUCCESS", "usage": ev.get("usage"), "response": info.get("last_text", "")}
        elif ev.get("type") in ("turn.failed", "error"):
            info["final"] = {"status": "ERROR", "response": json.dumps(ev, ensure_ascii=False)[:2000]}
        elif kind == "result" or (kind is None and "status" in ev):
            info["final"] = ev.get("result", ev) if isinstance(ev.get("result"), dict) else ev
            info["conversation_id"] = info["conversation_id"] or info["final"].get("conversation_id")
    # keep only the latest state per step index
    seen, order = {}, []
    for st in info["steps"]:
        if st["i"] not in seen:
            order.append(st["i"])
        seen[st["i"]] = st
    info["steps"] = [seen[k] for k in order if k is not None]
    return info


SIGNALS = {129: "SIGHUP", 130: "SIGINT", 134: "SIGABRT", 137: "SIGKILL", 142: "SIGALRM(time limit)",
           143: "SIGTERM", 141: "SIGPIPE"}


def alive(pidfile):
    try:
        os.kill(int(pidfile.read_text().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


def liveness(path):
    """running / finished / died (no exit code and no process) / stalled (no output for 10 min)."""
    now = time.time()
    hb = path / "heartbeat"
    out = path / "output.txt"
    d = {"last_heartbeat_s_ago": round(now - int(hb.read_text().strip())) if hb.exists() else None,
         "last_output_s_ago": round(now - out.stat().st_mtime) if out.exists() else None}
    if (path / "exit_code").exists():
        rc = (path / "exit_code").read_text().strip()
        d["state"] = "finished"
        if rc.isdigit() and int(rc) > 128:
            d["killed_by"] = SIGNALS.get(int(rc), f"signal {int(rc) - 128}")
    elif not alive(path / "pid") and not ((path / "child_pid").exists() and alive(path / "child_pid")):
        d["state"] = "died"
        d["note"] = "process gone without exit code (e.g. machine sleep, app restart, kill -9)"
    elif d["last_output_s_ago"] is not None and d["last_output_s_ago"] > 600:
        d["state"] = "stalled?"
    else:
        d["state"] = "running"
    return d


def tail(path, n):
    return "\n".join(path.read_text(errors="replace").splitlines()[-n:]) if path.exists() else ""


def agent_diagnose(args):
    path = job_path(args["job_id"])
    return {"job_id": path.name, "liveness": liveness(path),
            "meta": json.loads((path / "meta.json").read_text()) if (path / "meta.json").exists() else None,
            "events_log": tail(path / "events.log", 40), "stderr": clip(tail(path / "stderr.txt", 80)),
            "wrapper_stderr": tail(path / "wrapper_stderr.txt", 20),
            "output_last_lines": clip(tail(path / "output.txt", int(args.get("lines", 15)))),
            "server_errors": tail(LOG_DIR / "agents_errors.log", 30)}


def agent_status(args):
    path = job_path(args["job_id"])
    out = (path / "output.txt").read_text(errors="replace") if (path / "output.txt").exists() else ""
    err = (path / "stderr.txt").read_text(errors="replace") if (path / "stderr.txt").exists() else ""
    done = (path / "exit_code").exists()
    info = parse_output(out)
    meta = json.loads((path / "meta.json").read_text()) if (path / "meta.json").exists() else {}
    res = {"job_id": path.name, "finished": done,
           "exit_code": (path / "exit_code").read_text().strip() if done else None,
           "elapsed_min": round((time.time() - meta.get("started", time.time())) / 60, 1),
           **liveness(path),
           "n_steps": len(info["steps"]), "recent_steps": info["steps"][-int(args.get("steps", 8)):],
           "usage_so_far": info["usage"]}
    lm = path / "last_message.txt"
    if done and lm.exists() and not (info["final"] or {}).get("response"):
        info["final"] = {**(info["final"] or {"status": "SUCCESS" if res_ok(path) else "ERROR"}),
                         "response": lm.read_text(errors="replace")}
    if info["final"]:
        f = info["final"]
        res.update({"status": f.get("status"), "duration_seconds": f.get("duration_seconds"),
                    "usage": f.get("usage"), "result": clip(str(f.get("response", f.get("result", ""))))})
    res["stderr_tail"] = clip("\n".join(err.splitlines()[-10:]))
    return res


def res_ok(path):
    return (path / "exit_code").exists() and (path / "exit_code").read_text().strip() == "0"


def agent_stop(args):
    path = job_path(args["job_id"])
    pid = int((path / "pid").read_text())
    with (path / "events.log").open("a") as fh:
        fh.write(time.strftime("%Y-%m-%dT%H:%M:%S") + " agent_stop requested by Claude\n")
    try:
        os.killpg(pid, 15)
        return {"stopped": path.name}
    except ProcessLookupError:
        return {"stopped": path.name, "note": "already exited"}


def agent_jobs(args):
    rows = []
    paths = {}
    if JOB_DIR.is_dir():
        for path in JOB_DIR.iterdir():
            if path.is_dir():
                paths.setdefault(path.name, path)
    if paths:
        for name in sorted(paths, reverse=True)[: int(args.get("limit", 15))]:
            path = paths[name]
            try:
                st = agent_status({"job_id": path.name, "steps": 0})
                meta = json.loads((path / "meta.json").read_text())
                rows.append({"job_id": path.name, "agent": meta.get("agent"), "finished": st["finished"], "exit_code": st["exit_code"],
                             "status": st.get("status"), "model": meta.get("model"), "effort": meta.get("effort"),
                             "elapsed_min": st["elapsed_min"], "n_steps": st["n_steps"],
                             "tokens": (st.get("usage") or st["usage_so_far"]).get("total_tokens")})
            except Exception as exc:
                rows.append({"job_id": path.name, "error": str(exc)})
    return {"jobs": rows, "note": "Remaining account quota is not exposed by agy headless mode."}


def load_settings():
    if SETTINGS.exists() and SETTINGS.read_text(encoding="utf-8").strip():
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    return {}


def agent_config_get(args):
    return {"path": str(SETTINGS), "settings": load_settings()}


def agent_config_set(args):
    key = args["key"].strip()
    if not key or key.split(".")[0] in PROTECTED:
        return {"error": f"refused: '{key}' is user-managed (permissions/auth/provider)"}
    value = json.loads(args["value_json"])
    d = load_settings()
    if SETTINGS.exists():
        backup = SETTINGS.with_name(SETTINGS.name + time.strftime(".bak_%Y%m%d_%H%M%S"))
        backup.write_text(SETTINGS.read_text(encoding="utf-8"), encoding="utf-8")
    node = d
    parts = key.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    old = node.get(parts[-1])
    node[parts[-1]] = value
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"key": key, "old": old, "new": value}


HANDLERS = {"agent_check": agent_check, "agent_models": agent_models, "agent_run": agent_run,
            "agent_status": agent_status, "agent_stop": agent_stop, "agent_diagnose": agent_diagnose, "agent_jobs": agent_jobs,
            "agent_config_get": agent_config_get, "agent_config_set": agent_config_set,
            "agent_defaults_get": agent_defaults_get, "agent_defaults_set": agent_defaults_set}


def respond(msg_id, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": msg_id}
    msg["error" if error is not None else "result"] = error if error is not None else result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(msg):
    method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if msg_id is None:
        return
    if method == "initialize":
        respond(msg_id, {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                         "capabilities": {"tools": {"listChanged": False}},
                         "serverInfo": {"name": "agents-bridge", "version": "0.5.0"}})
    elif method == "ping":
        respond(msg_id, {})
    elif method == "tools/list":
        respond(msg_id, {"tools": TOOLS})
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in HANDLERS:
            respond(msg_id, error={"code": -32602, "message": f"unknown tool {name}"})
            return
        started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        try:
            result = HANDLERS[name](args)
            is_error = bool(result.get("error"))
        except Exception as exc:
            result, is_error = {"error": f"{type(exc).__name__}: {exc}"}, True
            server_error(f"tool {name} failed", traceback.format_exc())
        log({"time": started, "tool": name, "arguments": {k: (v[:200] if isinstance(v, str) else v)
             for k, v in args.items()}, "error": result.get("error"), "job_id": result.get("job_id")})
        respond(msg_id, {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=1)}],
                         "isError": is_error})
    else:
        respond(msg_id, error={"code": -32601, "message": f"method not found: {method}"})


def server_error(what, detail=""):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (LOG_DIR / "agents_errors.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} pid={os.getpid()} {what}\n{detail}\n")
    except OSError:
        pass


def on_signal(signum, _frame):
    server_error(f"server received signal {signum}, exiting")
    sys.exit(0)


def main():
    print(f"agents-bridge MCP ready, home={DECK_HOME}, agy={agy_bin()}, codex={codex_bin()}", file=sys.stderr)
    signal.signal(signal.SIGTERM, on_signal)
    server_error("server start", f"agy={agy_bin()} codex={codex_bin()}")
    for line in sys.stdin:
        line = line.strip()
        if line:
            try:
                handle(json.loads(line))
            except json.JSONDecodeError:
                continue
            except Exception:
                server_error("unhandled error in request loop", traceback.format_exc())
    server_error("server stdin closed (client disconnected), exiting")


if __name__ == "__main__":
    main()
