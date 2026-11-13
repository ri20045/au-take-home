# Assignment A pipeline

The pipeline reads lead CSVs and event JSONL files from each drop, stores
normalized lead data and the append-only event ledger in SQLite, and writes the
five Assignment A reports.

## Run from the repository root

Install the project requirements and set a private, persistent HMAC key. Use
the same key for every ingestion run that writes to the same database:

```bash
python -m pip install -r requirements.txt
export AGY_PII_KEY='replace-with-a-private-secret'
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
python -m solution.pipeline ingest data/drop_1
python -m solution.pipeline ingest data/drop_2
python -m solution.pipeline report --out reports/
python -m pytest -q
```

By default the SQLite database is `solution/assignment_a.sqlite3`. Override it
by placing `--db <path>` before the subcommand, for example:

```bash
python -m solution.pipeline --db /tmp/assignment_a.sqlite3 ingest data/drop_1
python -m solution.pipeline --db /tmp/assignment_a.sqlite3 report --out reports/
```

Re-ingesting the same resolved drop path is a no-op. The database records each
completed drop's quality counters and rejected rows so reports include totals
from all ingested drops. Keep the HMAC key safe and stable; changing it prevents
newly ingested phone hashes from matching existing database records.
