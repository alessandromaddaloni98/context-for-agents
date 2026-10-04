# context-for-agents

![Due agenti. Una memoria.](assets/cover.png)

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![Works with Claude Code and Codex](https://img.shields.io/badge/works%20with-Claude%20Code%20%C2%B7%20Codex-8A2BE2)

Una skill per **Claude Code** e **Codex** che passa da un agente all'altro un lavoro non finito e non ancora committato.

## Il problema

Inizi una modifica con un agente e a metà passi all'altro. Il secondo trova un working tree modificato senza alcuna spiegazione: `git diff` dice cosa è cambiato, non perché, cosa è rimasto a metà o cosa hai detto in chat.

Questa skill scrive quella parte in un unico file, `context/CONTEXT.md`, e l'altro agente riparte da lì.

## Quickstart

**1. Installa.** La skill vive nel progetto, non nella tua home. Copia la stessa struttura di cartelle nella radice del progetto in cui vuoi usarla:

```
your-project/
├── .claude/skills/context-for-agents/   ← letta da Claude Code
└── .agents/skills/context-for-agents/   ← letta da Codex
```

```bash
git clone https://github.com/<you>/context-for-agents
cd your-project
mkdir -p .claude/skills .agents/skills
cp -R ../context-for-agents/.claude/skills/context-for-agents .claude/skills/
cp -R ../context-for-agents/.agents/skills/context-for-agents .agents/skills/
```

Le due cartelle sono la stessa skill, duplicata perché ogni agente cerca nella propria cartella. Quando ne aggiorni una, tienile identiche. Riavvia entrambi gli agenti e controlla che la skill compaia nell'elenco.

**2. Nell'agente che esce**, nella stessa chat che ha fatto il lavoro:

```
passo a Codex, prepara il contesto
```

(oppure nomina la skill). Usa la stessa chat: i tentativi scartati e le verifiche esistono solo nel contesto di quell'agente.

**3. Nell'agente che entra:**

```
leggi context/CONTEXT.md e procedi
```

**Requisiti:** Python 3.9+ e git. Nessun'altra dipendenza.

### La prima volta in un progetto

La skill prepara il progetto una sola volta:

- `git init`, solo se la cartella non è una repo, e solo dopo averti chiesto conferma;
- `context/` aggiunto in coda a `.gitignore`, senza chiedere (si ferma e te lo dice se `context/` contiene già altro);
- una riga in `AGENTS.md` che rimanda a `context/CONTEXT.md`, solo dopo avertela mostrata. Se `AGENTS.md` non esiste la skill non lo crea: dì all'agente che entra `leggi context/CONTEXT.md e procedi`.

## Cosa finisce in CONTEXT.md

| Sezione | Scritta da | Cosa evita |
|---|---|---|
| Prossima azione, Punto di arresto | agente | Considerare finito un lavoro rimasto a metà |
| Stato degli step (quando c'è una spec) | agente | Rifare lavoro già fatto |
| Istruzioni fuori spec | agente (righe nuove), script (righe ereditate) | Violare una regola che hai detto solo a voce |
| Tentativi scartati | agente (righe nuove), script (righe ereditate) | Riprovare un approccio già fallito |
| Verifiche eseguite | agente | Fidarsi di codice che nessuno ha testato |
| Modifiche di questo tratto | script, da git | Tirare a indovinare cosa ha sovrascritto l'altro agente |
| File fuori mappa (quando c'è una spec) | lo script li elenca, l'agente annota ciascuno | Non accorgersi di lavoro uscito dal piano |
| Cronologia: i tuoi prompt e i file che ciascuno ha modificato | script, dal transcript della sessione | Perdere l'ordine degli eventi |
| Storico passaggi | script (righe ereditate), agente (ultima riga) | Perdere traccia di chi ha fatto cosa dopo più passaggi avanti e indietro |

Il file viene sovrascritto a ogni passaggio. Istruzioni, tentativi scartati e storico sono riportati alla lettera, così nulla si altera dopo più passaggi avanti e indietro. Senza una spec in `spec/`, il file usa una forma ridotta: niente stato degli step e niente file fuori mappa.

## Come funziona

- `scripts/build_context.py` fa la parte deterministica. Legge il transcript della sessione dell'agente che esce, fa uno snapshot del working tree, elenca cosa è cambiato dal passaggio precedente, copia le sezioni ereditate e scrive lo scheletro di CONTEXT.md.
- L'agente compila le sezioni che richiedono giudizio, poi lancia lo script con `--check`, che fallisce se è rimasto un segnaposto, se è stata aggiunta una sezione o se una riga ereditata è stata riscritta.
- Lo snapshot è un oggetto tree di git scritto tramite un index privato: nessun commit, niente in staging, la tua cronologia resta intatta.
- Con una spec in `spec/` il cui stato è `in-progress`, il file riporta anche lo stato di ogni step e i file modificati fuori dalla mappa dei file della spec.

## Privacy e cosa lascia nel tuo progetto

- `context/CONTEXT.md`
- in `.git/`, invisibili a `git status`: un index privato, l'ultimo CONTEXT.md completato e un piccolo file di stato
- oggetti git senza riferimenti per gli snapshot, che git rimuove da solo dopo circa due settimane

`.env`, `.env.*` e `.DS_Store` non entrano mai in uno snapshot. Gli output dei tool non vengono mai copiati, e tutto ciò che in un prompt somiglia a una chiave, un token o una stringa di connessione viene mascherato. CONTEXT.md contiene i tuoi prompt, ed è per questo che `context/` resta fuori da git.

## Limiti

- **CONTEXT.md è scritto in italiano.** I titoli delle sezioni e i messaggi dello script sono in italiano; le istruzioni della skill sono in inglese.
- **I formati dei transcript sono interni e possono cambiare.** Testato su Claude Code 2.1.288 e Codex CLI 0.160.0. Se una nuova versione cambia il formato, la cronologia esce vuota e lo script lo segnala; il resto del file viene comunque scritto. Le funzioni da aggiornare sono `parse_claude` e `parse_codex`.
- **Le modifiche fatte dalla shell sono dedotte.** Quando un agente cambia un file con `sed -i` o con uno script inline invece che con il suo tool di modifica, il file viene collegato al prompt solo se git conferma che è cambiato e un comando di scrittura di quel turno lo nomina. Quei file sono contrassegnati con `(da shell)`.
- **Serve una repo git** per l'elenco delle modifiche. Basta `git init`, non serve alcun commit.

## Debug

Per vedere come viene letto un transcript, senza scrivere nulla:

```bash
python3 .claude/skills/context-for-agents/scripts/build_context.py --agent claude --trace
```

## Note

Progetto indipendente, non affiliato né approvato da Anthropic o OpenAI. Claude Code e Codex sono marchi dei rispettivi proprietari; i loro loghi compaiono nell'immagine di copertina solo per nominare gli strumenti con cui questa skill funziona.

## Licenza

[MIT](LICENSE)
