# AI Usage Disclosure

AI tools are allowed. We want to know **how** you used them, because using them well is a
skill. Honest answers are never held against you. Discovering undisclosed use is.

## Tools used
GitHub Copilot AI assistance in VS Code.

## What I used them for
I used GitHub Copilot as a coding assistant for specific implementation questions and debugging tasks. Some examples include:

Asked it to explain the existing repository structure and how the different Python modules were connected.

Used it to understand the existing database schema and check which fields were available in the lead and event tables.

Asked for suggestions on handling missing values, duplicate records, and data validation during data cleaning.

Used it for guidance on parsing incoming lead and event data and inserting records into SQLite.

Asked how to handle repeated ingestion of the same data without creating duplicate records.

Used it to understand how to structure SQL queries for generating the required reports.

Asked for help understanding test failures and identifying mismatches between the report tests and the actual database schema.

Used it to clarify Python commands, pytest output, and Git staging and commit operations.

I treated Copilot's responses as suggestions and explanations, reviewed them against the existing code and assignment requirements, and verified the implementation by running tests and checking the generated outputs.

## What I wrote or decided myself
I reviewed the assignment requirements, explored the existing implementation, and made decisions about how the different components should work together.

I was responsible for implementing and integrating the changes, reviewing suggestions, making adjustments, and ensuring the solution aligned with the assignment requirements.

I also ran the commands, checked the outputs, reviewed staged changes before committing, and decided how to proceed at each stage.

I am responsible for understanding and being able to explain the final implementation, its design decisions, assumptions, and limitations.

## Where the AI was wrong or unhelpful
During development, an early suggestion for the report tests was based on assumptions about database fields and event relationships that did not match the repository's actual schema.

I identified the mismatch by checking the existing database structure and assignment specification, then adjusted the tests to match the actual tables and fields.

I also verified command-line staging and commit instructions against the actual Git output rather than blindly relying on suggestions.

## How I verified AI-generated code
I ran the automated test suite using .venv/bin/python -m pytest -q, which resulted in 67 passing tests.

I also generated fresh, small Assignment A datasets, ran both ingestion commands, reran ingestion for the first data drop to verify idempotency, and generated all five reports.

I reviewed the generated filenames and data-quality totals to ensure the expected outputs were produced.

I did not run the optional large-scale performance test.