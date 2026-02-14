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

Dry-run (recommended first):

```bash
python register.py --course CMPS211 --add-section "Sunday 4-6" --add-section "Monday 2-4"
```

Live mode (actually submits registration):

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
