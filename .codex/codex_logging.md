# codex_logging

These instructions apply to every Codex session and every user prompt handled
in this project.

1. At the start of each session, read `.chat-history/log.md` for previous
   context. If the directory or file does not exist, create it before
   continuing.
2. For every user prompt and assistant response, append exactly one entry to
   `.chat-history/log.md` using this exact four-field format:

   ```text
   ---
   - timestamp: "<ISO 8601 timestamp if available, otherwise estimate based on conversation order>"
   - user_prompt: "<the user's original prompt>"
   - assistant_response_summary: "<summary of what you generated or answered for this prompt>"
   - files_affected: "<comma-separated list of files created or modified, or none>"
   ```

3. Before sending the final response, finalize its summary, check all four
   fields for secrets and format, and append the entry. Verify the append
   succeeded. If the write result is unclear, check the file before retrying
   to avoid a duplicate entry. Create `.chat-history/` and `log.md` if needed.
   Append only; never delete, replace, truncate, reorder, or rewrite previous
   entries.
4. Do all logging silently and never ask for confirmation.
5. Preserve the user's original prompt in `user_prompt`. When line breaks or
   quotation marks would invalidate the one-line field, encode them with
   JSON-style escapes such as `\n` and `\"` without otherwise summarizing or
   paraphrasing the prompt.
6. In `files_affected`, list only files explicitly created or modified during
   the current exchange. Use repository-relative paths separated by commas, or
   `none` when no files changed. Include `.chat-history/log.md` because the
   current entry modifies it.
7. Keep `assistant_response_summary` concise and specific. Mention relevant
   function names, endpoints, files, and key decisions.
8. Never write secrets to the log. Before appending, replace every API key,
   token, password, credential, and `.env` value with `***` in every field,
   including `user_prompt`. Do not read `.env` merely to discover values for
   redaction; redact anything that appears or is identified as sensitive.
9. Never skip an exchange, including exchanges that only answer a question,
   report an error, request clarification, or make no project-file changes.
