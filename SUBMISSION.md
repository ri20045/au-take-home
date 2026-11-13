# Submission

## Candidate
- **Name:** Riya Rawat
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** 8 hours

## How to run

From a clean clone, run these commands from the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export AGY_PII_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
python -m solution.pipeline ingest data/drop_1
python -m solution.pipeline ingest data/drop_2
python -m solution.pipeline report --out reports/
python -m pytest -q
```

The default SQLite database is `solution/assignment_a.sqlite3`. To use another
database, pass `--db <path>` before the subcommand. Keep the HMAC key private
and use the same key for every run against the same database.

## Design

The pipeline separates schema loading, data cleaning and validation, database
storage, event ingestion, report generation, and command-line orchestration.

Lead records are stored in SQLite using `(workflow_id, phone_hash)` as their
identity. Phone numbers and other fields marked as PII are stored as HMAC
hashes; the phone's last four digits are retained for the masked lead report.
Calls and events are stored separately. Events are append-only, and
`event_links` records their relationships to leads.

The command-line interface processes one drop at a time. It records per-drop
quality counts and rejected rows in SQLite, and skips a drop if that resolved
path has already been ingested. Reports are generated from the stored leads
and linked reportable events.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| Use SQLite for local storage | It meets the assignment's storage requirements without requiring a separate database service. | PostgreSQL |
| Use HMAC-SHA256 for PII fields | It protects PII at rest while allowing deterministic matching between leads and events. | Store plaintext or use a non-keyed hash |
| Identify a previously ingested drop by its resolved path | This makes rerunning the same drop path a no-op and prevents its quality counts from being added again. | Reprocess every file and rely only on record-level deduplication |
| Persist drop-level quality counts and rejections in SQLite | These values are needed to create reports after ingestion has finished. | Keep counts only in memory and require them to be supplied manually |

## Failure modes considered

- Invalid lead rows are rejected with a reason; invalid optional fields are
  treated as warnings according to the schema rules.
- Duplicate leads are merged, and duplicate event IDs are not inserted again.
- Malformed event lines are counted and skipped.
- Events that cannot be linked are retained without a lead link and excluded
  from report facts. Orphan totals use the final database state so an event can
  be linked if its lead arrives later.
- Unknown-disposition events are retained but excluded from report facts.
- The event ledger prevents updates and deletes.
- The HMAC key is required for PII hashing. It must remain stable for a
  database; changing it prevents newly ingested phone hashes from matching
  existing ones.
- Drop idempotency is based on the resolved path. The pipeline does not detect
  if files at an already-ingested path have changed.

## Testing

### Automated tests

Ran the full test suite with:

```bash
.venv/bin/python -m pytest -q
```

**Result:** 67 tests passed.

The tests cover:

1. Cleaning and normalizing phone numbers, dates, decimal values, and enums.
2. Lead validation, including missing required fields, invalid values,
   optional-field warnings, and duplicate leads across files and drops.
3. SQLite lead storage, merging behavior, transaction rollback, and protection
   of PII using HMAC.
4. Event ingestion, including malformed JSONL lines, duplicate event IDs,
   unknown dispositions, orphan events, late lead linking, and append-only
   ledger behavior.
5. Report calculations and output formats, including required CSV headers,
   connectivity rates, masked phone numbers, dispositions, buckets, and
   data-quality totals.
6. CLI ingestion of multiple drops, re-ingestion of the same drop, and
   generation of all five reports.

### End-to-end check

Generated fresh small Assignment A data with seed 42, ingested both drops,
ingested the first drop again, and generated reports. Re-ingestion reported
that the drop had already been processed. The run produced all five expected
report files.

The recorded data-quality totals included 2,057 lead rows, 108 rejected rows,
57 merged duplicate lead rows, 8,745 event lines, 8 malformed event lines, and
237 duplicate event lines.

### Not tested

1. The optional large dataset and its under-two-minute performance target; no
   performance timing was recorded.
2. Multiple generated seeds or a comparison against an independent golden set
   of expected report files.
3. Running the setup instructions from a separate clean clone.

These checks were not completed, so I cannot claim they passed.

## Results

Generated the small Assignment A dataset with seed 42, ingested both drops,
then generated the reports. The run produced all five expected report files.

The data-quality report showed:

- 2,057 lead rows read: 1,892 accepted unique leads, 57 duplicate lead rows
  merged, and 108 rejected rows.
- 122 optional-field warnings.
- 8,745 event lines read: 8 malformed lines and 237 duplicate events dropped.
- 462 events remained unlinked in the final database state, and 19 events had
  unknown dispositions.

I did not record the run time, so I cannot report a measured timing. I also did
not run the optional large-scale performance test.

## If I had another day

1. Test the pipeline with additional generated seeds and compare the five
   reports against independently checked expected results.
2. Strengthen drop tracking so the pipeline can detect when files change after
   a drop has already been ingested.
3. Test recovery from a failure partway through ingesting a drop, including
   whether retrying preserves accurate quality counts.
4. Run the optional large dataset, measure the runtime, and optimize if it does
   not meet the target.
5. Review the final setup instructions and submission files from a clean clone
   before submitting.
