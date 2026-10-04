---
name: context-for-agents
description: Writes context/CONTEXT.md, the handoff file that lets another coding agent (Codex or Claude Code) continue an uncommitted job in the same repo. Use only when the user explicitly asks to hand the work over, e.g. "passo a Codex", "passo a Claude", "prepara il contesto", "aggiorna CONTEXT.md", "fai l'handoff", or names the skill. Not for reading CONTEXT.md as the incoming agent, and not for writing or closing a spec (that is spec-ale and spec-do).
---

# context-for-agents

## Why this exists

The user alternates Claude Code and Codex on the same job, in the same repo, without committing in between. The incoming agent finds a modified working tree with no explanation. This skill writes the explanation: `context/CONTEXT.md`, one file, overwritten at every handoff.

## Guiding principle

> CONTEXT.md holds only what the incoming agent cannot get from the spec and from `git diff`.

That is: what is half done, what the user said in chat, what was tried and dropped, what was verified. Never the diff, never a description of the code, never a copy of the spec.

## Flow

1. **Run the script** from the repo, as the outgoing agent:

   ```bash
   python3 <this skill's directory>/scripts/build_context.py --agent <claude|codex> --session "${CLAUDE_SESSION_ID}"
   ```

   `<this skill's directory>` is the folder that contains this SKILL.md. Pass `--agent claude` if you are Claude Code, `--agent codex` if you are Codex. Leave the `--session` argument as written: it is filled in automatically where supported and is empty elsewhere.

2. **Read the script's report** and act on it:
   - `SERVE UNA SCELTA` (more than one spec in-progress): ask the user which one, then rerun with `--spec <path>`.
   - `Primo prompt della sessione`: if it is not the first prompt of this conversation, the wrong transcript was picked. Tell the user and stop.
   - `Transcript: NON TROVATO`: go on. The Cronologia is missing, so take the user's instructions from your own context.
   - `CONTEXT.md precedente ... lavoro diverso` when the user is clearly continuing the same job (or the opposite): rerun with `--same-work` (or `--new-work`).
   - `AZIONI PRELIMINARI MANCANTI`: open `actions_before/ACTIONS.md` in this skill's directory and follow it.

3. **Open `context/CONTEXT.md` and fill every `_DA COMPILARE_` placeholder**, following the rules below. Edit the file in place; do not rewrite it from scratch. Replace each placeholder whole, from the opening underscore to the closing one.

4. **Run the check** and fix what it reports, until it passes:

   ```bash
   python3 <this skill's directory>/scripts/build_context.py --check
   ```

5. **Reply with one line**: the file path and the sentence to give the other agent, `leggi context/CONTEXT.md e procedi`. No summary of the content.

## Who writes what

| Section | Written by | Your job |
|---|---|---|
| First line (metadata), header, Modifiche di questo tratto, Cronologia | script | Leave untouched |
| Prossima azione, Punto di arresto, Stato degli step, Verifiche eseguite | you | Fill |
| Istruzioni fuori spec, Tentativi scartati, Storico passaggi | script copies the old lines, you add the new ones | Append only |
| File fuori mappa | script lists the files, you annotate each | Annotate |

## Rules for the sections you fill

- **Prossima azione**: one imperative sentence. With a spec, name the step it belongs to and any confirmation the spec requires that the user has not given yet. Without a spec, do not mention one.
- **Punto di arresto**: file, function, what is missing. Three lines at most. Always present: if nothing is half done, write `nessun lavoro a metà`.
- **Stato degli step** (only with a spec): a table `Step | Stato | Evidenza`, one row per step of Esecuzione and per Criterio di accettazione. States: `fatto`, `parziale`, `non iniziato`, `deviato`, `da verificare`. `fatto` needs evidence, either a file listed in the changes or a command you ran in this session. Without evidence, write `da verificare`. Never mark `fatto` from memory.
- **Istruzioni fuori spec**: what the user told you in chat that is not in the spec and still binds the work ("non usare quella libreria", "la validazione non va in routes"). One line each, with the time of the prompt taken from the Cronologia. Questions and one-off requests are not instructions.
- **Tentativi scartati**: one line each: what was tried, why it was dropped, whether code from it is still in the tree and where.
- **Verifiche eseguite**: command, outcome, and whether it ran before or after the last edit (a test that passed before the last edit proves nothing). New failures separate from pre-existing ones. Always present: `nessuna verifica eseguita` is information.
- **File fuori mappa**: for each file, either the step that needs it or `accidentale`.
- **Storico passaggi**: complete the last line with what this stretch did, in a few words.

A section that stays empty is removed with its title, except Punto di arresto and Verifiche eseguite. Keep what you write under 60 lines.

Do not add sections: the layout is fixed and the next run of the script reads it. Something worth telling that has no section of its own (an unexpected folder, a file that should not be there) goes in Punto di arresto.

## Inherited sections

Istruzioni fuori spec, Tentativi scartati and Storico passaggi carry lines from earlier handoffs. They were copied verbatim by the script so that nothing drifts after several round trips.

- Never reword, reorder or delete an inherited line.
- If an instruction no longer holds, strike it and say why: `- ~~<original line>~~ (superata: <reason>)`.
- Add your new lines below the inherited ones.

The check fails if an inherited line was changed.

## What not to do

- Do not open the session transcript yourself: it is huge and contains tool outputs. Only the script reads it.
- Do not load a full `git diff` into the context. The script already lists the changed files, and the incoming agent can run the diff command printed in CONTEXT.md.
- Do not edit the spec, and do not edit AGENTS.md except as `actions_before/ACTIONS.md` says.
- Do not run `git add`, `git commit` or `git push`. The script's snapshot uses a private index and leaves the user's staging and history untouched.
- Do not create any other file in `context/`: the folder holds CONTEXT.md and nothing else.
- Never read `.env` files and never write keys, tokens, connection strings or credentials into CONTEXT.md. The script masks what it finds in prompts; do the same in the lines you write.

## Language

Talk to the user in Italian. CONTEXT.md is written in Italian; file names, symbols and commands stay as in the repo.
