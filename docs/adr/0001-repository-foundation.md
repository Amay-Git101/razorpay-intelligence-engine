# ADR-0001: Repository Foundation Scaffold

**Status:** Accepted

## Context

The project had two governing markdown documents (architecture
contract v0.1, master handoff v1.0) and a set of Phase 1 verification
artifacts, but no repository structure, no version control, and no
Docker/dependency tooling verified as available on the development
machine. Before any implementation, a minimal, reversible foundation
was needed that matches the monorepo layout both governing documents
already specify (architecture contract §31, master handoff §23).

## Decision

- Initialize Git at the current directory as the repository root (no
  remote yet — will be added once a GitHub repository is provided).
- Create the agreed monorepo directory structure with a short README
  per directory explaining its purpose and current (empty) status —
  no application code, dependencies, or manifests.
- Add root-level `README.md`, `.env.example`, `docker-compose.yml`
  (Postgres + Redis only), `.gitignore`, and this `docs/adr/`
  structure.
- Precedence between the two governing documents is fixed as:
  architecture contract = engineering authority, master handoff =
  consolidated project/process context. Any future conflict between
  them must be raised explicitly, not silently resolved.
- Docker Desktop was not found installed on the development machine.
  Per explicit instruction, this is treated as a reported blocker, not
  something to spend implementation time on installing right now. The
  `docker-compose.yml` file exists but is unverified.

## Alternatives

- Scaffold `apps/api` and `apps/web` with real framework
  initialization (FastAPI/Next.js) now — rejected, out of scope for a
  "foundation only" approval; that is implementation.
- Install Docker Desktop automatically — rejected; requires admin
  elevation and likely a reboot, which is a system-level change
  outside what this task authorized.

## Trade-offs

- Directories are currently empty except for README placeholders —
  git does not track empty directories, so a README (rather than a
  bare `.gitkeep`) was used to also document intent per-folder.
- No dependency manifests exist yet, so there is nothing to install or
  lock. This avoids taking on dependency risk before the first
  vertical slice is chosen.

## Consequences

- The next implementation step (canonical event/state model, domain
  schema) has a stable place to land without further structural churn.
- Docker-based local development is blocked until Docker Desktop is
  installed and `docker compose up` is actually run and verified.
