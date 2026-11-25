# record-video-skills

A [Claude Code](https://claude.com/claude-code) plugin that lets QA/QC staff ask Claude to record a **user-manual video** of a web-app flow (built for Odoo): a real isolated browser, a visible arrow cursor with a click ripple, one narration cue per click, subtitles burned in, **one video per language** (Vietnamese, Korean, English).

Vietnamese guide for the people who record the videos: [README.vi.md](README.vi.md).

## Status

| Platform | State |
|---|---|
| Windows 11 | Implemented (Playwright video capture, per-language subtitle fonts, UTF-8 handling, `check-env`). **Not yet verified on a real Windows 11 machine** — the acceptance run is pending. Treat the first take as a test. |
| macOS | The same capture path was measured on synthetic pages (see `skills/record-demo-video/SKILL.md`). |
| Linux | Virtual-display capture, covered by the automated tests of this repository. |

The test suite (`run-tests.cmd` on Windows, `./run-tests.sh` elsewhere) runs on any platform; it does not prove that a recording works on a given Windows machine.

## Install

In Claude Code:

```
/plugin marketplace add namchamvinhcuu/record-video-skills
/plugin install record-video-skills@record-video
```

Then, once per machine, check the prerequisites:

```
/record-video-skills:check-env
```

It lists Python 3.10+, Playwright + Chromium, ffmpeg with libass and libx264, a Korean-capable font and free disk space, and prints the exact install command (`winget` / `pip`) for everything that is missing.

## Commands

| Command | What it does |
|---|---|
| `/record-video-skills:record-demo` | Starts the `record-demo-video` workflow: records a guided tour and produces one subtitled video per language. |
| `/record-video-skills:check-env` | Checks this computer for everything a take needs. |

## What it will and will not do

- It drives its **own temporary browser profile**; it never records your real screen and never uses your real browser profile.
- Use a **demo system and a throwaway demo account**. The login is read from the environment variables `ODOO_DEMO_URL`, `ODOO_DEMO_LOGIN` and `ODOO_DEMO_PASSWORD`; a password is never typed into the chat, printed, logged or written to a file, and a password field is covered in the video.
- The demo **login name** is typed visibly in the lead-in (cut from the final video with `--cut`); use a demo account name, never a personal e-mail. Do not choose "always allow" for `py -3 *` / `python *` in Claude Code's permission prompt: an always-allowed interpreter could read the password variable without asking.
- A manual video shows whatever data is on the screen — never record a system that holds real customer data.
- The raw capture still contains the login frames: delete raw captures once the final video exists.

## Requirements

Python 3.10+, `playwright` + its Chromium, ffmpeg (a full build with libass and libx264), Claude Code. No other tool is needed.

## Tests

```
run-tests.cmd        # Windows
./run-tests.sh       # macOS / Linux
```

Each test file runs as its own script; real-browser files are skipped with a message when Playwright or Chromium is missing.

## License

MIT — see [LICENSE](LICENSE).
