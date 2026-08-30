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

Two additional entry points use the same selection and safety logic while changing
how SIS is controlled:

```bash
# No Chrome/Selenium: login, polling, selection, and final registration use HTTP.
python register_api.py

# Chrome visibly scrolls to and clicks every newly selected lecture/tutorial.
python register_visible.py
```

`register_api.py` talks directly to the SIS Ext.NET HTTP endpoints. It does not
launch a browser, and its section-source menu therefore offers saved schedule-plan
import or manual filters rather than opening schedule-plan. `register_visible.py`
keeps each clicked item outlined during the fast visible selection pass, then verifies
that Chrome's selected `SchId` values exactly match the intended payload. It stops if
the website removes a selection because of a conflict. At startup, the student chooses
whether the program stops after selection for a manual Chrome handoff, or continues
through the automated registration flow.

Prebuilt Windows versions are written to `dist/` as `registrationscript.exe`,
`registrationscript-lite.exe`, `registrationscript-api.exe`, and
`registrationscript-visible.exe`.

The script is now an interactive CLI. It asks what you want to do instead of requiring command-line flags. In modern Windows terminals, menus and confirmations use colors plus Up/Down arrow selection. If the terminal does not support that, the script falls back to typed input.

You can choose from:

- Open [schedule-plan](https://schedule-plan.pages.dev/), then import what you picked.
- Import a saved schedule-plan schedule by student ID.
- Type a course code and section filters before login.

The script is designed for the registration-opening moment: choose or import the
schedule first, then let visible Chrome log in and keep checking until registration
opens. While waiting, it checks about once per second.

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
shows the plan once before opening SIS, then only asks before submitting the
selected lectures and before the final live request.
The final SIS review groups the selected courses and shows their chosen sections,
whether they came from schedule-plan or manual entry.

The first time you run the CLI, it asks for your SIS Student ID and password. It saves them in `.registration_bot_credentials.json`, which is an editable JSON file ignored by git. After that, the bot reuses those credentials until you edit the file. Keep that file private because it contains your password.

When importing a saved schedule from schedule-plan, the bot uses the same saved Student ID from `.registration_bot_credentials.json`.

## Section Format

When typing section filters manually, use:

- SIS period format: `Day 9:10`
- Time range format: `Day 4-6` or `Day 4:00-6:00`

The script logs both SIS slot and real time (example: `9:10 (4:00-5:50)`).
When schedule-plan contains two tutorials at the same day and time, the importer
uses the section group number to select the matching SIS tutorial.

## Safety Notes

- No password is stored in source code.
- Student GUID/stdid is auto-detected after login when possible.
- HTTPS requests use the native Windows certificate store when available, matching
  Chrome's trust decisions for schedule-plan and other HTTPS endpoints.
- If auto-detection fails, the script asks for manual input.
- If SIS says registration is closed, the script keeps visible Chrome open in front of you and presses "Check if it is open now" or refreshes until the timetable page becomes visible. Press `Ctrl+C` to cancel the wait.
- Existing SIS selections are always preserved in the final selection payload so the bot does not deselect courses you already had selected.
- After the final registration request, the script saves the SIS result page to `debug_final_registration_response.html` and prints whether course codes appear in the returned schedule/status text.
- In live mode, the final registration request is submitted through the visible Chrome window so the SIS website itself stays open on the final schedule/status page.
- Keep generated debug/capture files private; `.gitignore` blocks common sensitive artifacts.

## Repository Layout

- `register.py`: Main automation script
- `register_api.py`: Direct HTTP/API variant (no Chrome/Selenium launch)
- `register_visible.py`: Visible Chrome-clicking variant
- `RegistrationBOT-api.spec`: PyInstaller build for the API executable
- `RegistrationBOT-visible.spec`: PyInstaller build for the visible-click executable
- `requirements.txt`: Python dependencies
- `.registration_bot_credentials.example.json`: Placeholder credentials/config template
- `.gitignore`: Excludes sensitive/generated files

## Making a Release

Pushing a version tag automatically builds and publishes four individual Windows
executables through `.github/workflows/release.yml`:

| Release file | CAPTCHA | Purpose |
|---|---|---|
| `registrationscript.exe` | Automatic OCR plus SIS validation | Standard Chrome-assisted registration flow. |
| `registrationscript-lite.exe` | Manual entry plus SIS validation | Smaller standard build without the OCR model. |
| `registrationscript-visible.exe` | Automatic OCR when continuing; manual when handed off | Rapidly clicks and highlights every lecture/tutorial, then follows the student's startup choice: manual handoff or automatic continuation. |
| `registrationscript-api.exe` | Automatic OCR plus SIS validation | Direct SIS HTTP/Ext.NET flow without opening Chrome. |

The packaged EXEs include Python and the Python libraries they use, so friends do
not need Python, pip, or `requirements.txt`. They do need Windows 10/11, Google
Chrome installed, and internet access to SIS. ChromeDriver is handled automatically
when the EXE starts; the first run may download it.

To publish a release:

1. Make sure `python -m py_compile register.py` passes and push the code.
2. Create a version tag, for example:

```bash
git tag v0.1.0
git push origin v0.1.0
```

3. GitHub Actions builds all four EXEs and attaches them directly to the release.

For source usage instead of a packaged release, install `requirements.txt` and run
`python register.py`. The optional `ddddocr` package enables automatic CAPTCHA
solving; install it with `pip install -r requirements-ocr.txt`. Without it, the
script asks for the CAPTCHA manually.

## Disclaimer

Use this script only on accounts and systems you are authorized to access, and only in compliance with your institution policies.
