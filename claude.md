# knotify-backend

## Project metadata

- **Name:** knotify-backend
- **Description:** Cloud Hosted Backend Infrastructure for Knotify React Native App
- **Scope / vision:** Backend infrastructure hosted and maintained on AWS cloud into development and production environment
- **Desired output:** A Fully operational, robust, well tested, well architected cloud infrastructure
- **Intended audience:** For development environment the audience is project developers and testers, for production real world users

## Technology stack

- **Frontend:** None
- **Backend:** Python, Terraform
- **Hosting:** AWS
- **Deployment:** AWS
- **IaC:** Terraform

## Workspace rules

- `C:\Users\syede\Claude-Master\engineeringprinciples.md` — global engineering principles + non-negotiable rules (no parallelism, phase-completion handoff)
- `C:\Users\syede\Claude-Master\gitbranching.md` — git branching strategy (skeleton, filled out later)
- `C:\Users\syede\Claude-Master\contextmanagement.md` — protocol for context updates and `/clear` after each feature

<!-- ## Workspace lessons applied
     (Populate this section only if the user opted in during /start-project. Otherwise omit it.) -->

## Workflow

Workflow triggers are slash commands. Never act on phrases.

- `/create-plan` — light cross-phase brainstorm on `architecture.md`, then generate the thin `implementationplan.md` index plus one detailed PRD per phase under `implementationplan/`
- `/implement-phase <n>` — per-phase brainstorm against the active PRD (persisted to `phasebrainstorms/`), then implement phase `n` if its `ready` flag is true in the index and prior phases are done

## Session loading

When a new session starts in this folder, the main agent reads (in this order):

1. Workspace `CLAUDE.md` (auto-loaded)
2. This file (`claude.md`)
3. `context.md`
4. `implementationplan.md` (thin index only)

It does NOT load `architecture.md`, `codingprinciples.md`, `cicd.md`, any per-phase PRD under `implementationplan/`, or any brainstorm file under `phasebrainstorms/` at session start. Those are read on demand.
