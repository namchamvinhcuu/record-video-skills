---
description: Check this computer for everything needed to record a manual video (Python, Playwright, ffmpeg, fonts, disk)
---

Check whether this computer is ready to record a user-manual video with the `record-demo-video` skill of this plugin.

1. Run the checker. On Windows run the launcher, which finds a working Python by itself:

   `"${CLAUDE_PLUGIN_ROOT}/check-env.cmd"`

   If that cannot be run in this shell, run the script with the first Python that works:
   `py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/check_env.py"`, then `python "${CLAUDE_PLUGIN_ROOT}/scripts/check_env.py"`.
2. Show the user the report as it is: one line per prerequisite (`OK`, `MISSING` or `SKIP`), and under each
   `MISSING` the command that fixes it.
3. For every `MISSING` line, explain in plain language what it is for and offer to run the fix command. Run an
   install command only after the user agrees; a program that was just installed is not visible to a terminal
   that was already open, so ask the user to open a new terminal (and start Claude Code from it) and run this
   check again.
4. Never ask the user to type a password into the chat. This check needs no login.
