# RegistrationBOT

Automates the CUFE SIS registration flow with Selenium + HTTP requests.

## What It Does

- Opens SIS login in Chrome and waits for manual login.
- Reuses authenticated session cookies for registration requests.
- Loads timetable XML and finds matching sections for a target course.
- Supports section filters by SIS period (`9:10`) or time range (`4-6`).
- Defaults to **dry-run** mode for safety.

## Requirements

- Python 3.10+
- Google Chrome installed
- Windows/macOS/Linux (Chrome + Selenium supported)

Install dependencies:

```bash
pip install -r requirements.txt
```

## Usage

Use schedule-plan as the visual planner:

```bash
python register.py --gui
```

This opens [schedule-plan](https://schedule-plan.pages.dev/). Build your schedule there, then return to the terminal and paste the Share link, or enter the schedule-plan student ID for a saved schedule. The script imports those selected sections and maps them to SIS `SchId`s after login.

The default `--gui` flow asks for your saved schedule-plan student ID. Press Enter at that prompt only if you want to paste a Share link instead.

Import directly from a schedule-plan Share link:

```bash
python register.py --schedule-plan-url "https://schedule-plan.pages.dev/?share=..."
```

Import a saved schedule-plan schedule:

```bash
python register.py --schedule-plan-student-id 1240002
```

Interactive dry-run (recommended first):

```bash
python register.py
```

or:

```bash
python register.py --interactive
```

After login, the script loads the timetable, shows available courses, then lets you choose section numbers from a list. Dry-run is the default, so the final registration request is not sent unless you add `--live`.

Interactive live mode:

```bash
python register.py --interactive --live
```

The script asks for confirmation before continuing in live mode.

Remember Student ID and final password locally:

```bash
python register.py --interactive --remember-credentials
```

On Windows, the password is protected with DPAPI for your Windows account and saved in `.registration_bot_credentials.json`. The file is ignored by git. To delete saved credentials:

```bash
python register.py --forget-credentials
```

CLI dry-run:

```bash
python register.py --course CMPS211 --add-section "Sunday 4-6" --add-section "Monday 2-4"
```

CLI live mode (actually submits registration):

```bash
python register.py --course CMPS211 --add-section "Sunday 4-6" --add-section "Monday 2-4" --live
```

## Section Format

`--add-section` accepts:

- SIS period format: `Day 9:10`
- Time range format: `Day 4-6` or `Day 4:00-6:00`

The script logs both SIS slot and real time (example: `9:10 (4:00-5:50)`).

## Safety Notes

- No password is stored in source code.
- Student GUID/stdid is auto-detected after login when possible.
- If auto-detection fails, the script asks for manual input.
- Keep generated debug/capture files private; `.gitignore` blocks common sensitive artifacts.

## Repository Layout

- `register.py`: Main automation script
- `requirements.txt`: Python dependencies
- `.gitignore`: Excludes sensitive/generated files

## Disclaimer

Use this script only on accounts and systems you are authorized to access, and only in compliance with your institution policies.
