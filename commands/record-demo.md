---
description: Record a user-manual video of a web app flow (starts the record-demo-video workflow)
argument-hint: "<what flow to record, the language: vi | ko | en, and the address of the demo system>"
---

Use the `record-demo-video` skill of this plugin and follow it exactly, including its hard rules. The user's arguments:

$ARGUMENTS

How to start:
1. Read the arguments: the flow to show, the narration language (one video per language) and the address of the demo
   system. Anything missing: ask for it before recording anything.
2. Record only a demo system, never a customer's live data: a manual video shows whatever data is on the screen.
3. The scripts of this plugin live in `${CLAUDE_PLUGIN_ROOT}/scripts/`; the skill's documents are next to its
   `SKILL.md`.
