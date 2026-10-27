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

- Current working tree: 69 non-test Python files, 30 test files, approximately 20,730 Python lines.
- Current regression run: 375 tests passed with `py -3 -m unittest discover -p "test_*.py"`; existing `ResourceWarning` messages remain.
- A documented production sample contains 12 chapters and approximately 30,474 words. A later editorial assessment recorded 19 issue groups and about 45 text-level issues, so it was not considered ready for release.
- Three local portfolio runs contain complete 12-chapter manuscripts of roughly 30,000 words each. Their story text and runtime artifacts are excluded from this repository.
- A historical long request completed in about 125 seconds after streaming support was added; this is one observed run, not a throughput guarantee.

The code and test counts describe the full local working tree, which also contains excluded experiments. They do not imply that every new quality workflow passed a real end-to-end production run. See the [project dossier](docs/portfolio/project-dossier.md), [production evidence](docs/portfolio/production-evidence.md), and [evidence index](docs/portfolio/evidence-index.md) for details.

## Tests

```powershell
py -3 -m unittest discover -p "test_*.py"
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
