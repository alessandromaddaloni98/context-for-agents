#!/usr/bin/env python3
"""Deterministic half of the context-for-agents skill.

Builds the skeleton of context/CONTEXT.md, the handoff file passed from one
coding agent (Claude Code or Codex) to the other in the middle of a job:

1. finds the session transcript of the outgoing agent and extracts the
   chronology (user prompts and the files each prompt led to modify);
2. takes a commit-less git snapshot of the working tree and lists what changed
   since the previous handoff;
3. copies verbatim the sections inherited from the previous CONTEXT.md;
4. writes the new CONTEXT.md, leaving placeholders for the sections that only
   the agent can fill.

Run with --check after the agent has filled the placeholders.

Only the standard library is used. Tool outputs are never read into the
result, .env files are never added to the snapshot, and anything that looks
like a secret is masked before it is written.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

CONTEXT_DIR = "context"
CONTEXT_FILE = "CONTEXT.md"
HEADER_TAG = "context-for-agents v1"
PLACEHOLDER = "_DA COMPILARE"

INDEX_NAME = "context-for-agents.index"
STATE_NAME = "context-for-agents.state.json"
BACKUP_NAME = "context-for-agents.prev.md"

SEC_NEXT = "Prossima azione"
SEC_STOP = "Punto di arresto"
SEC_STEPS = "Stato degli step"
SEC_INSTRUCTIONS = "Istruzioni fuori spec"
SEC_ATTEMPTS = "Tentativi scartati"
SEC_CHECKS = "Verifiche eseguite"
SEC_CHANGES = "Modifiche di questo tratto"
SEC_OFF_MAP = "File fuori mappa"
SEC_TIMELINE = "Cronologia"
SEC_HISTORY = "Storico passaggi"

INHERITED_SECTIONS = (SEC_INSTRUCTIONS, SEC_ATTEMPTS, SEC_HISTORY)
REQUIRED_SECTIONS = (SEC_NEXT, SEC_STOP, SEC_CHECKS)

AGENT_LABELS = {"claude": "claude-code", "codex": "codex"}
CLAUDE_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

MIN_PROMPT_CHARS = 15
MAX_FILES_PER_TURN = 5
MAX_CHANGED_FILES = 40
NO_SPEC_MAX_AGE = timedelta(hours=24)
CODEX_ROLLOUTS_TO_SCAN = 60


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str, env: dict | None = None, check: bool = True) -> str:
    res = subprocess.run(
        ["git", "-c", "core.quotepath=off", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    if check and res.returncode != 0:
        raise GitError(res.stderr.strip() or f"git {' '.join(args)} failed")
    return res.stdout


def git_ok(repo: Path, *args: str) -> bool:
    res = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    return res.returncode == 0


def parse_time(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def iter_jsonl(path: Path, needles: tuple[str, ...] = ()):
    """Yield the JSON records of a JSONL file, skipping unreadable lines.

    `needles` is a cheap pre-filter: lines containing none of them are not
    even parsed, which matters for Codex rollouts that can weigh hundreds of MB.
    """
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if needles and not any(needle in line for needle in needles):
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def relative_to_repo(repo: Path, raw_path: str, base: Path | None = None) -> str | None:
    """Return the repo-relative POSIX path, or None if it is outside the repo."""
    if not raw_path:
        return None
    path = Path(raw_path)
    if not path.is_absolute():
        path = (base or repo) / path
    try:
        return Path(os.path.normpath(path)).relative_to(repo).as_posix()
    except ValueError:
        try:
            return path.resolve().relative_to(repo).as_posix()
        except (ValueError, OSError):
            return None


# --------------------------------------------------------------------------
# Prompt cleaning and secret masking
# --------------------------------------------------------------------------

MASK = "[omesso]"

SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S), MASK),
    (re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/:@]+:[^\s/@]+@"), r"\1" + MASK + "@"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{12,}"), r"\1 " + MASK),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"), MASK),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), MASK),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), MASK),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), MASK),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), MASK),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), MASK),
    (
        re.compile(
            r"(?i)\b([A-Za-z0-9_.-]*(?:password|passwd|pwd|secret|token(?!s)|api[_-]?key|access[_-]?key"
            r"|private[_-]?key|account[_-]?key|connection[_-]?string|conn[_-]?str|credential)[A-Za-z0-9_.-]*)"
            r"(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|\S+)"
        ),
        r"\1\2" + MASK,
    ),
]
# Long opaque strings mixing letters and digits (hashes, raw keys).
OPAQUE_TOKEN = re.compile(r"(?<![A-Za-z0-9+=])[A-Za-z0-9+=]{32,}(?![A-Za-z0-9+=])")

INJECTED_BLOCK = re.compile(
    r"<(system-reminder|ide_opened_file|ide_selection|ide_diagnostics|local-command-stdout"
    r"|local-command-stderr|local-command-caveat|command-message|command-contents"
    r"|environment_context|user_instructions|task-notification)\b[^>]*>.*?</\1>",
    re.S,
)
PASTED_BLOCK = re.compile(r"<pasted_content\b[^>]*>(.*?)(?:</pasted_content>|$)", re.S)
PASTED_EXCERPT_CHARS = 80
COMMAND_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.S)
COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
# The Codex IDE extension prepends the editor state to what the user typed.
CODEX_IDE_CONTEXT = re.compile(r"^\s*#\s*Context from my IDE setup:.*?##\s*My request(?: for Codex)?:\s*", re.S)
SKIP_PREFIXES = ("[Request interrupted", "<environment_context", "<user_instructions", "Caveat: The messages below")


def mask_secrets(text: str) -> str:
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)

    def _opaque(match: re.Match) -> str:
        token = match.group(0)
        has_digit = any(ch.isdigit() for ch in token)
        has_alpha = any(ch.isalpha() for ch in token)
        return MASK if has_digit and has_alpha else token

    return OPAQUE_TOKEN.sub(_opaque, text)


# Codex records the user's answer to a question asked by the agent as a user
# message holding a JSON list of {questionItemId, question, <answer fields>}.
QUESTION_REPLY = re.compile(
    r"^\s*<send_user_message_question_reply>\s*(.*?)\s*(?:</send_user_message_question_reply>\s*)?$", re.S
)
QUESTION_KEYS = {"questionItemId", "question", "id", "header", "options"}


def _flatten(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return ", ".join(filter(None, (_flatten(v) for v in value.values())))
    if isinstance(value, list):
        return ", ".join(filter(None, (_flatten(v) for v in value)))
    return "" if value is None else str(value)


def render_question_reply(body: str) -> str:
    label = "[risposta a una domanda dell'agente]"
    try:
        items = json.loads(body)
    except json.JSONDecodeError:
        return f"{label} {body}"
    parts = []
    for item in items if isinstance(items, list) else [items]:
        if not isinstance(item, dict):
            continue
        question = truncate(_flatten(item.get("question")), 70)
        answer = "; ".join(filter(None, (_flatten(v) for k, v in item.items() if k not in QUESTION_KEYS)))
        parts.append(f"«{question}» → {answer or 'risposta non leggibile'}")
    return f"{label} " + " | ".join(parts) if parts else label


def clean_prompt(raw: str) -> str:
    """Turn a raw user message into a single masked line, or '' if it is not a prompt."""
    if not raw:
        return ""
    command = COMMAND_NAME.search(raw)
    reply = QUESTION_REPLY.match(raw)
    if reply:
        text = render_question_reply(reply.group(1))
    elif command:
        args = COMMAND_ARGS.search(raw)
        text = f"{command.group(1).strip()} {args.group(1).strip() if args else ''}"
    else:
        text = CODEX_IDE_CONTEXT.sub("", raw, count=1)
        text = INJECTED_BLOCK.sub(" ", text)
        # A prompt written elsewhere and pasted is still the prompt: keep it.
        # Text pasted next to a typed prompt (a log, a trace) is cut to an excerpt.
        pasted = [re.sub(r"\s+", " ", mask_secrets(block)).strip() for block in PASTED_BLOCK.findall(text)]
        pasted = [block for block in pasted if block]
        typed = PASTED_BLOCK.sub(" ", text).strip()
        if typed and pasted:
            text = f"{typed} [incollato: {truncate(pasted[0], PASTED_EXCERPT_CHARS)}]"
        elif pasted:
            text = " ".join(pasted)
        else:
            text = typed
    text = text.strip()
    if not text or text.startswith(SKIP_PREFIXES):
        return ""
    text = mask_secrets(text)
    return re.sub(r"\s+", " ", text).strip()


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


# --------------------------------------------------------------------------
# Transcripts
# --------------------------------------------------------------------------


@dataclass
class Turn:
    time: datetime | None
    prompt: str
    files: list[str] = field(default_factory=list)
    shell: list[str] = field(default_factory=list)
    shell_files: list[str] = field(default_factory=list)

    def add_file(self, path: str) -> None:
        if path not in self.files:
            self.files.append(path)


# Agents often edit files from the shell (sed -i, an inline python script) instead
# of the edit tools. A command counts as a possible write only if it looks like one.
SHELL_WRITE = re.compile(
    r"\bsed\s+(?:-[A-Za-z]+\s+)*-[A-Za-z]*i"
    r"|\bperl\s+-[A-Za-z]*i"
    r"|\b(?:python3?|node|ruby|perl)\b[^|;&]*(?:<<|\s-[ce]\s)"
    r"|>>?\s*(?!/dev/null|&)[^\s>]"
    r"|\b(?:tee|mv|cp|rm|touch|patch|truncate)\b"
    r"|\bgit\s+(?:apply|checkout|restore|mv|rm|stash)\b"
    r"|apply_patch"
)
SHELL_TOOLS = {"Bash", "PowerShell"}


WRITE_COMMAND = re.compile(
    r"\bsed\s+(?:-[A-Za-z]+\s+)*-[A-Za-z]*i"
    r"|\bperl\s+-[A-Za-z]*i"
    r"|\b(?:tee|mv|rm|touch|patch|truncate)\b"
    r"|\bgit\s+(?:apply|checkout|restore|mv|rm|stash)\b"
    r"|apply_patch"
)
COPY_COMMAND = re.compile(r"\b(?:cp|install|rsync)\b")
INTERPRETER = re.compile(r"\b(?:python3?|node|ruby|perl)\b")
INLINE_CODE = re.compile(r"\b(?:python3?|node|ruby|perl)\b.*\s-[ce]\s")
CODE_WRITES = re.compile(r"\bwrite|['\"][wax]b?\+?['\"]|\.dump\(|\bshutil\b|\bos\.(?:remove|rename|replace)|unlink|appendFile")
REDIRECT = re.compile(r"(?<![0-9&])>>?\s*([^\s;|&<>]+)")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")


def _split_shell(text: str) -> list[str]:
    """Split a command line on ; | & and newlines that are outside quotes."""
    parts, buffer, quote = [], [], ""
    for char in text:
        if quote:
            quote = "" if char == quote else quote
            buffer.append(char)
        elif char in "'\"":
            quote = char
            buffer.append(char)
        elif char in ";|&\n":
            parts.append("".join(buffer))
            buffer = []
        else:
            buffer.append(char)
    parts.append("".join(buffer))
    return [part for part in parts if part.strip()]


def shell_writes(command: str, keys: list[str]) -> bool:
    """True when the command looks like it writes one of `keys` (a path or a file name).

    A file merely mentioned does not count: the path must be the target of a
    redirect, an argument of a writing command, or a whole string literal inside
    an inline script that writes. Text passed through a heredoc is not searched,
    unless the heredoc is the source of a script."""
    head: list[str] = []
    scripts: list[str] = []
    terminator, owner_is_script, body = "", False, []
    for line in command.splitlines():
        if terminator:
            if line.strip() == terminator:
                if owner_is_script:
                    scripts.append("\n".join(body))
                terminator, body = "", []
            else:
                body.append(line)
            continue
        head.append(line)
        opened = HEREDOC.search(line)
        if opened:
            terminator, owner_is_script = opened.group(2), bool(INTERPRETER.search(line))
    if terminator and owner_is_script:
        scripts.append("\n".join(body))

    names = "(?:" + "|".join(re.escape(key) for key in keys) + ")"
    prefix = r"(?:[^\s'\"=<>|;&()]*/)?"
    word = re.compile(r"(?:^|(?<=[\s'\"=]))" + prefix + names + r"(?=$|[\s'\";|&)])")
    literal = re.compile(r"['\"]" + prefix + names + r"['\"]")

    for segment in _split_shell("\n".join(head)):
        for target in REDIRECT.findall(segment):
            if word.fullmatch(target.strip("'\"")):
                return True
        if WRITE_COMMAND.search(segment) and word.search(segment):
            return True
        # A copy writes its last argument only: the others are read.
        if COPY_COMMAND.search(segment) and word.fullmatch(segment.split()[-1].strip("'\"")):
            return True
        if INLINE_CODE.search(segment) and literal.search(segment) and CODE_WRITES.search(segment):
            return True
    return any(literal.search(script) and CODE_WRITES.search(script) for script in scripts)


def attribute_shell_edits(turns: list[Turn], changed: list[str]) -> None:
    """Link a file to a turn when the file really changed in this stretch (git says so)
    and a shell command of that turn looks like it wrote it. It is an inference."""
    names: dict[str, int] = {}
    for path in changed:
        names[Path(path).name] = names.get(Path(path).name, 0) + 1
    for turn in turns:
        if not turn.shell:
            continue
        for path in changed:
            if path in turn.files or path in turn.shell_files:
                continue
            name = Path(path).name
            keys = [path] + ([name] if names[name] == 1 and name != path else [])
            if any(shell_writes(command, keys) for command in turn.shell):
                turn.shell_files.append(path)


@dataclass
class Transcript:
    path: Path
    session_id: str = ""
    version: str = ""
    records: int = 0
    turns: list[Turn] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def find_claude_transcript(repo: Path, cwd: Path, session_id: str) -> Path | None:
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    projects = config / "projects"
    if not projects.is_dir():
        return None

    def encode(path: Path) -> str:
        return re.sub(r"[^A-Za-z0-9]", "-", str(path))

    candidates = []
    for base in dict.fromkeys((cwd, repo)):
        for name in dict.fromkeys((encode(base), str(base).replace("/", "-"))):
            folder = projects / name
            if folder.is_dir():
                candidates.append(folder)

    if session_id:
        for folder in candidates + [p for p in projects.iterdir() if p.is_dir()]:
            hit = folder / f"{session_id}.jsonl"
            if hit.is_file():
                return hit

    files = [f for folder in candidates for f in folder.glob("*.jsonl")]
    if not files:
        # Last resort: any project folder whose records were written from this repo.
        for folder in projects.iterdir():
            if not folder.is_dir():
                continue
            for candidate in folder.glob("*.jsonl"):
                if _claude_file_is_in_repo(candidate, repo):
                    files.append(candidate)
    return max(files, key=lambda f: f.stat().st_mtime) if files else None


def _claude_file_is_in_repo(path: Path, repo: Path) -> bool:
    for index, record in enumerate(iter_jsonl(path)):
        cwd = record.get("cwd")
        if isinstance(cwd, str):
            return relative_to_repo(repo, cwd) is not None
        if index > 50:
            break
    return False


def _claude_prompt_text(record: dict) -> str | None:
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    texts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            return None
        if block.get("type") == "text":
            texts.append(block.get("text") or "")
    return "\n".join(texts) if texts else None


def parse_claude(path: Path, repo: Path) -> Transcript:
    """Read a Claude Code session transcript.

    Prompts: `user` records whose origin is human and that carry no tool_result.
    Edits: `tool_use` blocks of the write tools in `assistant` records; an edit
    whose tool_result reports an error is dropped.
    """
    transcript = Transcript(path=path)
    # Each entry: ("prompt", strict, Turn) or ("edit", tool_use_id, repo-relative path).
    events: list[tuple] = []
    failed: set[str] = set()
    seen_records: set[str] = set()
    seen_tool_uses: set[str] = set()

    for record in iter_jsonl(path):
        transcript.records += 1
        uuid = record.get("uuid")
        if uuid:
            if uuid in seen_records:
                continue
            seen_records.add(uuid)
        transcript.session_id = transcript.session_id or record.get("sessionId") or ""
        transcript.version = transcript.version or record.get("version") or ""
        kind = record.get("type")
        content = (record.get("message") or {}).get("content")

        if kind == "user":
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                        failed.add(block.get("tool_use_id") or "")
            if record.get("isMeta") or record.get("isCompactSummary") or record.get("isSidechain"):
                continue
            raw = _claude_prompt_text(record)
            if raw is None:
                continue
            text = clean_prompt(raw)
            if not text:
                continue
            origin = record.get("origin")
            strict = isinstance(origin, dict) and origin.get("kind") == "human"
            events.append(("prompt", strict, Turn(parse_time(record.get("timestamp")), text)))

        elif kind == "assistant" and isinstance(content, list):
            record_cwd = Path(record["cwd"]) if isinstance(record.get("cwd"), str) else None
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name")
                if name not in CLAUDE_WRITE_TOOLS and name not in SHELL_TOOLS:
                    continue
                tool_id = block.get("id") or ""
                if tool_id in seen_tool_uses:
                    continue
                seen_tool_uses.add(tool_id)
                tool_input = block.get("input") or {}
                if name in SHELL_TOOLS:
                    if isinstance(tool_input.get("command"), str):
                        events.append(("shell", tool_id, tool_input["command"]))
                    continue
                raw_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
                rel = relative_to_repo(repo, raw_path, record_cwd)
                if rel:
                    events.append(("edit", tool_id, rel))

    # Versions that mark the origin of each record let us keep human prompts only.
    has_origin = any(event[0] == "prompt" and event[1] for event in events)
    if not has_origin and any(event[0] == "prompt" for event in events):
        transcript.notes.append("campo origin assente: prompt riconosciuti con la regola larga")

    current: Turn | None = None
    for event in events:
        if event[0] == "prompt":
            if has_origin and not event[1]:
                continue
            current = event[2]
            transcript.turns.append(current)
        elif current is None:
            continue
        elif event[0] == "shell":
            # Kept even when the command exits non-zero: it may have written before failing.
            current.shell.append(event[2])
        elif event[1] not in failed:
            current.add_file(event[2])
    return transcript


def find_codex_rollout(repo: Path, session_id: str) -> Path | None:
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    sessions = home / "sessions"
    if not sessions.is_dir():
        return None
    rollouts = sorted(sessions.rglob("rollout-*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    if session_id:
        for rollout in rollouts:
            if session_id in rollout.name:
                return rollout
    for rollout in rollouts[:CODEX_ROLLOUTS_TO_SCAN]:
        for record in iter_jsonl(rollout):
            if record.get("type") == "session_meta":
                payload = record.get("payload") or {}
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and relative_to_repo(repo, cwd) is not None:
                    return rollout
            break
    return None


PATCH_FILE = re.compile(r"\*\*\* (?:Add|Update|Delete) File: ([^\n\\\"']+)")


def parse_codex(path: Path, repo: Path) -> Transcript:
    """Read a Codex CLI rollout.

    Prompts: `event_msg` records of type `user_message`.
    Edits: `patch_apply_end` events with success == true (keys of `changes`).
    If the rollout has no such events (older versions), fall back to the file
    headers found in apply_patch calls: those are attempts, not confirmed edits.
    """
    transcript = Transcript(path=path)
    needles = (
        '"session_meta"',
        '"user_message"',
        '"patch_apply_end"',
        '"custom_tool_call"',
        '"function_call"',
        '"local_shell_call"',
    )
    current: Turn | None = None
    confirmed_edits = False
    attempts: list[tuple[Turn, str]] = []
    session_cwd: Path | None = None

    for record in iter_jsonl(path, needles):
        transcript.records += 1
        kind = record.get("type")
        payload = record.get("payload") or {}
        if not isinstance(payload, dict):
            continue

        if kind == "session_meta":
            transcript.session_id = payload.get("id") or payload.get("session_id") or ""
            transcript.version = payload.get("cli_version") or ""
            if isinstance(payload.get("cwd"), str):
                session_cwd = Path(payload["cwd"])

        elif kind == "event_msg" and payload.get("type") == "user_message":
            text = clean_prompt(payload.get("message") if isinstance(payload.get("message"), str) else "")
            if text:
                current = Turn(parse_time(record.get("timestamp")), text)
                transcript.turns.append(current)

        elif kind == "event_msg" and payload.get("type") == "patch_apply_end":
            if not payload.get("success") or current is None:
                continue
            changes = payload.get("changes")
            if not isinstance(changes, dict):
                continue
            confirmed_edits = True
            for raw_path, change in changes.items():
                for candidate in (raw_path, (change or {}).get("move_path") if isinstance(change, dict) else None):
                    rel = relative_to_repo(repo, candidate or "", session_cwd)
                    if rel:
                        current.add_file(rel)

        elif kind == "response_item" and current is not None:
            if payload.get("type") not in ("custom_tool_call", "function_call", "local_shell_call"):
                continue
            blob = json.dumps(payload.get("input") or payload.get("arguments") or payload.get("action") or "")
            raw_input = payload.get("input") or payload.get("arguments") or ""
            current.shell.append(raw_input if isinstance(raw_input, str) else blob)
            for raw_path in PATCH_FILE.findall(blob):
                rel = relative_to_repo(repo, raw_path.strip(), session_cwd)
                if rel:
                    attempts.append((current, rel))

    if not confirmed_edits and attempts:
        for turn, rel in attempts:
            turn.add_file(rel)
        transcript.notes.append("nessun evento patch_apply_end: file ricavati dalle chiamate apply_patch (tentativi, esito non confermato)")
    return transcript


def other_sessions(agent: str, repo: Path, main: Path, since: datetime) -> list[Path]:
    """Other transcripts of the same agent, for this repo, written to after `since`.

    The user may open a new chat in the middle of a stretch: its prompts belong
    to the stretch too.
    """
    cutoff = since.timestamp()
    if agent == "claude":
        return [f for f in main.parent.glob("*.jsonl") if f != main and f.stat().st_mtime > cutoff]
    found = []
    sessions = main
    while sessions.name != "sessions" and sessions != sessions.parent:
        sessions = sessions.parent
    for rollout in sessions.rglob("rollout-*.jsonl"):
        if rollout == main or rollout.stat().st_mtime <= cutoff:
            continue
        for record in iter_jsonl(rollout):
            cwd = (record.get("payload") or {}).get("cwd") if record.get("type") == "session_meta" else None
            if isinstance(cwd, str) and relative_to_repo(repo, cwd) is not None:
                found.append(rollout)
            break
    return found


def locate_transcript(agent: str, repo: Path, cwd: Path, session_id: str, override: str) -> Path | None:
    if override:
        path = Path(override).expanduser()
    elif agent == "claude":
        path = find_claude_transcript(repo, cwd, session_id)
    else:
        path = find_codex_rollout(repo, session_id or os.environ.get("CODEX_THREAD_ID", ""))
    return path if path is not None and path.is_file() else None


def load_transcript(agent: str, repo: Path, cwd: Path, session_id: str, override: str) -> Transcript | None:
    path = locate_transcript(agent, repo, cwd, session_id, override)
    if path is None:
        return None
    return parse_claude(path, repo) if agent == "claude" else parse_codex(path, repo)


def trace_transcript(agent: str, path: Path, repo: Path, rows: int) -> list[str]:
    """Diagnostic view of how the transcript is read: one line per relevant
    record, structure only. Prompts are masked and cut, tool outputs never shown."""

    def stamp(record: dict) -> str:
        when = parse_time(record.get("timestamp"))
        return when.astimezone().strftime("%H:%M:%S") if when else "--:--:--"

    def short(text: str) -> str:
        return truncate(re.sub(r"\s+", " ", mask_secrets(text or "")).strip(), 60)

    out: list[str] = []
    if agent == "claude":
        for record in iter_jsonl(path):
            kind = record.get("type")
            if kind not in ("user", "assistant"):
                continue
            content = (record.get("message") or {}).get("content")
            origin = record.get("origin")
            flags = "origin=" + (
                str(origin.get("kind")) if isinstance(origin, dict) else ("assente" if "origin" not in record else "null")
            )
            flags += "".join(f" {key}" for key in ("isMeta", "isSidechain", "isCompactSummary") if record.get(key))
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if kind == "user" and btype == "text":
                    cleaned = clean_prompt(block.get("text") or "")
                    out.append(f"{stamp(record)} user [{flags}] testo: {short(cleaned) or '(vuoto dopo la pulizia)'}")
                elif kind == "user" and btype == "tool_result":
                    tool_id = str(block.get("tool_use_id") or "")[-6:]
                    out.append(f"{stamp(record)} user [{flags}] tool_result id=…{tool_id} is_error={bool(block.get('is_error'))}")
                elif kind == "assistant" and btype == "tool_use":
                    tool_input = block.get("input") or {}
                    raw_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
                    detail = f"path={raw_path}" if raw_path else ""
                    if not raw_path and isinstance(tool_input.get("command"), str):
                        detail = f"comando: {short(tool_input['command'])}"
                    counted = "SCRITTURA" if block.get("name") in CLAUDE_WRITE_TOOLS else "ignorato"
                    if block.get("name") in SHELL_TOOLS and SHELL_WRITE.search(str(tool_input.get("command") or "")):
                        counted = "shell, scrittura possibile"
                    tool_id = str(block.get("id") or "")[-6:]
                    out.append(f"{stamp(record)} assistant tool_use {block.get('name')} id=…{tool_id} {detail} → {counted}")
    else:
        needles = ('"user_message"', '"patch_apply_end"', '"custom_tool_call"', '"function_call"', '"session_meta"')
        for record in iter_jsonl(path, needles):
            payload = record.get("payload") or {}
            ptype = payload.get("type") if isinstance(payload, dict) else None
            if record.get("type") == "session_meta":
                out.append(f"{stamp(record)} session_meta id={payload.get('id')} cwd={payload.get('cwd')}")
            elif ptype == "user_message":
                cleaned = clean_prompt(payload.get("message") if isinstance(payload.get("message"), str) else "")
                out.append(f"{stamp(record)} user_message testo: {short(cleaned) or '(vuoto dopo la pulizia)'}")
            elif ptype == "patch_apply_end":
                changes = payload.get("changes") if isinstance(payload.get("changes"), dict) else {}
                out.append(f"{stamp(record)} patch_apply_end success={bool(payload.get('success'))} file={', '.join(changes) or '-'}")
            elif ptype in ("custom_tool_call", "function_call"):
                out.append(f"{stamp(record)} {ptype} name={payload.get('name')}")
    hidden = max(0, len(out) - rows)
    return ([f"… {hidden} righe precedenti omesse"] if hidden else []) + out[-rows:]


HANDOFF_REQUEST = re.compile(
    r"(?i)context-for-agents|handoff|pass(?:o|a|are|iamo) a (?:codex|claude)|(?:prepara|aggiorna)(?: il)? contesto"
)


def select_turns(turns: list[Turn], since: datetime | None) -> list[Turn]:
    """Keep the turns after the previous handoff."""
    selected = [t for t in turns if since is None or t.time is None or t.time > since]
    # The same prompt can be recorded twice in a row: keep the copy that did the work.
    merged: list[Turn] = []
    for turn in selected:
        if merged and merged[-1].prompt == turn.prompt and not merged[-1].files and not merged[-1].shell:
            merged[-1] = turn
        else:
            merged.append(turn)
    return merged


def drop_handoff_requests(turns: list[Turn]) -> list[Turn]:
    """A prompt that only asked for a handoff is noise. One that also changed files stays."""
    return [t for t in turns if t.files or t.shell_files or not HANDOFF_REQUEST.search(t.prompt)]


# --------------------------------------------------------------------------
# Git snapshot and diff
# --------------------------------------------------------------------------


def take_snapshot(repo: Path, git_dir: Path) -> str:
    """Store the working tree as a tree object, without commits and without
    touching the user's index. context/ and .env files are left out."""
    env = {**os.environ, "GIT_INDEX_FILE": str(git_dir / INDEX_NAME)}
    git(
        repo, "add", "-A", "--", ".",
        ":(exclude,glob)**/.env", ":(exclude,glob)**/.env.*", ":(exclude,glob)**/.DS_Store",
        env=env,
    )
    # context/ is normally git-ignored; when it is not, drop it from the private index.
    # (An exclude pathspec cannot be used here: git rejects one that names an ignored path.)
    git(repo, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", CONTEXT_DIR, env=env)
    return git(repo, "write-tree", env=env).strip()


def resolve_tree(repo: Path, ref: str) -> str | None:
    res = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", f"{ref}^{{tree}}"], cwd=repo, capture_output=True, text=True
    )
    return res.stdout.strip() if res.returncode == 0 and res.stdout.strip() else None


def empty_tree(repo: Path) -> str:
    res = subprocess.run(
        ["git", "hash-object", "-t", "tree", "--stdin"], cwd=repo, input="", capture_output=True, text=True
    )
    return res.stdout.strip()


@dataclass
class Change:
    status: str
    path: str
    added: str
    deleted: str

    def render(self) -> str:
        counts = "binario" if self.added == "-" else f"+{self.added} −{self.deleted}"
        return f"- {self.status} `{self.path}` ({counts})"


def diff_trees(repo: Path, base: str, new: str) -> list[Change]:
    if base == new:
        return []
    statuses: dict[str, str] = {}
    tokens = git(repo, "diff", "--no-renames", "--name-status", "-z", base, new).split("\0")
    for status, path in zip(tokens[0::2], tokens[1::2]):
        statuses[path] = status
    changes = []
    for entry in git(repo, "diff", "--no-renames", "--numstat", "-z", base, new).split("\0"):
        parts = entry.split("\t", 2)
        if len(parts) == 3:
            added, deleted, path = parts
            changes.append(Change(statuses.get(path, "M"), path, added, deleted))
    return changes


# --------------------------------------------------------------------------
# Spec
# --------------------------------------------------------------------------

STATO = re.compile(r"^\*\*Stato\*\*\s*:\s*(.+)$", re.M)


def spec_state(path: Path) -> str:
    try:
        match = STATO.search(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""
    return match.group(1).strip() if match else ""


def in_progress_specs(repo: Path) -> list[Path]:
    folder = repo / "spec"
    if not folder.is_dir():
        return []
    return [p for p in sorted(folder.glob("*.md")) if spec_state(p).lower().startswith("in-progress")]


def file_map_entries(spec: Path) -> list[str]:
    """Paths listed in the first column of the spec's 'Mappa dei file' table."""
    entries: list[str] = []
    inside = False
    for line in spec.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("## "):
            inside = "mappa dei file" in line.lower()
            continue
        if not inside or not line.lstrip().startswith("|"):
            continue
        cell = line.strip().strip("|").split("|")[0].strip()
        if not cell or set(cell) <= set("-: ") or cell.lower() == "file":
            continue
        quoted = re.findall(r"`([^`]+)`", cell)
        for item in quoted or [cell]:
            entries.append(item.strip().lstrip("./"))
    return entries


def in_file_map(path: str, entries: list[str]) -> bool:
    for entry in entries:
        folder = entry.rstrip("/")
        if path == entry or path.startswith(folder + "/") or fnmatch.fnmatch(path, entry):
            return True
    return False


# --------------------------------------------------------------------------
# Previous CONTEXT.md
# --------------------------------------------------------------------------

HEADER = re.compile(r"<!--\s*" + re.escape(HEADER_TAG) + r"\s*\|(.*?)-->", re.S)


@dataclass
class Previous:
    meta: dict[str, str]
    sections: dict[str, list[str]]

    @property
    def time(self) -> datetime | None:
        return parse_time(self.meta.get("time", ""))


def parse_context(text: str) -> Previous | None:
    match = HEADER.search(text)
    if not match:
        return None
    meta = {}
    for item in match.group(1).split("|"):
        key, _, value = item.strip().partition("=")
        if key:
            meta[key.strip()] = value.strip()
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            title = line[3:].strip()
            for known in INHERITED_SECTIONS:
                if title.startswith(known):
                    title = known
            current = sections.setdefault(title, [])
        elif current is not None:
            current.append(line)
    return Previous(meta, sections)


def inherited_lines(previous: Previous, section: str) -> list[str]:
    lines = [line.rstrip() for line in previous.sections.get(section, [])]
    return [line for line in lines if line.strip() and PLACEHOLDER not in line]


def load_previous(context_path: Path, backup_path: Path) -> tuple[Previous | None, str]:
    """Return the last completed CONTEXT.md and a note on where it came from.

    A CONTEXT.md still holding placeholders is an aborted run: it is ignored
    and the backup of the last completed file is used instead.
    """
    if context_path.is_file():
        text = context_path.read_text(encoding="utf-8", errors="replace")
        if PLACEHOLDER not in text:
            previous = parse_context(text)
            backup_path.write_text(text, encoding="utf-8")
            if previous is None:
                return None, "il file esistente non era stato scritto da questa skill: sovrascritto (copia in .git)"
            return previous, ""
        note = "CONTEXT.md esistente incompleto (run interrotto): ignorato"
    else:
        note = ""
    if backup_path.is_file() and note:
        previous = parse_context(backup_path.read_text(encoding="utf-8", errors="replace"))
        if previous is not None:
            return previous, note + ", uso l'ultimo completo"
    return None, note


def is_same_work(previous: Previous, spec_rel: str, now: datetime, args) -> tuple[bool, str]:
    if args.new_work:
        return False, "richiesto con --new-work"
    if args.same_work:
        return True, "richiesto con --same-work"
    old_spec = previous.meta.get("spec", "")
    if old_spec != spec_rel:
        return False, f"spec diversa (prima: {old_spec or 'nessuna'})"
    if not spec_rel:
        old_time = previous.time
        if old_time is None or now - old_time > NO_SPEC_MAX_AGE:
            return False, "senza spec e più vecchio di 24 ore"
    return True, "stessa spec" if spec_rel else "senza spec, entro 24 ore"


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def format_when(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "--:--"
    local = moment.astimezone()
    return local.strftime("%H:%M") if local.date() == now.date() else local.strftime("%d/%m %H:%M")


def render_timeline(
    transcript: Transcript | None,
    turns: list[Turn],
    changes: list[Change],
    now: datetime,
    args,
) -> tuple[list[str], str]:
    title = f"## {SEC_TIMELINE}"
    if transcript is None:
        return [
            title,
            "_Non disponibile: transcript della sessione non trovato o non leggibile._",
        ], "non disponibile"

    visible = [t for t in turns if t.files or t.shell_files or len(t.prompt) >= MIN_PROMPT_CHARS]
    short = len(turns) - len(visible)
    older = max(0, len(visible) - args.max_turns)
    visible = visible[-args.max_turns :] if args.max_turns > 0 else visible

    count = f"{len(visible)} turno" if len(visible) == 1 else f"{len(visible)} turni"
    lines = [f"{title} (tratto di {AGENT_LABELS[args.agent]}, {count})"]
    if older:
        lines.append(f"- … {older} turni precedenti omessi")
    for turn in visible:
        labels = [f"`{p}`" for p in turn.files] + [f"`{p}` (da shell)" for p in turn.shell_files]
        if labels:
            extra = len(labels) - MAX_FILES_PER_TURN
            files = ", ".join(labels[:MAX_FILES_PER_TURN]) + (f", +{extra} altri" if extra > 0 else "")
        else:
            files = "nessun file"
        lines.append(f"- {format_when(turn.time, now)} · {truncate(turn.prompt, args.prompt_chars)} → {files}")
    if not visible:
        lines.append("- nessun prompt in questo tratto")

    if any(turn.shell_files for turn in visible):
        lines.append("")
        lines.append(
            "«da shell»: file cambiato nel tratto e nominato in un comando di scrittura di quel turno."
            " È una deduzione, non una conferma."
        )
    by_tools = {path for turn in turns for path in turn.files + turn.shell_files}
    outside = [c.path for c in changes if c.path not in by_tools]
    if outside:
        shown = ", ".join(f"`{p}`" for p in outside[:MAX_CHANGED_FILES])
        extra = len(outside) - MAX_CHANGED_FILES
        lines.append("")
        lines.append(
            "Modificati nel tratto e non collegati a nessun prompt (a mano, script, altro agente): "
            + shown
            + (f", +{extra} altri" if extra > 0 else "")
        )
    summary = count
    if older:
        summary += f", {older} più vecchi omessi"
    if short:
        summary += f", {short} senza modifiche e sotto i {MIN_PROMPT_CHARS} caratteri omessi"
    return lines, summary


def render_changes(changes: list[Change] | None, base_note: str, command: str) -> list[str]:
    lines = [f"## {SEC_CHANGES}"]
    if changes is None:
        return lines + ["_Non disponibile: la cartella non è una repo git._"]
    lines.append(base_note)
    if not changes:
        return lines + ["- nessun file modificato"]
    lines.append(f"Righe esatte: `{command}`")
    lines.extend(change.render() for change in changes[:MAX_CHANGED_FILES])
    if len(changes) > MAX_CHANGED_FILES:
        lines.append(f"- … +{len(changes) - MAX_CHANGED_FILES} altri file")
    return lines


def render_inherited(title: str, lines: list[str], hint: str) -> list[str]:
    return [f"## {title}", *lines, f"{PLACEHOLDER}: {hint}_"]


def build_document(
    *,
    args,
    now: datetime,
    spec_rel: str,
    spec_status: str,
    snapshot: str,
    base: str,
    job_base: str,
    session_id: str,
    inherited: dict[str, list[str]],
    changes: list[Change] | None,
    base_note: str,
    off_map: list[str] | None,
    off_map_notes: dict[str, str],
    timeline: list[str],
) -> str:
    label = AGENT_LABELS[args.agent]
    stamp = now.strftime("%Y-%m-%d %H:%M")
    meta = " | ".join(
        [
            HEADER_TAG,
            f"agent={label}",
            f"time={now.isoformat(timespec='seconds')}",
            f"spec={spec_rel}",
            f"snapshot={snapshot}",
            f"base={base}",
            f"job={job_base}",
            f"session={session_id}",
        ]
    )
    out = [
        f"<!-- {meta} -->",
        "# CONTEXT: passaggio tra agenti",
        "",
        f"- **Da**: {label} · {stamp}",
        f"- **Spec**: `{spec_rel}` · Stato: {spec_status}" if spec_rel else "- **Spec**: nessuna (sessione senza spec)",
    ]
    if snapshot:
        out.append(f"- **Snapshot**: `{snapshot[:12]}` (stato dei file al momento del passaggio, senza commit)")
    out += [
        "",
        "> Per chi riceve: questo file descrive lo stato del lavoro al momento del passaggio.",
        f'> Prima di toccare un file elencato in "{SEC_CHANGES}", rileggilo dal disco: può essere',
        f'> diverso da come lo ricordi. Le "{SEC_INSTRUCTIONS}" valgono quanto la spec.',
        f'> Se l\'utente ti chiede altro, la sua richiesta viene prima della "{SEC_NEXT}".',
        "",
        f"## {SEC_NEXT}",
        f"{PLACEHOLDER}: una frase all'imperativo"
        + (", con lo step della spec a cui appartiene" if spec_rel else "")
        + " e le eventuali conferme attese dall'utente._",
        "",
        f"## {SEC_STOP}",
        f'{PLACEHOLDER}: file, funzione e cosa manca (max 3 righe), oppure "nessun lavoro a metà"._',
        "",
    ]
    if spec_rel:
        out += [
            f"## {SEC_STEPS}",
            f"{PLACEHOLDER}: tabella Step | Stato | Evidenza, una riga per ogni step di Esecuzione e ogni Criterio"
            " di accettazione. Stati: fatto, parziale, non iniziato, deviato, da verificare._",
            "",
        ]
    out += render_inherited(
        SEC_INSTRUCTIONS,
        inherited[SEC_INSTRUCTIONS],
        "aggiungi le istruzioni date dall'utente in chat in questo tratto (una riga ciascuna, con l'ora),"
        " poi elimina questa riga. Se la sezione resta vuota, elimina anche il titolo.",
    )
    out.append("")
    out += render_inherited(
        SEC_ATTEMPTS,
        inherited[SEC_ATTEMPTS],
        "aggiungi i tentativi scartati in questo tratto (cosa, perché, codice residuo e dove),"
        " poi elimina questa riga. Se la sezione resta vuota, elimina anche il titolo.",
    )
    out += [
        "",
        f"## {SEC_CHECKS}",
        f"{PLACEHOLDER}: comando, esito, se lanciato prima o dopo l'ultima modifica; fallimenti nuovi separati"
        ' da quelli preesistenti. Oppure "nessuna verifica eseguita"._',
        "",
    ]
    command = f"git diff {base[:12]} {snapshot[:12]}" if snapshot else ""
    out += render_changes(changes, base_note, command)
    out.append("")
    if off_map:
        out.append(f"## {SEC_OFF_MAP}")
        for path in off_map:
            out.append(off_map_notes.get(path) or f"- `{path}`: {PLACEHOLDER}: necessario per quale step, oppure accidentale_")
        out.append("")
    out += timeline
    out += [
        "",
        f"## {SEC_HISTORY}",
        *inherited[SEC_HISTORY],
        f"- {stamp} · {label}: {PLACEHOLDER}: cosa è stato fatto in questo tratto, in poche parole_",
        "",
    ]
    return "\n".join(out)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def preliminary_gaps(repo: Path, is_git: bool) -> list[str]:
    gaps = []
    if not is_git:
        gaps.append("la cartella non è una repo git (niente snapshot né elenco delle modifiche)")
    if is_git and not git_ok(repo, "check-ignore", "-q", f"{CONTEXT_DIR}/{CONTEXT_FILE}"):
        gaps.append(f"{CONTEXT_DIR}/ non è in .gitignore")
    agents = repo / "AGENTS.md"
    if not agents.is_file():
        gaps.append("AGENTS.md non esiste")
    elif f"{CONTEXT_DIR}/{CONTEXT_FILE}" not in agents.read_text(encoding="utf-8", errors="replace"):
        gaps.append(f"AGENTS.md non cita {CONTEXT_DIR}/{CONTEXT_FILE}")
    return gaps


def run_check(repo: Path, state_dir: Path) -> int:
    context_path = repo / CONTEXT_DIR / CONTEXT_FILE
    if not context_path.is_file():
        print(f"ERRORE: {CONTEXT_DIR}/{CONTEXT_FILE} non esiste")
        return 1
    text = context_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    problems = []

    left = [i + 1 for i, line in enumerate(lines) if PLACEHOLDER in line]
    if left:
        problems.append(f"segnaposto ancora da compilare alle righe {', '.join(map(str, left))}")
    stray = [
        i + 1
        for i, line in enumerate(lines)
        if re.search(r"[.\"”»)]_\s*$", line) and not line.lstrip().startswith(("_", ">", "- _"))
    ]
    if stray:
        problems.append(f"resto di segnaposto (carattere _ a fine riga) alle righe {', '.join(map(str, stray))}")
    if parse_context(text) is None:
        problems.append("la prima riga con i metadati è stata rimossa o modificata")
    titles = [line[3:].strip() for line in lines if line.startswith("## ")]
    for section in REQUIRED_SECTIONS:
        if not any(title.startswith(section) for title in titles):
            problems.append(f'manca la sezione obbligatoria "{section}"')

    state_path = state_dir / STATE_NAME
    if state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        emitted = state.get("sections") or []
        for title in titles:
            if emitted and not any(title.startswith(name) for name in emitted):
                problems.append(f'sezione non prevista "{title}": lo schema è fisso, sposta il contenuto in una sezione esistente')
        for section, inherited in (state.get("inherited") or {}).items():
            for line in inherited:
                core = line.strip().lstrip("-").strip()
                if core and not any(core in candidate.replace("~~", "") for candidate in lines):
                    problems.append(f'riga ereditata modificata o rimossa in "{section}": {truncate(core, 80)}')

    others = [p.name for p in (repo / CONTEXT_DIR).iterdir() if p.name != CONTEXT_FILE]
    if others:
        problems.append(f"in {CONTEXT_DIR}/ ci sono altri file: {', '.join(sorted(others))}")

    if problems:
        print("CONTROLLO NON SUPERATO")
        for problem in problems:
            print(f"- {problem}")
        return 1
    print(f"CONTROLLO SUPERATO: {CONTEXT_DIR}/{CONTEXT_FILE} completo ({len(lines)} righe)")
    return 0


def run_build(args, repo: Path, cwd: Path, is_git: bool, state_dir: Path) -> int:
    now = datetime.now().astimezone()
    context_dir = repo / CONTEXT_DIR
    context_path = context_dir / CONTEXT_FILE
    report: list[str] = []

    # Previous handoff.
    context_dir.mkdir(exist_ok=True)
    previous, previous_note = load_previous(context_path, state_dir / BACKUP_NAME)
    if previous_note:
        report.append(f"Nota: {previous_note}")

    # Spec of the job in progress.
    spec_rel, spec_status = "", ""
    if args.spec:
        spec_path = (cwd / args.spec).resolve() if not Path(args.spec).is_absolute() else Path(args.spec)
        if not spec_path.is_file():
            print(f"ERRORE: spec non trovata: {args.spec}")
            return 1
        spec_rel = relative_to_repo(repo, str(spec_path)) or args.spec
        spec_status = spec_state(spec_path) or "stato non indicato"
    elif not args.no_spec:
        candidates = in_progress_specs(repo)
        if len(candidates) > 1:
            print("SERVE UNA SCELTA: più spec in-progress. Chiedi all'utente e rilancia con --spec <percorso>:")
            for candidate in candidates:
                print(f"- {candidate.relative_to(repo).as_posix()}")
            return 2
        if candidates:
            spec_path = candidates[0]
            spec_rel = spec_path.relative_to(repo).as_posix()
            spec_status = spec_state(spec_path)
        elif previous is not None and not args.new_work:
            # The spec of the previous handoff may have been closed in this stretch:
            # it is still the spec of this job.
            carried = previous.meta.get("spec", "")
            if carried and (repo / carried).is_file():
                spec_rel = carried
                spec_status = spec_state(repo / carried) or "stato non indicato"
                report.append("Nota: nessuna spec in-progress, resta quella del passaggio precedente")

    same = False
    if previous is not None:
        same, reason = is_same_work(previous, spec_rel, now, args)
        old_agent = previous.meta.get("agent", "?")
        old_when = format_when(previous.time, now)
        report.append(
            f"CONTEXT.md precedente: {old_agent}, {old_when} → "
            + ("stesso lavoro" if same else "lavoro diverso: sezioni non ereditate, resta il confine del tratto")
            + f" ({reason})"
        )
    else:
        report.append("CONTEXT.md precedente: nessuno (primo passaggio)")
    inherited = {
        section: (inherited_lines(previous, section) if same and previous else []) for section in INHERITED_SECTIONS
    }
    if same:
        report.append(
            "Ereditate: "
            + ", ".join(f"{len(inherited[s])} righe in «{s}»" for s in INHERITED_SECTIONS)
        )

    # Snapshot and changes of this stretch.
    snapshot, base, base_note, job_base = "", "", "", ""
    changes: list[Change] | None = None
    session_changes: list[Change] = []
    if is_git:
        snapshot = take_snapshot(repo, state_dir)
        head = resolve_tree(repo, "HEAD")
        old_snapshot = previous.meta.get("snapshot", "") if previous else ""
        old_tree = resolve_tree(repo, old_snapshot) if old_snapshot else None
        # A snapshot of another job is still the tightest boundary, unless a commit came after it.
        if old_tree and not same and head:
            committed = parse_time(git(repo, "log", "-1", "--format=%cI", "HEAD", check=False).strip())
            if previous.time is None or committed is None or committed >= previous.time:
                old_tree = None
        if old_tree:
            base = old_tree
            base_note = (
                f"Rispetto allo snapshot del passaggio precedente `{base[:12]}`"
                f" ({previous.meta.get('agent', '?')}, {format_when(previous.time, now)}"
                + ("" if same else ", altro lavoro")
                + ")."
            )
        elif head:
            base = head
            base_note = "Rispetto all'ultimo commit (nessuno snapshot precedente utilizzabile)."
        else:
            base = empty_tree(repo)
            base_note = "Rispetto a una repo vuota (nessun commit e nessuno snapshot precedente)."
        changes = diff_trees(repo, base, snapshot)
        # Start of the whole job: fixed at its first handoff and carried along.
        job_base = (resolve_tree(repo, previous.meta.get("job", "")) if same and previous.meta.get("job") else None) or base
        session_changes = diff_trees(repo, job_base, snapshot)
        report.append(f"Snapshot: {snapshot[:12]} · {len(changes)} file modificati nel tratto")
    else:
        report.append("Snapshot: non disponibile (non è una repo git)")

    # Files changed in the whole job that the spec's file map does not list.
    off_map: list[str] | None = None
    if spec_rel:
        entries = file_map_entries(repo / spec_rel)
        if entries:
            off_map = [c.path for c in session_changes if c.path != spec_rel and not in_file_map(c.path, entries)]
        report.append(
            f"Spec: {spec_rel} · {spec_status}"
            + (f" · {len(off_map)} file fuori mappa" if off_map is not None else " · Mappa dei file non trovata")
        )
    else:
        report.append("Spec: nessuna in-progress → forma ridotta")

    off_map_notes: dict[str, str] = {}
    if same and previous:
        for line in inherited_lines(previous, SEC_OFF_MAP):
            noted = re.match(r"- `([^`]+)`", line)
            if noted:
                off_map_notes[noted.group(1)] = line

    # Chronology from the session transcript.
    transcript = None
    try:
        transcript = load_transcript(args.agent, repo, cwd, args.session, args.transcript)
    except OSError as error:
        report.append(f"Transcript: errore di lettura ({error})")
    turns: list[Turn] = []
    if transcript is not None:
        since = previous.time if previous else None
        all_turns = list(transcript.turns)
        if since is not None and not args.transcript:
            parse = parse_claude if args.agent == "claude" else parse_codex
            extra = [parse(path, repo) for path in other_sessions(args.agent, repo, transcript.path, since)]
            extra = [t for t in extra if any(turn.time and turn.time > since for turn in t.turns)]
            for other in extra:
                all_turns.extend(other.turns)
            if extra:
                all_turns.sort(key=lambda turn: turn.time.timestamp() if turn.time else float("inf"))
                report.append(f"Altre sessioni di {AGENT_LABELS[args.agent]} in questo tratto: {len(extra)} (incluse nella Cronologia)")
        turns = select_turns(all_turns, since)
        attribute_shell_edits(turns, [change.path for change in changes or []])
        turns = drop_handoff_requests(turns)
        first = transcript.turns[0].prompt if transcript.turns else ""
        report.append(
            f"Transcript: sessione {transcript.session_id[:8] or '?'} · {transcript.records} record"
            + (f" · versione {transcript.version}" if transcript.version else "")
        )
        report.append(f'Primo prompt della sessione: "{truncate(first, 80)}"')
        if not transcript.turns:
            report.append("ATTENZIONE: nessun prompt riconosciuto, il formato del transcript può essere cambiato")
        report.extend(f"Nota transcript: {note}" for note in transcript.notes)
    else:
        report.append("Transcript: NON TROVATO → Cronologia non disponibile, ricostruisci le istruzioni dal contesto")
    timeline, timeline_summary = render_timeline(transcript, turns, changes or [], now, args)
    report.append(f"Cronologia: {timeline_summary}")

    document = build_document(
        args=args,
        now=now,
        spec_rel=spec_rel,
        spec_status=spec_status,
        snapshot=snapshot,
        base=base,
        job_base=job_base,
        session_id=transcript.session_id if transcript else "",
        inherited=inherited,
        changes=changes,
        base_note=base_note,
        off_map=off_map,
        off_map_notes=off_map_notes,
        timeline=timeline,
    )
    context_path.write_text(document, encoding="utf-8")
    (state_dir / STATE_NAME).write_text(
        json.dumps(
            {
                "written": now.isoformat(timespec="seconds"),
                "inherited": inherited,
                "sections": [line[3:].split(" (")[0].strip() for line in document.splitlines() if line.startswith("## ")],
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )

    print(f"Scheletro scritto in {CONTEXT_DIR}/{CONTEXT_FILE} ({AGENT_LABELS[args.agent]})")
    for line in report:
        print(line)
    print(f"Da compilare: {document.count(PLACEHOLDER)} segnaposto, poi lancia lo script con --check")
    others = [p.name for p in context_dir.iterdir() if p.name != CONTEXT_FILE]
    if others:
        print(f"ATTENZIONE: in {CONTEXT_DIR}/ ci sono altri file: {', '.join(sorted(others))}")
    gaps = preliminary_gaps(repo, is_git)
    if gaps:
        print("AZIONI PRELIMINARI MANCANTI (apri actions_before/ACTIONS.md): " + "; ".join(gaps))
    return 0


def detect_agent() -> str:
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    if os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SANDBOX"):
        return "codex"
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Build or check context/CONTEXT.md for an agent handoff.")
    parser.add_argument("--agent", choices=sorted(AGENT_LABELS), help="the outgoing agent (the one running this)")
    parser.add_argument("--check", action="store_true", help="validate the CONTEXT.md filled by the agent")
    parser.add_argument("--repo", default="", help="repository root (default: git root of the current directory)")
    parser.add_argument("--spec", default="", help="spec of the job in progress, when more than one is in-progress")
    parser.add_argument("--no-spec", action="store_true", help="ignore spec/ and use the reduced form")
    parser.add_argument("--session", default="", help="session id of the outgoing agent, when known")
    parser.add_argument("--transcript", default="", help="explicit path of the session transcript")
    parser.add_argument("--new-work", action="store_true", help="do not inherit from the existing CONTEXT.md")
    parser.add_argument("--same-work", action="store_true", help="inherit from the existing CONTEXT.md")
    parser.add_argument("--trace", action="store_true", help="show how the transcript is read; writes nothing")
    parser.add_argument("--trace-rows", type=int, default=80, help="rows shown by --trace")
    parser.add_argument("--prompt-chars", type=int, default=250, help="maximum characters per prompt")
    parser.add_argument("--max-turns", type=int, default=30, help="maximum turns in the chronology")
    args = parser.parse_args()
    # An unsubstituted "${CLAUDE_SESSION_ID}" reaches us as an empty string.
    args.session = args.session.strip()

    cwd = Path(args.repo).expanduser().resolve() if args.repo else Path.cwd().resolve()
    is_git = git_ok(cwd, "rev-parse", "--show-toplevel")
    if is_git:
        repo = Path(git(cwd, "rev-parse", "--show-toplevel").strip()).resolve()
        state_dir = Path(git(repo, "rev-parse", "--absolute-git-dir").strip())
    else:
        repo = cwd
        # Without git there is no hidden place for the state: keep it out of context/.
        digest = hashlib.sha1(str(repo).encode()).hexdigest()[:12]
        state_dir = Path(tempfile.gettempdir()) / f"context-for-agents-{digest}"
        state_dir.mkdir(parents=True, exist_ok=True)

    if args.check:
        return run_check(repo, state_dir)

    args.agent = args.agent or detect_agent()
    if not args.agent:
        print("ERRORE: indica l'agente uscente con --agent claude oppure --agent codex")
        return 1
    if args.trace:
        path = locate_transcript(args.agent, repo, cwd, args.session, args.transcript)
        if path is None:
            print("Transcript non trovato")
            return 1
        print(f"Transcript: {path.name}")
        print("\n".join(trace_transcript(args.agent, path, repo, args.trace_rows)))
        return 0
    try:
        return run_build(args, repo, cwd, is_git, state_dir)
    except GitError as error:
        print(f"ERRORE git: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
