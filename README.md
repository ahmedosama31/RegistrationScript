# RegistrationBOT

Automates the CUFE SIS registration flow with Selenium + HTTP requests.

## What It Does

- Opens SIS login in Chrome and logs in with saved local credentials.
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

Optional: copy the example credentials file and edit it before running:

```bash
copy .registration_bot_credentials.example.json .registration_bot_credentials.json
```

If you skip this, the script asks for your SIS Student ID and password on first run and creates `.registration_bot_credentials.json` for you.

## Usage

Run the script:

```bash
python register.py
```

The script is now an interactive CLI. It asks what you want to do instead of requiring command-line flags. In modern Windows terminals, menus and confirmations use colors plus Up/Down arrow selection. If the terminal does not support that, the script falls back to typed input.

You can choose from:

- Open [schedule-plan](https://schedule-plan.pages.dev/), then import what you picked.
- Import a saved schedule-plan schedule by student ID.
- Type a course code and section filters before login.

During setup, the script also asks whether to preserve courses SIS might drop from
the payload, such as Industrial Training. Enter the course code once and it is
saved in `.registration_bot_credentials.json` for later runs.

The script is designed for the registration-opening moment: choose or import the
schedule first, confirm the plan, then let visible Chrome log in and keep checking
until registration opens. The bot will not start the registration-open polling loop
until after the pre-open confirmations are complete. While waiting, it checks about
once per second.

### Typing course and filters before login

This option prompts before opening SIS:

```text
Enter target course code, for example CMPS211:
Section filter:
```

Enter one section filter per line, such as `Sunday 4-6` or `Monday 9:10`, then press Enter on a blank line when done. Use this when you already know the exact course and section times.

Dry-run is the default and recommended first. For Yes/No confirmations, use the
Up/Down arrows to highlight Yes or No, then press Enter. You can also press `y`
or `n` directly. For numbered menus, use Up/Down and Enter or press the number.
When asked "Send the real final registration request? Choose No for dry-run.",
choose No for a dry run. To actually submit registration, choose Yes; the script
asks for a second confirmation before continuing.
Before opening SIS, it also shows a pre-open plan review and asks you to confirm
the exact schedule plan, confirm that visible Chrome should start waiting, and,
in live mode, confirm live mode again. After registration opens, the script maps
your planned sections to SIS, prints the final selected sections and raw payload
string, then asks again before submitting anything.

The first time you run the CLI, it asks for your SIS Student ID and password. It saves them in `.registration_bot_credentials.json`, which is an editable JSON file ignored by git. After that, the bot reuses those credentials until you edit the file. Keep that file private because it contains your password.

When importing a saved schedule from schedule-plan, the bot uses the same saved Student ID from `.registration_bot_credentials.json`.

## Section Format

When typing section filters manually, use:

- SIS period format: `Day 9:10`
- Time range format: `Day 4-6` or `Day 4:00-6:00`

The script logs both SIS slot and real time (example: `9:10 (4:00-5:50)`).

## Safety Notes

- No password is stored in source code.
- Student GUID/stdid is auto-detected after login when possible.
- If auto-detection fails, the script asks for manual input.
- If SIS says registration is closed, the script keeps visible Chrome open in front of you and presses "Check if it is open now" or refreshes until the timetable page becomes visible. Press `Ctrl+C` to cancel the wait.
- Existing SIS selections are always preserved in the final selection payload so the bot does not deselect courses you already had selected.
- The setup prompt can save per-user `force_preserve_course_codes` in `.registration_bot_credentials.json` for courses SIS shows as visually selected while the timetable XML reports `Selected="0"`.
- After the final registration request, the script saves the SIS result page to `debug_final_registration_response.html` and prints whether course codes appear in the returned schedule/status text.
- In live mode, the final registration request is submitted through the visible Chrome window so the SIS website itself stays open on the final schedule/status page.
- Keep generated debug/capture files private; `.gitignore` blocks common sensitive artifacts.

## Repository Layout

- `register.py`: Main automation script
- `requirements.txt`: Python dependencies
- `.registration_bot_credentials.example.json`: Placeholder credentials/config template
- `.gitignore`: Excludes sensitive/generated files

## Making a Release

For a simple GitHub release:

1. Make sure `python -m py_compile register.py` passes.
2. Create a version tag, for example:

```bash
git tag v0.1.0
git push origin v0.1.0
```

3. On GitHub, create a release from that tag and attach a zip containing:

- `register.py`
- `requirements.txt`
- `.registration_bot_credentials.example.json`
- `README.md`

Users can download the zip, install requirements, edit the example credentials file if they want, and run `python register.py`.

For a more polished Windows release later, package it with PyInstaller:

```bash
pip install pyinstaller
pyinstaller --onefile register.py
```

Then attach `dist/register.exe` plus the example credentials file to the GitHub release.

## Disclaimer

Use this script only on accounts and systems you are authorized to access, and only in compliance with your institution policies.
