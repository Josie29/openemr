# Synthetic Data Generation for OpenEMR

Reference for seeding the dev database with fake patients. OpenEMR has **no
admin-UI generator and no Faker integration** — synthetic data lives entirely in
the Docker dev environment, driven by `openemr-cmd` (which wraps
`docker compose exec openemr /root/devtools <cmd>`).

Three mechanisms, in order of usefulness for the Co-Pilot work:

## 1. Random patients via Synthea (the main generator)

Generates net-new, statistically realistic synthetic patients (with encounters,
problems, meds, labs) using [Synthea](https://github.com/synthetichealth/synthea)
and imports them as CCDA documents.

```sh
openemr-cmd import-random-patients <N> [dev-mode]
```

| Arg | Required | Default | Meaning |
|-----|----------|---------|---------|
| `<N>` | yes | — | Number of patients to generate (integer). Passed to Synthea as `-p <N>`. |
| `dev-mode` | no | `true` | `true` = fast path: imports patient data directly, **bypasses** the CCDA-document import and disables the audit log. `false` = full CCDA-document import path (slower, exercises the real import pipeline). |

```sh
openemr-cmd import-random-patients 100          # 100 patients, dev mode (fast)
openemr-cmd import-random-patients 100 false    # 100 patients, full import path
```

Notes:
- First run downloads `synthea-with-dependencies.jar` + a JRE (~cached in
  `/root/synthea`); subsequent runs are faster.
- Only **count** and **dev-mode** are exposed. Synthea's own `-s <seed>`,
  `-a <ageRange>`, and state/city args are **not** surfaced by the wrapper — you
  can't set a deterministic seed or demographics through this command.
- Generated names carry numeric suffixes (e.g. `Ashley34 Bergstrom287`) — a
  reliable tell for synthetic-vs-real records.
- **Never run with `dev-mode=false` (or at all) against a site holding real
  patient data.** Dev mode disables the audit log during import.

Under the hood: Synthea writes CCDA to `/tmp/synthea/output/ccda`, then
`contrib/util/ccda_import/import_ccda.php` imports it (`--sourcePath`, `--site`,
`--openemrPath`, `--isDev`, plus optional `--dedup` for duplicate-patient
checking and `--enableMoves` to shuffle processed files aside). The wrapper only
lets you reach the two args in the table above; call the PHP script directly if
you need `--dedup` or a non-`default` site.

## 2. Canned demo database (OpenEMR 5.0.0.5 sample data)

A fixed, pre-built sample dataset (patients, encounters, documents, users,
portal logins) — **not generated**, so it's identical every time. Useful when
you want a known, reproducible fixture rather than random data.

```sh
openemr-cmd dev-reset-install-demodata
```

Takes no parameters. **Destructive:** drops all tables, imports the demo SQL,
upgrades the schema, and converts to utf8mb4.

## 3. Custom SQL "drive" (your own datasets)

Imports every `*.sql` file (alphabetical order) from the directory set in the
`SQL_DATA_DRIVE` env var. Use this to load a hand-crafted or exported test
dataset from a mounted volume.

```sh
openemr-cmd dev-sqldrive                        # import the drive into current DB
openemr-cmd dev-reset-install-sqldrive          # fresh install, then import drive
openemr-cmd dev-reset-install-demodata-sqldrive # demo data + drive
```

## Which to use

- **Realistic volume/variety for exercising the Co-Pilot** → `import-random-patients`.
- **A stable, reproducible fixture** (tests, demos, screenshots) → `dev-reset-install-demodata`.
- **A specific curated scenario you control** → `dev-sqldrive`.

## Generating data on the deployed Railway app

The production deployment (`agentforge-openemr` project, `openemr` service,
`https://openemr-production-7ee5.up.railway.app`) runs the official
`openemr/openemr:flex` image — the **same** image that ships `/root/devtools` and
the Synthea tooling. So all three mechanisms above work identically against the
deployed container. The only thing that changes is how you reach it: Railway has
no `openemr-cmd` and no `docker compose exec`. Use `railway ssh` to run the
`devtools` command directly inside the running container.

The repo is already linked to the `openemr` service (`railway status` confirms),
so no `-s`/`-p` flags are needed:

```sh
# Random Synthea patients into the deployed MySQL
railway ssh /root/devtools import-random-patients 100
railway ssh /root/devtools import-random-patients 100 false   # full CCDA import path

# Or open an interactive shell in the container and run devtools there
railway ssh
# then, inside: /root/devtools import-random-patients 100
```

Be explicit about the target if the link is ever ambiguous:

```sh
railway ssh -s openemr -e production /root/devtools import-random-patients 100
```

Map of local command -> deployed equivalent:

| Local (`openemr-cmd ...`) | Deployed (`railway ssh ...`) | Safe on Railway? |
|---|---|---|
| `import-random-patients 100` | `railway ssh /root/devtools import-random-patients 100` | Yes (additive) |
| `dev-sqldrive` | `railway ssh /root/devtools dev-sqldrive` | Yes (import only) |
| `dev-reset-install-demodata` | *(do not run — see below)* | **No — bricks the app** |

### Do NOT run `dev-reset-install-*` on Railway

The `dev-reset-install-demodata` / `dev-reset-install-*` family is written for the
**local dev stack** and assumes local-dev scaffolding that does not exist in the
deployed flex image: a pristine `/openemr` source snapshot, `/root/auto_configure.php`,
a couchdb data dir, and a **local** database literally named `openemr`. On Railway,
the database is the separate managed **MySQL service** — so the reset gets just far
enough to `DROP DATABASE openemr` and drop the `openemr` DB user, then fails partway
through the reinstall. Result: the app goes to **HTTP 500** (no database), the volume's
`sites/default` (config + crypto keys) is wiped, and all patient data is lost. This
happened once (2026-07-07) and required a full rebuild — see recovery below.

**Recovery if it was already run** (rebuilds a clean empty OpenEMR; any data already
dropped is unrecoverable, but synthetic patients are regenerable):

1. Fix the DB/admin credentials via Railway's API (do **not** use `railway variables
   set` with values containing quotes — a smart-quote once corrupted `MYSQL_PASS`
   into a multi-line shell fragment). Set `MYSQL_PASS` (clean hex), `OE_PASS`, and
   `OE_USER`. Setting variables auto-triggers a redeploy.
2. The redeploy starts a **fresh container from the `openemr:flex` image**, which
   restores `/var/www/localhost/htdocs/auto_configure.php` (deleted after the first
   successful install). With `sqlconf.php` and the DB both gone, the flex entrypoint
   (`docker/flex/openemr.sh`) treats it as first boot and **auto-recreates** the
   `openemr` database, DB user, full schema, `sites/default`, and the admin account
   from the env vars. Only a redeploy does this — a plain **restart** will not (the
   provisioning is gated on `auto_configure.php`, which the running container no
   longer has).
3. Verify: `railway ssh` + `mariadb --skip-ssl -h "$MYSQL_HOST" -u root -p"$MYSQL_ROOT_PASS"
   -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='openemr';"`
   should show ~283 tables, and the app root should return 302.
4. Reseed: `railway ssh /root/devtools import-random-patients <N>`.

### Railway-specific caveats

- **This writes to the live deployed database.** Data lands in the `MySQL`
  service (persisted on `mysql-volume`), so it survives redeploys — but so does
  any mess. There is no separate "dev" DB here; the deployed MySQL *is* the
  target. Treat the deployed data as the environment of record, per
  `deployment-strategy.md`.
- **`import-random-patients` is additive** — it appends patients without dropping
  anything, which is the only data-generation command that is safe to point at the
  deployment.
- **Dev mode disables the audit log during import.** `import-random-patients N`
  defaults to `dev-mode=true`, which turns off the audit log for the run. Given
  the audit's PHI/compliance posture (`AUDIT.md`), only ever point this at the
  synthetic-seed deployment — never at a site holding real patient data.
- **First run re-downloads the Synthea JAR + JRE.** The cache lives at
  `/root/synthea` in the container's ephemeral filesystem (not on a volume), so
  the first `import-random-patients` after each redeploy pays the one-time
  download cost again.
- **`railway ssh` needs the service online.** If it can't connect, check
  `railway status` shows the `openemr` service `Online` first.

## Worktree note

To target a specific worktree's container from outside it, prefix any command
with `worktree exec <branch>`:

```sh
openemr-cmd worktree exec <branch> import-random-patients 100
```
