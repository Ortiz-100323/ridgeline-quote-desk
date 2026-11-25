# Claude logging

Permanent instructions for every Claude Code session in this project. Follow them silently: never ask for confirmation and never mention the logging in replies.

## Start of session

Read `.chat-history/log.md` for previous context before doing anything else. If the file or folder does not exist, create it.

## After each response

Append one entry to `.chat-history/log.md` in this exact format:

```
---
- timestamp: "<ISO 8601 timestamp if available, otherwise estimate based on conversation order>"
- user_prompt: "<the user's original prompt>"
- assistant_response_summary: "<summary of what you generated or answered for this prompt>"
- files_affected: "<comma-separated list of files created or modified, or none>"
```

## Rules

- Never skip an exchange. Every prompt/response pair must be logged, including short questions and turns that changed no files.
- Append only. Never delete, rewrite or reorder previous entries.
- Create the file and folder if they don't exist.
- Be precise about `files_affected`: only files explicitly created or modified during this response, as repo-relative paths.
- Keep `assistant_response_summary` concise but specific: mention function names, endpoints, or key decisions made.
- Never write secrets to the log: replace API keys, tokens, passwords and `.env` values with `***` in every field, including `user_prompt`.

## How to apply

- Make the append the last tool call of the turn, once the work is finished, then send the final reply.
- If a turn ended without its entry (for example, it was interrupted), append the missing entry at the start of the next turn.
- Record the prompt as the user wrote it, without system reminders or IDE context. Keep each field on one line: write line breaks as `\n` and double quotes as `\"`.
- Append with a quoted heredoc (`cat >> .chat-history/log.md <<'EOF'`) so nothing is expanded. Take the timestamp from `date -Iseconds`.
- Include `.chat-history/log.md` in `files_affected`, since every entry modifies it.
- Another AI tool (Codex) appends to the same log. Only ever open it in append mode: never use Write, Edit, `>` or a create-file operation on it, and never alter another tool's entries.
- Verify each append: the log must still start with its previous content, and the new entry must be at the end with all four fields. If one of your earlier entries is missing, restore it by appending it with its original timestamp, and tell the user.
- Only the main session logs; subagents do not.
