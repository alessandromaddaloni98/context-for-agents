# Actions before the first handoff in a repo

Open this file only when `build_context.py` prints `AZIONI PRELIMINARI MANCANTI`. Each project needs these actions once. Do only the ones the script reported as missing, then go back to the flow in SKILL.md. CONTEXT.md is already written: rerun the script only after action 0.

## 0. Make the folder a git repo

Reported as: `la cartella non è una repo git`

Without git the script cannot take the snapshot, so CONTEXT.md has no list of changed files and the incoming agent cannot see what the other agent overwrote.

1. **Ask the user for confirmation** before creating a repo.
2. On a yes, run `git init` at the project root. No commit is needed, and never make one yourself.
3. Rerun `build_context.py`: this time it takes the snapshot. Then do the actions below if still reported.

On a no, go on without the snapshot and say so in your final line.

## 1. Keep `context/` out of git

Reported as: `context/ non è in .gitignore`

CONTEXT.md contains the user's prompts and is temporary state: it must never be committed.

1. Look at what is inside `context/`. If it holds anything other than `CONTEXT.md`, the repo already uses that folder for something else: **stop and tell the user**, do not ignore it.
2. Otherwise append this line to `.gitignore` at the repo root (create the file if missing):

   ```
   context/
   ```

## 2. Point AGENTS.md to CONTEXT.md

Reported as: `AGENTS.md non cita context/CONTEXT.md` or `AGENTS.md non esiste`

A fresh session reads the project instructions at start. This line makes it pick up the handoff without being told.

Codex always reads `AGENTS.md`. Claude Code (v2.1.277 or later) reads it by default only when the project has no `CLAUDE.md`, `.claude/CLAUDE.md` or `CLAUDE.local.md`. If one of those exists and does not already contain `@AGENTS.md`, tell the user: the line reaches Claude Code only if `CLAUDE.md` imports `AGENTS.md`. Do not edit `CLAUDE.md` yourself.

- If `AGENTS.md` exists: show the user the line below and **ask for confirmation** before adding it. This is the only change this skill may make to AGENTS.md, and it is an addition: do not touch any other line.

  ```
  - Se esiste `context/CONTEXT.md` e riguarda il lavoro corrente, leggilo prima di iniziare.
  ```

- If `AGENTS.md` does not exist: do not create it. Tell the user it is missing and that, without it, the incoming agent must be told `leggi context/CONTEXT.md e procedi`.

## After

Report in one line which actions you did and which you left to the user.
