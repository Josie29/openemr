# AgentForge Clinical Co-Pilot

An AI Clinical Co-Pilot built on OpenEMR (Gauntlet AI case study).

**▶ Live app:** https://openemr-production-7ee5.up.railway.app/

Project docs: [PRD — Week 1](PRD-week-1.md) · [PRD — Week 2](PRD-week-2.md) · [ARCHITECTURE.md](ARCHITECTURE.md) · [W2_ARCHITECTURE.md](W2_ARCHITECTURE.md) · [USERS.md](USERS.md) · [AUDIT.md](AUDIT.md)

Deliverables — one row per requirement, with where it lives and how to verify it:
[**Week 1**](docs/DELIVERABLES-week-1.md) (production & ops evidence) ·
[**Week 2**](docs/DELIVERABLES-week-2.md) (multimodal evidence agent).

## Week 1 baseline vs Week 2

Week 2 is additive — it extends the Week-1 agent rather than replacing it. Full delta in [W2_ARCHITECTURE.md §2](W2_ARCHITECTURE.md).

| | Week 1 (baseline) | Week 2 (adds) |
|---|---|---|
| **Agents** | One conversational agent | Supervisor + intake-extractor + evidence-retriever |
| **Inputs** | Structured FHIR reads only | + lab PDF, intake form, medication list (document extraction with per-field citations) |
| **Retrieval** | None — patient record only | Hybrid RAG over a guideline corpus (Qdrant dense+sparse → Cohere rerank) |
| **HTTP surface** | `/health` · `/ready` · `/chat` | + read-only `/documents`, `/documents/{id}/extraction`, `/evidence`; committed OpenAPI spec |
| **Eval harness** | 7 cases, report-only | 53 cases, 5 boolean rubrics, **PR-blocking** |

## Running the Week-2 flow

- **Branch:** `main` (production; `qa/integration` is staging).
- **Services:** `openemr`, `copilot-agent`, MySQL, and **Qdrant** (new in Week 2), plus two external APIs — Cohere Rerank and Mistral OCR.
- **Credentials** (native SDK names; `COPILOT_`-prefixed forms also accepted): `ANTHROPIC_API_KEY`, `QDRANT_URL` + `QDRANT_API_KEY`, `COHERE_API_KEY`, `MISTRAL_API_KEY`, `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY`.
- **No API keys? Run the whole flow offline.** Set `COPILOT_FHIR_CLIENT_MODE=fixture`, `COPILOT_RETRIEVAL_MODE=fixture`, and `COPILOT_EXTRACTOR_MODE=fixture` — bundled FHIR seed data, an in-process retriever, and recorded OCR responses stand in for every external service, and `/ready` returns 200. This is the fastest path for a reviewer.

Full setup, env-var reference, and the API collection: [`agent/README.md`](agent/README.md).

> The live deployment runs on Railway with synthetic demo data. The upstream OpenEMR README follows below.

---

[![Syntax Status](https://github.com/openemr/openemr/actions/workflows/syntax.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/syntax.yml)
[![Styling Status](https://github.com/openemr/openemr/actions/workflows/styling.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/styling.yml)
[![Testing Status](https://github.com/openemr/openemr/actions/workflows/test.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/test.yml)
[![JS Unit Testing Status](https://github.com/openemr/openemr/actions/workflows/js-test.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/js-test.yml)
[![PHPStan](https://github.com/openemr/openemr/actions/workflows/phpstan.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/phpstan.yml)
[![Rector](https://github.com/openemr/openemr/actions/workflows/rector.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/rector.yml)
[![ShellCheck](https://github.com/openemr/openemr/actions/workflows/shellcheck.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/shellcheck.yml)
[![Docker Compose Linting](https://github.com/openemr/openemr/actions/workflows/docker-compose-lint.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/docker-compose-lint.yml)
[![Dockerfile Linting](https://github.com/openemr/openemr/actions/workflows/docker-lint-hadolint.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/docker-lint-hadolint.yml)
[![Isolated Tests](https://github.com/openemr/openemr/actions/workflows/isolated-tests.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/isolated-tests.yml)
[![Inferno Certification Test](https://github.com/openemr/openemr/actions/workflows/inferno-test.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/inferno-test.yml)
[![Composer Checks](https://github.com/openemr/openemr/actions/workflows/composer.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/composer.yml)
[![Composer Require Checker](https://github.com/openemr/openemr/actions/workflows/composer-require-checker.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/composer-require-checker.yml)
[![API Docs Freshness Checks](https://github.com/openemr/openemr/actions/workflows/api-docs.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/api-docs.yml)
[![codecov](https://codecov.io/gh/openemr/openemr/graph/badge.svg?token=7Eu3U1Ozdq)](https://codecov.io/gh/openemr/openemr)

[![Backers on Open Collective](https://opencollective.com/openemr/backers/badge.svg)](#backers) [![Sponsors on Open Collective](https://opencollective.com/openemr/sponsors/badge.svg)](#sponsors)

# OpenEMR

[OpenEMR](https://open-emr.org) is a Free and Open Source electronic health records and medical practice management application. It features fully integrated electronic health records, practice management, scheduling, electronic billing, internationalization, free support, a vibrant community, and a whole lot more. It runs on Windows, Linux, Mac OS X, and many other platforms.

### Contributing

OpenEMR is a leader in healthcare open source software and comprises a large and diverse community of software developers, medical providers and educators with a very healthy mix of both volunteers and professionals. [Join us and learn how to start contributing today!](https://open-emr.org/wiki/index.php/FAQ#How_do_I_begin_to_volunteer_for_the_OpenEMR_project.3F)

> Already comfortable with git? Check out [CONTRIBUTING.md](CONTRIBUTING.md) for quick setup instructions and requirements for contributing to OpenEMR by resolving a bug or adding an awesome feature 😊.

### Support

Community and Professional support can be found [here](https://open-emr.org/wiki/index.php/OpenEMR_Support_Guide).

Extensive documentation and forums can be found on the [OpenEMR website](https://open-emr.org) that can help you to become more familiar about the project 📖.

### Reporting Issues and Bugs

Report these on the [Issue Tracker](https://github.com/openemr/openemr/issues). If you are unsure if it is an issue/bug, then always feel free to use the [Forum](https://community.open-emr.org/) and [Chat](https://www.open-emr.org/chat/) to discuss about the issue 🪲.

### Reporting Security Vulnerabilities

Check out [SECURITY.md](.github/SECURITY.md)

### API

Check out [API_README.md](API_README.md)

### Docker

Check out [DOCKER_README.md](DOCKER_README.md)

### FHIR

Check out [FHIR_README.md](FHIR_README.md)

### For Developers

If using OpenEMR directly from the code repository, then the following commands will build OpenEMR (Node.js version 24.* is required) :

```shell
composer install --no-dev
npm install
npm run build
composer dump-autoload -o
```

### Contributors

This project exists thanks to all the people who have contributed. [[Contribute]](CONTRIBUTING.md).
<a href="https://github.com/openemr/openemr/graphs/contributors"><img src="https://opencollective.com/openemr/contributors.svg?width=890" /></a>


### Sponsors

Thanks to our [ONC Certification Major Sponsors](https://www.open-emr.org/wiki/index.php/OpenEMR_Certification_Stage_III_Meaningful_Use#Major_sponsors)!


### License

[GNU GPL](LICENSE)
