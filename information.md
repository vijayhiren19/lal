# Project Instructions

> **Process guide.** For the complete technical specification, see `AGENTS.md`.

## Role

You are an expert at building applications using LLM models with the OpenCode orchestrator.

## Approach

Follow the **markdown-driven development** pattern:

| File | Role |
|------|------|
| `AGENTS.md` | **Technical specification** — pipeline, DB schema, scoring formula, CLI |
| `.opencode/context/schema.md` | **Database schema reference** — all table definitions in one place |
| `.opencode/skills/*.skill.md` | Implementation patterns and conventions (generated later) |
| `information.md` | **This file** — development workflow and process guide |
| `README.md` | Project overview and quick start |

## Goal

Build a framework where:

1. Markdown files define the application behavior
2. The orchestrator reads markdown files and generates Python code
3. The application uses SQLite as its database
4. The application predicts stocks based on calculations on historical data

## Workflow

### How to Add or Change Features

1. **Edit the markdown file first** — Update `AGENTS.md` or relevant skill file
2. **Ask the orchestrator to implement** — Agent reads the updated markdown and generates code
3. **Verify** — Run tests/lint to confirm changes match the spec

> Markdown files define the application behavior. All changes start with markdown.

## Technical Requirements

### Performance

- Use **SQLite batch updates** (`executemany` with 500-row batches)
- Use **multi-threading** where possible (e.g., `ThreadPoolExecutor` for downloads, scoring)
- Use **vectorized pandas operations** instead of row-by-row loops

### Architecture

- Main agent acts as **orchestrator**
- Sub-agents do the actual work (via `task` tool)
- Keep the main agent responsive by delegating heavy work

## Documentation Standards

### Make Markdown Files:

- **Robust** — Cover edge cases, error handling, data quality
- **Simple** — Easy for humans to understand and modify
- **LLM-friendly** — Clear structure, tables, code blocks, consistent formatting

### File Ownership

These markdown files are part of the repository. They enable:

- Building new features by editing markdown first
- Changing existing features by updating the spec
- Onboarding new contributors with clear documentation

## Current State

The specification may not be perfect. The task is to:

1. Refine markdown files to be more robust and human-readable
2. Ensure consistency across all documentation
3. Start implementing the application based on the refined specs

## Notes

- Do research and think carefully before making changes
- Ask questions if anything is unclear
- Prefer using sub-agents for implementation work
