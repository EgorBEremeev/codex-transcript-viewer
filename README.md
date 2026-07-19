# codex-transcript-viewer

Lossless, local inspection for Codex CLI JSONL sessions. One standard-library Python core powers:

- `codex-transcript`, a human and agent-friendly CLI
- self-contained HTML transcripts
- compact JSONL/Markdown exports and focused queries
- parent/subagent session trees
- a Codex plugin with deterministic usage guidance

This is a fork of [masonc15/codex-transcript-viewer](https://github.com/masonc15/codex-transcript-viewer). The original HTML viewer remains the visual foundation.

## Install

Install the CLI and Codex plugin directly from GitHub:

```bash
curl -fsSL https://raw.githubusercontent.com/sadanand1120/codex-transcript-viewer/main/install.sh | bash
```

The installer refreshes both the `codex-transcript` CLI and `codex-transcript@codex-transcript` plugin without tying either installation to a checkout.

## Human commands

```bash
codex-transcript list --limit 10
codex-transcript render SESSION --output transcript.html
codex-transcript browser SESSION
codex-transcript browser SSH_ALIAS:SESSION_ID
```

`browser` writes a deterministic private HTML file under the system temporary directory and opens it with the default browser.

The viewer keeps rolled-back turns under closed archive markers. Tool calls, tool outputs, and reasoning details are also collapsed by default.

## Agent commands

```bash
codex-transcript --json doctor
codex-transcript query SESSION --view conversation --last 10 --format jsonl --compact
codex-transcript query SESSION --turn TURN_ID --compact
codex-transcript export SESSION --format jsonl --compact --redact --output session.jsonl
codex-transcript tree SESSION --format json
codex-transcript raw SESSION --line 42 --redact
codex-transcript breakdown SESSION --output breakdown.json
codex-transcript analyze breakdown.json --output analysis/SESSION_ID
codex-transcript visualize breakdown.json --spans analysis/SESSION_ID/spans.json --output trace.html
```

`--view conversation` reconciles duplicate log representations into the canonical user/assistant flow. Use `--last N` to bound recent context before reaching for raw normalized events.

`SESSION` accepts a local JSONL path, local session ID/prefix, or `SSH_HOST:SESSION_ID`. Remote references work with `render`, `browser`, `export`, `query`, `tree`, and `raw`.

`breakdown` is a local-only JSON export for performance analysis of a root session and its subagent tree. It preserves every physical record (including each `token_count` snapshot), records native/inherited provenance, pairs tool calls with outputs, and stores content sizes instead of transcript bodies. A unique JSONL basename in the sessions directory is also accepted by local commands.

`analyze` reads an immutable breakdown JSON and writes `spans.json`: task, session, turn, and linked tool spans. It keeps only event IDs and derived timing/size attributes, never a second copy of the events. `visualize` validates that spans were generated from the supplied breakdown and writes a self-contained local Trace HTML with the common time scale, cumulative payload/token graphs, and the numerical event table.

Remote sessions are fetched through `ssh-script`, parsed locally, and removed from private staging when the command finishes. Browser HTML, exports, and every other final output stay on the current machine; the remote session is never modified. Remote hosts need only Python 3 and a configured SSH alias. When supplied with a remote reference, `--sessions-dir` refers to the remote sessions directory.

## Data policy

- Every parsed line receives a versioned normalized envelope.
- Unknown records and malformed JSON remain visible instead of being silently discarded.
- Function and custom tool calls/results retain their `call_id` relationship.
- Subagent identity uses the first `session_meta`; copied parent history is marked `inherited` and excluded by default.
- Raw JSON is preserved by default. `--compact` removes raw known records and truncates large values.
- JSONL export and query stream records instead of loading the whole transcript.

Generated files use owner-only permissions. They may still contain commands, paths, prompts, and tool output. `--redact` performs best-effort redaction of secret-like keys, assignments, and authorization headers; inspect every artifact before sharing.

## Plugin layout

```text
.agents/plugins/marketplace.json
plugins/codex-transcript/.codex-plugin/plugin.json
plugins/codex-transcript/skills/codex-transcript/SKILL.md
```

The CLI is deterministic infrastructure. The plugin teaches Codex to discover sessions, query narrowly, inspect subagent trees, prefer compact structured evidence, and reserve `browser` for human-facing use.

## Development

```bash
git clone https://github.com/sadanand1120/codex-transcript-viewer.git
cd codex-transcript-viewer
./scripts/install-local.sh
./scripts/run-tests.sh
```

The local installer keeps the CLI editable for development. The runtime has no third-party dependencies.
