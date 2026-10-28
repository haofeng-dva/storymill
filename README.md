# Storymill

**An AI-assisted English short-fiction production and quality-control engine.** Storymill connects story discovery, planning, chapter generation, acceptance checks, issue tracking, repair workflows, and EPUB packaging in a resumable Python application.

This project is archived as an engineering portfolio as of October 27, 2026. The repository documents both implemented workflows and the limits still found in real production runs. A generated manuscript or a passing test suite is not treated as proof of publication readiness.

## What it does

- Collects market signals and stores story recommendations for human selection.
- Generates an outline and world bible, then writes chapters through configurable model providers.
- Tracks run and chapter state, model calls, retries, errors, and resumable work in SQLite.
- Compiles chapter contracts, scene plans, fact slots, and character voice guidance.
- Separates deterministic text checks from model-based editorial review.
- Keeps anchored repair cards, source hashes, revisions, and recheck status.
- Packages manuscripts and supporting metadata as EPUB and delivery manifests.
- Provides a local dashboard for operator review and intervention.

## Architecture

```text
radar.py / direction_manager.py  discovery and story selection
engine.py                        outline, world bible, chapter writing and acceptance
control_*.py / story_bible*.py   chapter contracts, continuity and fact checks
quality_*.py / native_check.py  deterministic checks and editorial review
repair_*.py / final_editor*.py  anchored repair and final-candidate verification
orchestrator.py / state_store.py resumable runs, scheduling and call audit
packager.py / dashboard.py      delivery artifacts and operator interface
test_*.py                        regression and contract tests
```

## Evidence snapshot

- The Git checkout contains the application, configuration, documentation, and 30 test modules. Local experiments, generated stories, databases, credentials, and one-off helper scripts are not part of the checkout.
- Current regression run: 375 tests passed with `py -3 -m unittest discover -p "test_*.py"` on Python 3.13.14; existing `ResourceWarning` messages remain.
- A documented production sample contains 12 chapters and approximately 30,474 words. A later editorial assessment recorded 19 issue groups and about 45 text-level issues, so it was not considered ready for release.
- Three local portfolio runs contain complete 12-chapter manuscripts of roughly 30,000 words each. Their story text and runtime artifacts are excluded from this repository.
- A historical long request completed in about 125 seconds after streaming support was added; this is one observed run, not a throughput guarantee.

The test result describes a local run. It does not imply that every quality workflow passed a real end-to-end production run. See the [project dossier](docs/portfolio/project-dossier.md), [production evidence](docs/portfolio/production-evidence.md), and [evidence index](docs/portfolio/evidence-index.md) for details.

## Quick start

The documented setup targets Windows and CPython 3.13. The current checkout was tested with Python 3.13.14. The tracked application and test suite use the Python standard library; `requirements.txt` records that no third-party pip packages are needed.

From PowerShell, clone the repository and create an isolated environment:

```powershell
git clone https://github.com/haofeng-dva/storymill.git
cd storymill
py -3.13 -m venv .venv
# The tracked code has no third-party packages. Run this when pip is available
# to verify the declared dependency file; otherwise skip this line.
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -p "test_*.py"
```

Check the command-line entry point without contacting an external service:

```powershell
.\.venv\Scripts\python.exe engine.py --help
```

To open the local operator dashboard:

```powershell
.\.venv\Scripts\python.exe dashboard.py
```

Then visit `http://localhost:8900`. The dashboard and its Python child processes use the same interpreter from `.venv`.

## Configuration and credentials

The repository already includes two configuration files:

- `config.json` selects provider endpoints, model names, routing, writing defaults, and output paths.
- `orchestrator.example.json` is the tracked template for `orchestrator.json`, which controls recommendation sources, quality thresholds, production limits, and optional workflow switches. `orchestrator.json` is intentionally ignored because the dashboard can modify local thresholds and switches.

Before starting the dashboard or orchestrator for the first time, create the local orchestrator config:

```powershell
Copy-Item orchestrator.example.json orchestrator.json
```

These files contain project defaults, not a complete service account. Provider access requires your own credentials and a currently reachable endpoint. Copy `.env.example` to `.env` and fill only the variables required by the routes you use. Common names include `ARK_API_KEY`, `OPENAI_RELAY_BASE`, `OPENAI_RELAY_KEY`, `GEMINI_API_KEY`, and `GEMINI_PROXY`. The local `keys.local.json` file is also supported. `.env` and `keys.local.json` are ignored by Git; never put credentials in either tracked configuration file.

The first real generation or recommendation run contacts external services and may incur charges. The `engine.py --help` check and unit tests do not make those calls. A fresh clone starts without `state.db`, generated manuscripts, logs, or cached production outputs; those are created locally as needed and are intentionally excluded from Git.

The ignored `node_modules` junction sometimes present in the development workspace is not a project dependency: the repository has no Node package manifest, and the tracked application does not use it. Optional local document-building scripts are not included in the Git checkout and are not part of the supported setup.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -p "test_*.py"
```

The suite covers parsing, contracts, state transitions, recovery behavior, deterministic checks, repair validation, and packaging. It uses mocks and isolated fixtures for most paths; it does not establish editorial quality or replace a live production smoke test.

## Configuration and data

Runtime model credentials are read from environment variables. Supply your own provider configuration and credentials; do not commit `.env`, `keys.local.json`, databases, generated manuscripts, logs, or model responses. The repository `.gitignore` excludes common runtime and generated-workspace paths.

The application requires external model and discovery services for production use. Availability, model names, provider APIs, and credentials change over time, so the archived local configuration is not a current service guarantee.

## Portfolio materials

- [Project dossier](docs/portfolio/project-dossier.md)
- [Resume bullets in Chinese and English](docs/portfolio/resume-bullets.md)
- [Evidence index and public-data boundary](docs/portfolio/evidence-index.md)
- [Machine-readable metrics snapshot](docs/portfolio/metrics.json)

## License

No open-source license is currently declared. Do not assume the code is licensed for reuse until a license is added.
