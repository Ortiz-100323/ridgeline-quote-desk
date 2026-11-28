# Project instructions

At the start of every Codex session in this repository, read
[`.codex/codex_logging.md`](.codex/codex_logging.md) and follow its instructions
to load the project history. Before sending each final response, append and
verify exactly one redacted entry for the current exchange.

## System contract

Before working on the portal, dispatcher, worker or database, read
[`docs/CONTRACT.md`](docs/CONTRACT.md). It defines the database schema, the job
queue, the draft quote JSON and what the portal may read and write. Never read
`answer_key/`.
