import time
import re
import json
import logging
import sys
import os
import base64
import ctypes
import getpass
import webbrowser
import html as html_lib
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from urllib.parse import urljoin, urlparse, parse_qs
from PIL import Image
from io import BytesIO

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

BASE_URL = "https://std.eng.cu.edu.eg"
REGISTRATION_PATH = "/SIS/Modules/MetaLoader.aspx?path=~/SIS/Modules/Student/Registration/Registration.ascx"
XML_HANDLER_PATH = "/SIS/Modules/MyXMLHandler.ashx"
CAPTCHA_PATH = "/SIS/Modules/CaptchaImage.aspx"
SCHEDULE_PLAN_URL = "https://schedule-plan.pages.dev/"
SCHEDULE_PLAN_API_BASE = "https://schedule-plan.pages.dev/api"
REGISTRATION_CHECK_INTERVAL_SECONDS = 5

# Optional local fallback if dynamic extraction fails.
# Keep empty in shared code; provide interactively when prompted.
MANUAL_STD_ID_GUID = ""
SLOT_TIME_BOUNDS = "8:00,8:50,9:00,9:50,10:00,10:50,11:00,11:50,12:00,12:50,1:00,1:50,2:00,2:50,3:00,3:50,4:00,4:50,5:00,5:50,6:00,6:50,7:00,7:50,8:00,8:50,9:00,9:50"


def build_slot_time_map(bounds_csv):
    """Map SIS slot number -> (start_time, end_time)."""
    items = [x.strip() for x in bounds_csv.split(",") if x.strip()]
    slot_map = {}
    for i in range(0, len(items), 2):
        slot = (i // 2) + 1
        if i + 1 < len(items):
            slot_map[slot] = (items[i], items[i + 1])
    return slot_map


SLOT_TIME_MAP = build_slot_time_map(SLOT_TIME_BOUNDS)
CREDENTIALS_FILE = ".registration_bot_credentials.json"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _to_blob(data):
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def protect_secret(secret):
    """Encrypt a secret with Windows DPAPI for the current Windows user."""
    if os.name != "nt":
        raise RuntimeError("Password remembering is only supported on Windows.")
    data = secret.encode("utf-8")
    in_blob, in_buffer = _to_blob(data)
    out_blob = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)
        del in_buffer


def unprotect_secret(protected):
    """Decrypt a Windows DPAPI-protected secret."""
    if os.name != "nt":
        raise RuntimeError("Password remembering is only supported on Windows.")
    encrypted = base64.b64decode(protected.encode("ascii"))
    in_blob, in_buffer = _to_blob(encrypted)
    out_blob = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        raise ctypes.WinError()
    try:
        decrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return decrypted.decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)
        del in_buffer


class CredentialStore:
    def __init__(self, path=CREDENTIALS_FILE, remember=False):
        self.path = path
        self.remember = remember
        self.data = self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.warning(f"Could not read saved credentials: {exc}")
            return {}

    def _save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)

    def forget(self):
        self.data = {}
        if os.path.exists(self.path):
            os.remove(self.path)
        logger.info("Forgot saved SIS credentials.")

    def get_user_id(self):
        return self.data.get("user_id")

    def get_force_preserve_course_codes(self):
        codes = self.data.get("force_preserve_course_codes") or []
        if isinstance(codes, str):
            codes = [codes]
        return {normalize_course_code(code) for code in codes if normalize_course_code(code)}

    def save_user_id(self, user_id):
        if not self.remember or not user_id:
            return
        self.data["user_id"] = user_id
        self._save()

    def get_password(self):
        password = self.data.get("password")
        if password:
            return password

        protected = self.data.get("password_dpapi")
        if not protected:
            return None
        try:
            return unprotect_secret(protected)
        except Exception as exc:
            logger.warning(f"Could not decrypt saved SIS password: {exc}")
            return None

    def save_password(self, password):
        if not self.remember or not password:
            return
        self.data["password"] = password
        self.data.pop("password_dpapi", None)
        self._save()

    def ensure_login_credentials(self):
        changed = False
        user_id = self.get_user_id()
        if not user_id:
            user_id = input("Enter your SIS Student ID: ").strip()
            self.data["user_id"] = user_id
            changed = True

        password = self.get_password()
        if not password:
            password = getpass.getpass("Enter your SIS Password: ").strip()
            self.data["password"] = password
            self.data.pop("password_dpapi", None)
            changed = True

        if changed:
            self._save()
            logger.info(f"Saved credentials to editable file: {os.path.abspath(self.path)}")

        return user_id, password


def period_to_time_range(period):
    """Convert SIS period notation like 9:10 into human time range."""
    m = re.fullmatch(r"(\d+):(\d+)", (period or "").strip())
    if not m:
        return None
    start_slot = int(m.group(1))
    end_slot = int(m.group(2))
    if start_slot > end_slot:
        return None
    if start_slot not in SLOT_TIME_MAP or end_slot not in SLOT_TIME_MAP:
        return None
    return SLOT_TIME_MAP[start_slot][0], SLOT_TIME_MAP[end_slot][1]


def format_period_with_time(period):
    rng = period_to_time_range(period)
    if not rng:
        return period
    return f"{period} ({rng[0]}-{rng[1]})"


def normalize_course_code(code):
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def parse_yes_no(prompt, default=False):
    suffix = " [Y/n]: " if default else " [y/N]: "
    answer = input(prompt + suffix).strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def prompt_choice(prompt, choices):
    """Show a numbered menu and return the selected choice key."""
    print()
    print(prompt)
    for idx, (_, label) in enumerate(choices, start=1):
        print(f"  {idx}. {label}")

    while True:
        raw = input("Choose an option: ").strip()
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(choices):
                return choices[idx - 1][0]
        print(f"Please enter a number from 1 to {len(choices)}.")


def parse_number_list(raw, max_value):
    """
    Accept comma-separated numbers and ranges like: 1,3,5-7
    Returns sorted zero-based indexes.
    """
    selected = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            if not start_text.strip().isdigit() or not end_text.strip().isdigit():
                raise ValueError(f"Invalid range: {part}")
            start = int(start_text)
            end = int(end_text)
            if start > end:
                raise ValueError(f"Invalid range: {part}")
            nums = range(start, end + 1)
        else:
            if not part.isdigit():
                raise ValueError(f"Invalid number: {part}")
            nums = [int(part)]

        for num in nums:
            if num < 1 or num > max_value:
                raise ValueError(f"Selection out of range: {num}")
            selected.add(num - 1)

    return sorted(selected)


def normalize_period_input(period_text):
    """
    Accept:
      - SIS slots: '9:10'
      - explicit clock range: '4-6', '4:00-6:00', '4 to 6'
    Returns: (normalized_period, note_or_none)
    """
    raw = (period_text or "").strip()
    if not raw or raw == "*":
        return raw, None

    # Native SIS period format.
    if re.fullmatch(r"\d+:\d+", raw):
        return raw, None

    normalized = re.sub(r"\s+to\s+", "-", raw, flags=re.IGNORECASE).replace(" ", "")
    m = re.fullmatch(r"(\d{1,2})(?::00)?-(\d{1,2})(?::00)?", normalized)
    if not m:
        return raw, None

    start_hour = int(m.group(1))
    end_hour = int(m.group(2))
    if end_hour <= start_hour:
        return raw, None

    start_label = f"{start_hour}:00"
    end_label = f"{end_hour - 1}:50"

    start_candidates = [slot for slot, bounds in SLOT_TIME_MAP.items() if bounds[0] == start_label]
    end_candidates = [slot for slot, bounds in SLOT_TIME_MAP.items() if bounds[1] == end_label]

    # Avoid ambiguous mappings (e.g., 8:00 appears twice).
    if len(start_candidates) != 1 or len(end_candidates) != 1:
        return raw, None

    start_slot = start_candidates[0]
    end_slot = end_candidates[0]
    if start_slot > end_slot:
        return raw, None

    converted = f"{start_slot}:{end_slot}"
    note = f"Interpreted time range '{raw}' as SIS period '{converted}' ({start_label}-{end_label})."
    return converted, note


def parse_section_targets(section_texts):
    """
    Convert typed section filters into (day, period) tuples.
    Accepts values like 'Monday 2:3', 'Sunday 4-6', or 'Sunday 4:00-6:00'.
    """
    specific_sections = []
    for section_text in section_texts:
        parts = section_text.strip().split()
        if len(parts) >= 2:
            day = parts[0]
            period, note = normalize_period_input(parts[1])
            if note:
                logger.info(note)
            mapped = period_to_time_range(period)
            if mapped and re.fullmatch(r"\d+:\d+", period):
                logger.info(f"Section filter '{day} {period}' maps to actual time {mapped[0]}-{mapped[1]}.")
            specific_sections.append((day, period))
        else:
            logger.warning(f"Invalid section format: '{section_text}'. Expected 'Day Period'. Ignoring.")
    return specific_sections


def same_schedule_plan_session(left, right):
    return (
        left.get("day") == right.get("day")
        and left.get("startHour") == right.get("startHour")
        and left.get("endHour") == right.get("endHour")
        and left.get("location") == right.get("location")
    )


def merge_schedule_plan_sections(courses):
    """Mirror schedule-plan's frontend normalization for grouped section IDs."""
    normalized_courses = []
    for course in courses:
        sections_by_key = {}
        for section in course.get("sections", []):
            key = f"{section.get('courseCode')}|{section.get('type')}|{section.get('group')}"
            existing = sections_by_key.get(key)
            section_id = section.get("id")
            section_legacy_ids = section.get("legacyIds") or []

            if not existing:
                copied = section.copy()
                copied["legacyIds"] = list(dict.fromkeys(section_legacy_ids + ([section_id] if section_id else [])))
                copied["sessions"] = list(section.get("sessions") or [])
                sections_by_key[key] = copied
                continue

            legacy_ids = existing.get("legacyIds") or []
            existing["legacyIds"] = list(dict.fromkeys(legacy_ids + section_legacy_ids + ([section_id] if section_id else [])))
            for session in section.get("sessions") or []:
                if not any(same_schedule_plan_session(session, current) for current in existing.get("sessions") or []):
                    existing.setdefault("sessions", []).append(session)

        copied_course = course.copy()
        copied_course["sections"] = list(sections_by_key.values())
        normalized_courses.append(copied_course)
    return normalized_courses


def fetch_schedule_plan_courses():
    resp = requests.get(f"{SCHEDULE_PLAN_API_BASE}/courses", timeout=30)
    resp.raise_for_status()
    return merge_schedule_plan_sections(resp.json())


def decode_schedule_plan_share_url(url):
    parsed = urlparse(url.strip())
    share_values = parse_qs(parsed.query).get("share")
    if not share_values:
        raise ValueError("Share URL does not contain a ?share=... parameter.")
    encoded = share_values[0].replace(" ", "+")
    padding = "=" * (-len(encoded) % 4)
    decoded = base64.b64decode((encoded + padding).encode("ascii")).decode("utf-8")
    return json.loads(decoded)


def fetch_schedule_plan_saved_schedule(student_id, schedule_name=None):
    params = {"student_id": student_id}
    if schedule_name:
        params["schedule_name"] = schedule_name
    resp = requests.get(f"{SCHEDULE_PLAN_API_BASE}/schedules", params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("exists"):
        raise ValueError(f"No saved schedule found for student ID {student_id}.")
    if data.get("protected"):
        raise ValueError("Saved schedule is protected and could not be loaded.")
    return json.loads(data["schedule_json"])


def normalize_schedule_plan_selections(raw_items):
    if isinstance(raw_items, dict):
        for key in ("courses", "selections", "selectedCourses", "schedule"):
            if isinstance(raw_items.get(key), list):
                raw_items = raw_items[key]
                break
        else:
            raw_items = [raw_items]

    selections = []
    for item in raw_items or []:
        course_code = item.get("courseCode") or item.get("c")
        if not course_code:
            continue
        selections.append({
            "courseCode": course_code,
            "selectedLectureId": item.get("selectedLectureId") or item.get("l"),
            "selectedTutorialId": item.get("selectedTutorialId") or item.get("t"),
            "selectedLabId": item.get("selectedLabId") or item.get("b"),
            "selectedMthsGroup": item.get("selectedMthsGroup") or item.get("m"),
            "selectedSectionIds": (
                item.get("selectedSectionIds")
                or item.get("sectionIds")
                or item.get("selectedSections")
                or item.get("sections")
                or item.get("ids")
            ),
        })
    return selections


def load_schedule_plan_selections(share_url=None, student_id=None, schedule_name=None):
    courses = fetch_schedule_plan_courses()
    if share_url:
        raw_items = decode_schedule_plan_share_url(share_url)
    elif student_id:
        raw_items = fetch_schedule_plan_saved_schedule(student_id, schedule_name=schedule_name)
    else:
        raise ValueError("Provide a schedule-plan share URL or student ID.")

    selections = normalize_schedule_plan_selections(raw_items)
    return expand_schedule_plan_sections(selections, courses)


def format_schedule_plan_section(section):
    sessions = section.get("sessions") or []
    if sessions:
        session_labels = [
            f"{session.get('day')} {session.get('startString')}-{session.get('endString')}"
            for session in sessions
        ]
        session_text = ", ".join(session_labels)
    else:
        session_text = "no session time"
    return (
        f"{section.get('courseCode')} | {section.get('type')} | "
        f"group {section.get('group')} | {session_text} | id={section.get('id')}"
    )


def log_schedule_plan_preview(planned_sections, source_label):
    logger.info("=" * 72)
    logger.info(f"schedule-plan import preview from {source_label}")
    logger.info(f"Selected sections imported: {len(planned_sections)}")
    course_codes = sorted({section.get("courseCode") for section in planned_sections if section.get("courseCode")})
    logger.info(f"Courses imported ({len(course_codes)}): {', '.join(course_codes) if course_codes else 'none'}")
    for idx, section in enumerate(planned_sections, start=1):
        logger.info(f"  {idx:>2}. {format_schedule_plan_section(section)}")
    logger.info("=" * 72)


def expand_schedule_plan_sections(selections, courses):
    course_by_code = {course.get("code"): course for course in courses}
    planned_sections = []
    missing = []

    def section_matches_id(section, section_id):
        return section.get("id") == section_id or section_id in (section.get("legacyIds") or [])

    for selection in selections:
        course_code = selection["courseCode"]
        course = course_by_code.get(course_code)
        if not course:
            missing.append(f"{course_code}: course not found")
            continue

        sections = course.get("sections", [])
        if selection.get("selectedMthsGroup"):
            group = selection["selectedMthsGroup"]
            group_sections = [section for section in sections if str(section.get("group")) == str(group)]
            if not group_sections:
                missing.append(f"{course_code}: group {group} not found")
            planned_sections.extend(group_sections)
            continue

        selected_ids = [
            selection.get("selectedLectureId"),
            selection.get("selectedTutorialId"),
            selection.get("selectedLabId"),
        ]
        if isinstance(selection.get("selectedSectionIds"), list):
            selected_ids.extend(selection["selectedSectionIds"])
        elif selection.get("selectedSectionIds"):
            selected_ids.append(selection["selectedSectionIds"])

        for section_id in [sid for sid in selected_ids if sid]:
            section = next((section for section in sections if section_matches_id(section, section_id)), None)
            if section:
                planned_sections.append(section)
            else:
                missing.append(f"{course_code}: section {section_id} not found")

    if missing:
        logger.warning("Some schedule-plan sections could not be found: " + "; ".join(missing))
    return planned_sections


def normalize_section_type(value):
    text = (value or "").strip().lower()
    if "lec" in text:
        return "lecture"
    if "tut" in text or "sec" in text:
        return "tutorial"
    if "lab" in text or "laboratory" in text:
        return "lab"
    return text


def decimal_time_from_text(value):
    if not value:
        return None
    hour_text, minute_text = value.split(":", 1)
    hour = int(hour_text)
    minute = int(minute_text)
    if hour < 8:
        hour += 12
    return hour + minute / 60


def planned_session_times(section):
    sessions = section.get("sessions") or []
    if not sessions:
        return []
    times = []
    for session in sessions:
        start = decimal_time_from_text(session.get("startString"))
        end = decimal_time_from_text(session.get("endString"))
        if start is not None and end is not None:
            times.append((session.get("day"), start, end))
    return times


def sis_lecture_time(lecture):
    rng = period_to_time_range(lecture.get("period"))
    if not rng:
        return None
    return decimal_time_from_text(rng[0]), decimal_time_from_text(rng[1])


def times_close(a, b, tolerance=0.02):
    return abs(a - b) <= tolerance


def build_schedule_plan_mapping(planned_sections, sis_lectures):
    results = []

    for section in planned_sections:
        course_code = section.get("courseCode")
        section_type = normalize_section_type(section.get("type"))
        sessions = planned_session_times(section)
        section_matches = []
        section_errors = []
        matched_any = False

        for day, start, end in sessions:
            candidates = []
            for lecture in sis_lectures:
                lecture_time = sis_lecture_time(lecture)
                if not lecture_time:
                    continue
                sis_start, sis_end = lecture_time
                if (
                    course_code
                    and course_code.upper() in (lecture.get("code") or "").upper()
                    and normalize_section_type(lecture.get("type")) == section_type
                    and (day or "").lower() in (lecture.get("day") or "").lower()
                    and times_close(start, sis_start)
                    and times_close(end, sis_end)
                ):
                    candidates.append(lecture)

            if candidates:
                unique_schids = [c.get("sch_id") for c in candidates if c.get("sch_id")]
                if len(set(unique_schids)) > 1:
                    section_errors.append(
                        f"{course_code} {section.get('type')} {day} is ambiguous "
                        f"(matches SchIds: {', '.join(unique_schids)})"
                    )
                    continue
                for candidate in candidates:
                    sch_id = candidate.get("sch_id")
                    if sch_id:
                        section_matches.append(candidate)
                matched_any = True

        if not matched_any:
            section_errors.append(
                f"{course_code} {section.get('type')} "
                f"{', '.join(f'{d} {s}-{e}' for d, s, e in sessions) or section.get('id')}"
            )

        results.append({
            "section": section,
            "matches": section_matches,
            "errors": section_errors,
        })

    return results


def log_schedule_plan_mapping_report(mapping_results):
    logger.info("=" * 72)
    logger.info("schedule-plan -> SIS mapping report")
    selected_schids = []
    has_errors = False

    for idx, result in enumerate(mapping_results, start=1):
        section = result["section"]
        matches = result["matches"]
        errors = result["errors"]
        logger.info(f"  {idx:>2}. {format_schedule_plan_section(section)}")
        if matches:
            for match in matches:
                sch_id = match.get("sch_id")
                if sch_id and sch_id not in selected_schids:
                    selected_schids.append(sch_id)
                logger.info(
                    "      MATCH -> "
                    f"{match.get('code')} {match.get('type')} {match.get('day')} "
                    f"{match.get('period_label')} SchId={sch_id}"
                )
        if errors:
            has_errors = True
            for error in errors:
                logger.error(f"      NO MATCH -> {error}")

    logger.info(f"Mapped SIS SchIds ({len(selected_schids)}): {', '.join(selected_schids) if selected_schids else 'none'}")
    logger.info("=" * 72)
    return selected_schids, has_errors


def map_schedule_plan_sections_to_sis(planned_sections, sis_lectures):
    mapping_results = build_schedule_plan_mapping(planned_sections, sis_lectures)
    selected_schids, has_errors = log_schedule_plan_mapping_report(mapping_results)
    if has_errors:
        return None

    return selected_schids

def _find_login_user_field(driver):
    fields = driver.find_elements(By.CSS_SELECTOR, "input[type='text'], input:not([type])")
    preferred = []
    fallback = []
    for field in fields:
        if not field.is_displayed() or not field.is_enabled():
            continue
        name = (field.get_attribute("name") or "").lower()
        field_id = (field.get_attribute("id") or "").lower()
        if any(token in name or token in field_id for token in ["user", "login", "student", "id"]):
            preferred.append(field)
        fallback.append(field)
    return (preferred or fallback)[0] if (preferred or fallback) else None


def _find_login_submit_button(driver):
    buttons = driver.find_elements(By.CSS_SELECTOR, "input[type='submit'], button[type='submit'], button")
    preferred = []
    fallback = []
    for button in buttons:
        if not button.is_displayed() or not button.is_enabled():
            continue
        label = " ".join(
            [
                button.get_attribute("value") or "",
                button.text or "",
                button.get_attribute("name") or "",
                button.get_attribute("id") or "",
            ]
        ).lower()
        if any(token in label for token in ["login", "log in", "sign in", "submit", "enter"]):
            preferred.append(button)
        fallback.append(button)
    return (preferred or fallback)[0] if (preferred or fallback) else None


def _page_text_from_html(html):
    soup = BeautifulSoup(html, "html.parser")
    return " ".join(soup.get_text(" ", strip=True).split())


def _is_registration_closed_html(html):
    text = _page_text_from_html(html).lower()
    if "registration is closed" in text:
        return True

    soup = BeautifulSoup(html, "html.parser")
    for element in soup.find_all(["input", "button"]):
        label = (element.get("value") or element.get_text(" ", strip=True) or "").lower()
        if "check" in label and "open" in label:
            return True
    return False


def _looks_like_registration_table_html(html):
    if _is_registration_closed_html(html):
        return False
    return (
        "MyXMLHandler.ashx" in html
        or 'name="ctl07$nextPage"' in html
        or 'name="StdSelectedLecs"' in html
        or 'id="ctl07_nextPage"' in html
    )


def _element_label(element):
    return " ".join(
        [
            element.get_attribute("value") or "",
            element.text or "",
            element.get_attribute("name") or "",
            element.get_attribute("id") or "",
        ]
    ).strip()


def _find_visible_button_by_label(driver, required_words):
    for element in driver.find_elements(By.CSS_SELECTOR, "input[type='submit'], input[type='button'], button"):
        if not element.is_displayed() or not element.is_enabled():
            continue
        label = _element_label(element).lower()
        if all(word in label for word in required_words):
            return element, label.strip()
    return None, None


def _check_visible_agreement_checkbox(driver):
    checkboxes = driver.find_elements(By.CSS_SELECTOR, "input[type='checkbox']")
    for checkbox in checkboxes:
        if not checkbox.is_displayed() or not checkbox.is_enabled():
            continue
        checked = checkbox.is_selected()
        if not checked:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", checkbox)
            driver.execute_script("arguments[0].click();", checkbox)
            logger.info("Checked the registration approval checkbox in Chrome.")
            time.sleep(0.5)
        return True
    return False


def _click_approval_next_if_available(driver):
    approval_checkbox_seen = _check_visible_agreement_checkbox(driver)
    buttons = driver.find_elements(By.CSS_SELECTOR, "input[type='submit'], input[type='button'], button")
    for button in buttons:
        if not button.is_displayed():
            continue
        label = _element_label(button).lower()
        if "next" not in label:
            continue
        if not button.is_enabled():
            logger.info("Approval Next button is still disabled after checking the checkbox.")
            time.sleep(1)
            return True
        logger.info(f"Clicking approval button '{label or 'Next'}' in Chrome.")
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
        driver.execute_script("arguments[0].click();", button)
        return True
    if approval_checkbox_seen:
        logger.info("Approval checkbox was found, but Next button was not ready yet.")
        time.sleep(1)
        return True
    return False


def wait_for_registration_to_open_in_browser(driver):
    """Use visible Chrome to refresh/click until SIS shows the registration table."""
    registration_url = urljoin(BASE_URL, REGISTRATION_PATH)
    logger.info("Opening registration page in visible Chrome.")
    driver.get(registration_url)
    attempt = 1

    while True:
        html = driver.page_source

        if _looks_like_registration_table_html(html):
            logger.info("Registration timetable is visible in Chrome.")
            with open("debug_registration_open_browser.html", "w", encoding="utf-8") as f:
                f.write(html)
            logger.info("Saved visible browser registration page to 'debug_registration_open_browser.html'.")
            return html

        if _click_approval_next_if_available(driver):
            time.sleep(1)
            continue

        check_button, check_label = _find_visible_button_by_label(driver, ["check", "open"])
        if check_button:
            logger.info(
                f"Registration is not open yet. Clicking '{check_label or 'Check if it is open now'}' "
                f"in Chrome (attempt {attempt}). Press Ctrl+C to cancel."
            )
            driver.execute_script("arguments[0].click();", check_button)
        else:
            logger.info(
                f"Registration table is not visible yet. Refreshing registration page in Chrome "
                f"(attempt {attempt}). Press Ctrl+C to cancel."
            )
            driver.refresh()

        attempt += 1
        time.sleep(REGISTRATION_CHECK_INTERVAL_SECONDS)


def login_with_selenium(user_id, password):
    """Use Selenium to handle login and extract session cookies."""
    logger.info("Launching visible Selenium Chrome for login and registration-open checking...")
    options = webdriver.ChromeOptions()
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    
    try:
        driver.get(BASE_URL)
        logger.info("Logging in with saved SIS credentials...")

        password_field = WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password']"))
        )
        user_field = _find_login_user_field(driver)
        if user_field is None:
            logger.error("Could not find the SIS Student ID login field.")
            return None, None, None, driver

        user_field.clear()
        user_field.send_keys(user_id)
        password_field.clear()
        password_field.send_keys(password)

        submit_button = _find_login_submit_button(driver)
        if submit_button:
            submit_button.click()
        else:
            password_field.submit()

        logger.info("Waiting for login to complete (redirect to /SIS/Default.aspx)...")
        
        WebDriverWait(driver, 120).until(
            EC.url_contains("/SIS/Default.aspx")
        )
        logger.info("Login detected.")

        registration_html = wait_for_registration_to_open_in_browser(driver)

        logger.info("Extracting cookies from visible Chrome...")
        selenium_cookies = driver.get_cookies()
        session_cookies = {}
        for cookie in selenium_cookies:
            session_cookies[cookie['name']] = cookie['value']
            
        logger.info(f"Extracted {len(session_cookies)} cookies.")
        
        page_source = registration_html or driver.page_source
        
        # GUID Pattern 1: image.aspx?FileName=GUID
        # GUID Pattern 2: stdid=GUID (common in AJAX calls)
        std_guid = None
        
        guid_match = re.search(r'FileName=([a-f0-9\-]{36})', page_source)
        if guid_match:
            std_guid = guid_match.group(1)
            logger.info(f"Found Student GUID (Pattern 1): {std_guid}")
        else:
            guid_match_2 = re.search(r'stdid=([a-f0-9\-]{36})', page_source)
            if guid_match_2:
                std_guid = guid_match_2.group(1)
                logger.info(f"Found Student GUID (Pattern 2): {std_guid}")
            
        return session_cookies, std_guid, registration_html, driver
        
    except Exception as e:
        logger.error(f"Login failed or timed out: {e}")
        return None, None, None, driver
    finally:
        logger.info("Leaving Chrome open so you can inspect what happened.")

class RegistrationClient:
    def __init__(
        self,
        cookies,
        std_guid=None,
        dry_run=True,
        credential_store=None,
        registration_ready=False,
        registration_html=None,
        browser_driver=None,
    ):
        self.session = requests.Session()
        self.session.cookies.update(cookies)
        self.dry_run = dry_run
        self.credential_store = credential_store
        self.viewstate = None
        self.eventvalidation = None
        self.viewstategenerator = None
        self.std_guid = std_guid
        self.user_id = None
        self.selected_lectures = []
        self.selected_lecture_details = []
        self.existing_selected_details = []
        self.force_preserved_details = []
        self.new_selected_details = []
        self.stdid_candidates = []
        self.registration_table_ready = registration_ready
        self.registration_html = registration_html
        self.browser_driver = browser_driver
        self.force_preserve_course_codes = set()
        if std_guid:
            self._add_stdid_candidate(std_guid)
        if credential_store:
            self.user_id = credential_store.get_user_id()
            self.force_preserve_course_codes = credential_store.get_force_preserve_course_codes()

        if registration_html:
            soup = BeautifulSoup(registration_html, "html.parser")
            if self._update_page_state(soup, "visible browser registration page"):
                self._extract_identifiers_from_html(registration_html, source="visible browser registration page")

    def _add_stdid_candidate(self, stdid):
        """Store possible stdid values while preserving insertion order."""
        if not stdid:
            return
        stdid = stdid.strip()
        if stdid and stdid not in self.stdid_candidates:
            self.stdid_candidates.append(stdid)

    def _extract_identifiers_from_html(self, html, source="page"):
        """
        Extract stdid/userName hints from registration HTML.
        The SIS pages often embed the exact MyXMLHandler URL in JS.
        """
        found_stdid = None
        found_user = None

        # Most reliable source once approval is accepted.
        loadxml_match = re.search(
            r"MyXMLHandler\.ashx\?stdid=([a-f0-9\-]+)&userName=(\d+)",
            html,
            re.IGNORECASE
        )
        if loadxml_match:
            found_stdid = loadxml_match.group(1)
            found_user = loadxml_match.group(2)

        # Fallback patterns.
        if not found_stdid:
            stdid_match = re.search(r"stdid=([a-f0-9\-]+)", html, re.IGNORECASE)
            if stdid_match:
                found_stdid = stdid_match.group(1)
        if not found_stdid:
            img_match = re.search(r"FileName=([a-f0-9\-]{36})", html, re.IGNORECASE)
            if img_match:
                found_stdid = img_match.group(1)

        if not found_user:
            user_match = re.search(r"userName=(\d+)", html)
            if user_match:
                found_user = user_match.group(1)
        if not found_user:
            # Seen in registration markup as: !isNaN('1234567')
            user_match = re.search(r"isNaN\('(\d+)'\)", html)
            if user_match:
                found_user = user_match.group(1)

        if found_stdid:
            self.std_guid = found_stdid
            self._add_stdid_candidate(found_stdid)
            logger.info(f"Found stdid from {source}: {found_stdid}")

        if found_user:
            self.user_id = found_user
            logger.info(f"Found User ID from {source}: {found_user}")
            if self.credential_store:
                self.credential_store.save_user_id(found_user)

    def _build_stdid_candidates(self):
        """Return ordered stdid candidates with common SIS variants."""
        candidates = []

        def add(val):
            if val and val not in candidates:
                candidates.append(val)

        add(self.std_guid)
        for c in self.stdid_candidates:
            add(c)

        # SIS sometimes uses GUID + trailing zero in MyXMLHandler calls.
        base_list = list(candidates)
        for c in base_list:
            if re.fullmatch(r"[a-f0-9\-]{36}", c, flags=re.IGNORECASE):
                add(c + "0")
            if re.fullmatch(r"[a-f0-9\-]{37}", c, flags=re.IGNORECASE) and c.endswith("0"):
                add(c[:-1])

        return candidates

    @staticmethod
    def _looks_like_html_error(text):
        lowered = text[:400].lower()
        return (
            "<html" in lowered
            or "<!doctype html" in lowered
            or "aspxerrorpath" in lowered
            or "an error has occurred" in lowered
        )

    def _update_page_state(self, soup, source):
        """Capture ASP.NET form state needed by the next postback."""
        required_fields = {
            "viewstate": "__VIEWSTATE",
            "viewstategenerator": "__VIEWSTATEGENERATOR",
        }
        optional_fields = {
            "eventvalidation": "__EVENTVALIDATION",
        }

        values = {}
        missing = []
        for attr, field_id in required_fields.items():
            element = soup.find("input", {"id": field_id})
            if not element or element.get("value") is None:
                missing.append(field_id)
            else:
                values[attr] = element.get("value")

        if missing:
            logger.error(f"Could not find required form fields after {source}: {', '.join(missing)}")
            return False

        self.viewstate = values["viewstate"]
        self.viewstategenerator = values["viewstategenerator"]

        for attr, field_id in optional_fields.items():
            element = soup.find("input", {"id": field_id})
            if element and element.get("value") is not None:
                setattr(self, attr, element.get("value"))
            else:
                logger.warning(
                    f"Optional form field {field_id} was not present after {source}; "
                    "keeping the previous value."
                )
        return True

    @staticmethod
    def _save_debug_html(filename, html):
        with open(filename, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info(f"Saved debug HTML to '{filename}'")

    @staticmethod
    def _log_page_text_preview(html, source):
        soup = BeautifulSoup(html, "html.parser")
        text = " ".join(soup.get_text(" ", strip=True).split())
        if text:
            logger.info(f"{source} text preview: {text[:500]}")

    @staticmethod
    def _page_text(html):
        soup = BeautifulSoup(html, "html.parser")
        return " ".join(soup.get_text(" ", strip=True).split())

    @staticmethod
    def _is_registration_closed_page(soup, html):
        text = RegistrationClient._page_text(html).lower()
        if "registration is closed" in text:
            return True

        for element in soup.find_all(["input", "button"]):
            label = (element.get("value") or element.get_text(" ", strip=True) or "").lower()
            if "check" in label and "open" in label:
                return True

        return False

    @staticmethod
    def _looks_like_registration_table_page(soup, html):
        if RegistrationClient._is_registration_closed_page(soup, html):
            return False
        if "MyXMLHandler.ashx" in html:
            return True
        return bool(
            soup.find("input", attrs={"name": "ctl07$nextPage"})
            or soup.find("input", attrs={"name": "StdSelectedLecs"})
            or soup.find("input", attrs={"id": "ctl07_nextPage"})
        )

    @staticmethod
    def _build_form_payload(soup):
        payload = {}
        for element in soup.find_all("input"):
            name = element.get("name")
            if not name:
                continue
            input_type = (element.get("type") or "").lower()
            if input_type in {"submit", "button", "image", "reset"}:
                continue
            payload[name] = element.get("value", "")
        return payload

    @staticmethod
    def _find_check_open_button(soup):
        for element in soup.find_all(["input", "button"]):
            label = (element.get("value") or element.get_text(" ", strip=True) or "").strip()
            if "check" in label.lower() and "open" in label.lower():
                return element, label
        return None, None

    @staticmethod
    def _form_post_url(soup, default_url):
        form = soup.find("form")
        action = form.get("action") if form else None
        if not action:
            return default_url
        return urljoin(default_url, action)

    def wait_for_registration_to_open(self, html):
        """Keep checking SIS until the registration timetable page is returned."""
        url = urljoin(BASE_URL, REGISTRATION_PATH)
        attempt = 1

        while True:
            try:
                soup = BeautifulSoup(html, "html.parser")
                self._extract_identifiers_from_html(html, source="registration wait page")

                if self._looks_like_registration_table_page(soup, html):
                    if self._update_page_state(soup, "registration opened page"):
                        self.registration_table_ready = True
                        logger.info("Registration is open. Timetable page is visible.")
                        return True
                    self._save_debug_html("debug_registration_open_response.html", html)
                    self._log_page_text_preview(html, "Registration opened response")
                    logger.warning("Registration looked open, but page state was incomplete. Checking again.")

                elif not self._is_registration_closed_page(soup, html):
                    if self._update_page_state(soup, "registration response"):
                        logger.info("Registration page is not closed; continuing.")
                        return True
                    self._save_debug_html("debug_registration_wait_response.html", html)
                    self._log_page_text_preview(html, "Registration wait response")
                    logger.warning("Unexpected registration response. Checking again instead of stopping.")

                else:
                    button, button_label = self._find_check_open_button(soup)
                    payload = None
                    post_url = url

                    if button is None:
                        logger.warning(
                            "Registration is closed, but the check button was not found. "
                            "Reloading the registration page instead."
                        )
                        self._save_debug_html("debug_registration_closed_response.html", html)
                        self._log_page_text_preview(html, "Registration closed response")
                    else:
                        payload = self._build_form_payload(soup)
                        button_name = button.get("name")
                        if button_name:
                            payload[button_name] = button.get("value", button_label)
                        else:
                            button_id = button.get("id")
                            if button_id:
                                payload["__EVENTTARGET"] = button_id.replace("_", "$")
                                payload["__EVENTARGUMENT"] = ""
                        post_url = self._form_post_url(soup, url)

                        logger.info(
                            f"Registration is still closed. Pressing '{button_label}' "
                            f"(attempt {attempt}); checking again in {REGISTRATION_CHECK_INTERVAL_SECONDS}s."
                        )

                    time.sleep(REGISTRATION_CHECK_INTERVAL_SECONDS)
                    if payload is None:
                        resp = self.session.get(url)
                    else:
                        resp = self.session.post(post_url, data=payload)

                    if resp.status_code != 200:
                        logger.warning(
                            f"Registration check returned status {resp.status_code}. "
                            f"Trying again in {REGISTRATION_CHECK_INTERVAL_SECONDS}s."
                        )
                        self._save_debug_html("debug_registration_check_response.html", resp.text)
                        time.sleep(REGISTRATION_CHECK_INTERVAL_SECONDS)
                        html = self.session.get(url).text
                        attempt += 1
                        continue

                    html = resp.text
                    attempt += 1
                    continue

                logger.info(
                    f"Waiting for registration to open "
                    f"(attempt {attempt}); checking again in {REGISTRATION_CHECK_INTERVAL_SECONDS}s. Press Ctrl+C to cancel."
                )
                time.sleep(REGISTRATION_CHECK_INTERVAL_SECONDS)
                resp = self.session.get(url)
                if resp.status_code == 200:
                    html = resp.text
                else:
                    logger.warning(f"Registration page reload returned status {resp.status_code}. Retrying.")
                attempt += 1

            except KeyboardInterrupt:
                logger.info("Cancelled while waiting for registration to open.")
                return False
            except Exception as exc:
                logger.warning(
                    f"Registration check failed with {exc.__class__.__name__}: {exc}. "
                    f"Trying again in {REGISTRATION_CHECK_INTERVAL_SECONDS}s. Press Ctrl+C to cancel."
                )
                try:
                    time.sleep(REGISTRATION_CHECK_INTERVAL_SECONDS)
                    resp = self.session.get(url)
                    if resp.status_code == 200:
                        html = resp.text
                except KeyboardInterrupt:
                    logger.info("Cancelled while waiting for registration to open.")
                    return False
                except Exception as retry_exc:
                    logger.warning(f"Retry reload also failed: {retry_exc}")
                attempt += 1

    def get_registration_page(self):
        """Fetch the registration page and extract VIEWSTATE."""
        if self.registration_table_ready and self.registration_html:
            logger.info("Using registration page state captured from visible Chrome.")
            soup = BeautifulSoup(self.registration_html, "html.parser")
            if not self._update_page_state(soup, "visible browser registration page"):
                return False
            self._extract_identifiers_from_html(self.registration_html, source="visible browser registration page")
            return True

        url = urljoin(BASE_URL, REGISTRATION_PATH)
        logger.info(f"Fetching registration page: {url}")
        
        resp = self.session.get(url)
        if resp.status_code != 200:
            logger.warning(
                f"Registration page returned status {resp.status_code}. "
                "Keeping the registration-open checker running."
            )
            return self.wait_for_registration_to_open(resp.text)
            
        soup = BeautifulSoup(resp.text, 'html.parser')
        if self._is_registration_closed_page(soup, resp.text):
            return self.wait_for_registration_to_open(resp.text)
        if self._looks_like_registration_table_page(soup, resp.text):
            return self.wait_for_registration_to_open(resp.text)
        if not self._update_page_state(soup, "registration page fetch"):
            return False
        self._extract_identifiers_from_html(resp.text, source="registration page")
             
        return True

    def accept_approval(self):
        """Step 1: Click 'Next' on the approval message."""
        if self.registration_table_ready:
            logger.info("Registration timetable is already visible; skipping approval message.")
            return True

        logger.info("Step 1: Accepting approval message...")
        url = urljoin(BASE_URL, REGISTRATION_PATH)
        
        payload = {
            '__EVENTTARGET': '',
            '__EVENTARGUMENT': '',
            '__VIEWSTATE': self.viewstate,
            '__VIEWSTATEGENERATOR': self.viewstategenerator,
            'ctl07$ButMessageShown': 'Next>>',
            'ctl07$HiddenField1': SLOT_TIME_BOUNDS,
            'BData': ''
        }
        if self.eventvalidation:
            payload['__EVENTVALIDATION'] = self.eventvalidation
        
        resp = self.session.post(url, data=payload)
        if resp.status_code != 200:
            logger.warning("Failed to accept approval. Keeping the registration-open checker running.")
            self._save_debug_html("debug_approval_response.html", resp.text)
            self._log_page_text_preview(resp.text, "Approval response")
            return self.wait_for_registration_to_open(resp.text)
            
        if not self.wait_for_registration_to_open(resp.text):
            self._save_debug_html("debug_approval_response.html", resp.text)
            self._log_page_text_preview(resp.text, "Approval response")
            return False
        return True

    def fetch_timetable(self):
        """Fetch and parse the XML timetable from MyXMLHandler."""
        logger.info("Fetching timetable XML...")
        if not self.user_id:
            saved_user_id = self.credential_store.get_user_id() if self.credential_store else None
            if saved_user_id:
                self.user_id = saved_user_id
                logger.info("Using saved Student ID.")
            else:
                logger.warning("Could not auto-detect User ID.")
                self.user_id = input("Enter Student ID (e.g. 1234567): ").strip()
                if self.credential_store:
                    self.credential_store.save_user_id(self.user_id)

        candidates = self._build_stdid_candidates()
        if not candidates:
            logger.warning("Could not auto-detect Student GUID/stdid.")
            if MANUAL_STD_ID_GUID:
                prompt = f"Enter Student GUID/stdid (default: {MANUAL_STD_ID_GUID}): "
            else:
                prompt = "Enter Student GUID/stdid: "
            manual = input(prompt).strip() or MANUAL_STD_ID_GUID
            if not manual:
                logger.error("Student GUID/stdid is required when auto-detection fails.")
                return False
            self.std_guid = manual
            self._add_stdid_candidate(manual)
            candidates = self._build_stdid_candidates()

        import random
        import xml.etree.ElementTree as ET

        root = None
        xml_content = None
        last_response_text = None
        last_url = None

        for stdid in candidates:
            t_val = str(random.randint(10000000, 99999999))
            xml_url = urljoin(BASE_URL, f"{XML_HANDLER_PATH}?stdid={stdid}&userName={self.user_id}&t={t_val}")
            logger.info(f"Trying XML URL: {xml_url}")

            resp = self.session.get(xml_url)
            if resp.status_code != 200:
                logger.warning(f"MyXMLHandler returned status {resp.status_code} for stdid={stdid}")
                continue

            candidate_xml = resp.text.replace('&nbsp;', ' ')
            last_response_text = candidate_xml
            last_url = xml_url

            if self._looks_like_html_error(candidate_xml):
                logger.warning(f"MyXMLHandler returned HTML error page for stdid={stdid}")
                continue

            try:
                parsed = ET.fromstring(candidate_xml)
            except Exception as e:
                logger.warning(f"MyXMLHandler parse failed for stdid={stdid}: {e}")
                continue

            root = parsed
            xml_content = candidate_xml
            self.std_guid = stdid
            self._add_stdid_candidate(stdid)
            logger.info(f"Using stdid={stdid} for timetable parsing.")
            break

        if root is None:
            logger.error("Failed to fetch a valid timetable XML from MyXMLHandler.")
            if last_url:
                logger.error(f"Last attempted XML URL: {last_url}")
            if last_response_text:
                with open('debug_timetable.xml', 'w', encoding='utf-8') as f:
                    f.write(last_response_text)
                logger.info("Saved failing MyXMLHandler response to 'debug_timetable.xml'")
                logger.error(f"Response preview: {last_response_text[:500]}")
            return None

        with open('debug_timetable.xml', 'w', encoding='utf-8') as f:
            f.write(xml_content)
        logger.info("Saved timetable response to 'debug_timetable.xml'")
        return root

    @staticmethod
    def collect_lectures(root):
        lectures = []
        for day in root:
            day_name = day.attrib.get('Name', '')
            for lecture in day:
                lectures.append({
                    "day": day_name,
                    "code": lecture.attrib.get('Code', ''),
                    "type": lecture.attrib.get('Type', ''),
                    "period": lecture.attrib.get('Period', ''),
                    "period_label": format_period_with_time(lecture.attrib.get('Period', '')),
                    "sch_id": lecture.attrib.get('SchId'),
                    "selected": lecture.attrib.get('Selected') == '1',
                })
        return lectures

    @staticmethod
    def collect_existing_selected(root):
        existing_selected = []
        for day in root:
            for lecture in day:
                sch_id = lecture.attrib.get('SchId')
                if lecture.attrib.get('Selected') == '1' and sch_id:
                    existing_selected.append(sch_id)
        return list(dict.fromkeys(existing_selected))

    @staticmethod
    def collect_existing_selected_details(lectures):
        return [lec for lec in lectures if lec["selected"] and lec["sch_id"]]

    def collect_force_preserved_details(self, lectures):
        preserve_codes = self.force_preserve_course_codes
        if not preserve_codes:
            return []
        return [
            lec for lec in lectures
            if lec["sch_id"] and normalize_course_code(lec["code"]) in preserve_codes
        ]

    @staticmethod
    def format_lecture_line(lecture):
        return (
            f"{lecture['code']} ({lecture['type']}) "
            f"{lecture['day']} {lecture['period_label']} [SchId={lecture['sch_id']}]"
        )

    def choose_course_interactively(self, lectures, initial_course=None):
        course_codes = sorted({lec["code"] for lec in lectures if lec["code"]})
        if not course_codes:
            logger.error("No course codes found in timetable.")
            return None

        if initial_course:
            matches = [code for code in course_codes if initial_course.upper() in code.upper()]
            if matches:
                return initial_course
            logger.warning(f"Course '{initial_course}' was not found in the timetable.")

        print("\nAvailable courses:")
        for idx, code in enumerate(course_codes, start=1):
            print(f"  {idx:>2}. {code}")

        while True:
            raw = input("\nChoose a course by number or type a course code: ").strip()
            if not raw:
                continue
            if raw.isdigit():
                idx = int(raw)
                if 1 <= idx <= len(course_codes):
                    return course_codes[idx - 1]
                print(f"Please enter a number from 1 to {len(course_codes)}.")
                continue
            matches = [code for code in course_codes if raw.upper() in code.upper()]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                print("Multiple matches: " + ", ".join(matches))
                continue
            print("No matching course found. Try again.")

    def choose_sections_interactively(self, lectures, target_course_code):
        matches = [
            lec for lec in lectures
            if target_course_code.upper() in lec["code"].upper()
        ]
        if not matches:
            logger.error(f"Course {target_course_code} not found in timetable!")
            return []

        print(f"\nSections for {target_course_code}:")
        for idx, lec in enumerate(matches, start=1):
            selected_marker = " [already selected]" if lec["selected"] else ""
            print(
                f"  {idx:>2}. {lec['code']} ({lec['type']}) "
                f"{lec['day']} {lec['period_label']} [SchId={lec['sch_id']}]{selected_marker}"
            )

        while True:
            raw = input("\nChoose section numbers (example: 1,3 or 2-4): ").strip()
            try:
                indexes = parse_number_list(raw, len(matches))
            except ValueError as exc:
                print(f"{exc}. Try again.")
                continue
            if indexes:
                return [matches[i]["sch_id"] for i in indexes]
            print("Please choose at least one section.")

    def set_selected_lectures(self, root, found_lectures, specific_sections=None):
        if not found_lectures:
            return False

        # Warn if selecting MANY sections without specificity.
        if len(found_lectures) > 2 and not specific_sections:
            logger.warning(f"Found {len(found_lectures)} sections! You are registering for ALL of them.")

        lectures = self.collect_lectures(root)
        lecture_by_schid = {lec["sch_id"]: lec for lec in lectures if lec["sch_id"]}
        existing_selected = self.collect_existing_selected(root)
        force_preserved_details = self.collect_force_preserved_details(lectures)
        force_preserved_schids = [lec["sch_id"] for lec in force_preserved_details]
        found_lectures = [sch_id for sch_id in dict.fromkeys(found_lectures) if sch_id]

        if existing_selected:
            logger.info(
                "Preserving existing selected SIS sections so nothing is deselected: "
                + ", ".join(existing_selected)
            )
        else:
            logger.info("No existing selected SIS sections were marked in the timetable XML.")

        if force_preserved_details:
            logger.warning(
                "Force-preserving configured course(s) even if SIS XML says Selected=0: "
                + ", ".join(self.format_lecture_line(lec) for lec in force_preserved_details)
            )

        all_schids = [
            sch_id
            for sch_id in dict.fromkeys(existing_selected + force_preserved_schids + found_lectures)
            if sch_id
        ]
        if not all_schids:
            logger.error("Refusing to submit an empty selection string.")
            return False
        self.selected_lectures_str = "," + ",".join(all_schids) + ","
        self.existing_selected_details = [
            lecture_by_schid[sch_id] for sch_id in existing_selected if sch_id in lecture_by_schid
        ]
        self.force_preserved_details = [
            lecture_by_schid[sch_id]
            for sch_id in force_preserved_schids
            if sch_id in lecture_by_schid and sch_id not in existing_selected
        ]
        self.new_selected_details = [
            lecture_by_schid[sch_id]
            for sch_id in found_lectures
            if (
                sch_id in lecture_by_schid
                and sch_id not in existing_selected
                and sch_id not in force_preserved_schids
            )
        ]
        self.selected_lecture_details = [
            lecture_by_schid[sch_id] for sch_id in all_schids if sch_id in lecture_by_schid
        ]
        logger.info(f"Final Selection String: {self.selected_lectures_str}")
        for code in sorted(self.force_preserve_course_codes):
            if any(
                normalize_course_code(lec["code"]) == code
                for lec in self.existing_selected_details + self.force_preserved_details
            ):
                logger.info(f"{code} is included in the preserved selection payload.")
            else:
                logger.warning(f"{code} was configured for force-preservation but was not found in the SIS timetable.")

        return True

    def confirm_selection_before_submit(self):
        print("\nReview final SIS selection before anything is submitted:")
        print("\nAlready selected in SIS and preserved:")
        if self.existing_selected_details:
            for lec in self.existing_selected_details:
                print("  - " + self.format_lecture_line(lec))
        else:
            print("  - None detected")

        print("\nForce-preserved because SIS can show it visually selected while XML says Selected=0:")
        if self.force_preserved_details:
            for lec in self.force_preserved_details:
                print("  - " + self.format_lecture_line(lec))
        else:
            print("  - None")

        print("\nNew selections to add:")
        if self.new_selected_details:
            for lec in self.new_selected_details:
                print("  - " + self.format_lecture_line(lec))
        else:
            print("  - None")

        print("\nFinal schedule payload includes:")
        if self.selected_lecture_details:
            for lec in self.selected_lecture_details:
                print("  - " + self.format_lecture_line(lec))
        else:
            print("  - None")

        if self.force_preserve_course_codes:
            print("\nConfigured force-preservation checks:")
            for code in sorted(self.force_preserve_course_codes):
                preserved = any(
                    normalize_course_code(lec["code"]) == code
                    for lec in self.existing_selected_details + self.force_preserved_details
                )
                print(f"  - {code}: {'YES, included in final payload' if preserved else 'NO, not found to preserve'}")
        print(f"Raw selection string that will be sent: {self.selected_lectures_str}")

        return parse_yes_no("\nSubmit this selection to SIS?", default=False)

    def confirm_final_registration_request(self):
        print("\nFinal registration request review:")
        print("These sections are still in the final selection string:")
        if self.selected_lecture_details:
            for lec in self.selected_lecture_details:
                print("  - " + self.format_lecture_line(lec))
        else:
            print("  - None")
        print(f"Raw selection string: {self.selected_lectures_str}")
        if self.dry_run:
            print("Dry-run is ON, so the final registration request will not actually be sent.")
            return True
        return parse_yes_no("\nSend the final LIVE registration request now?", default=False)

    def get_timetable_and_select_course(self, target_course_code, specific_sections=None, interactive=False):
        """Fetch XML timetable and find the SchIds for the target course sections."""
        root = self.fetch_timetable()
        if root is None:
            return False

        lectures = self.collect_lectures(root)

        if interactive:
            selected_course = self.choose_course_interactively(lectures, initial_course=target_course_code)
            if not selected_course:
                return False
            found_lectures = self.choose_sections_interactively(lectures, selected_course)
            return self.set_selected_lectures(root, found_lectures, specific_sections=[("interactive", "*")])

        # Find the course
        found_lectures = []
        found_any_matching_code = False
        
        # specific_sections = [("Day", "Period"), ("Day2", "Period2")] OR None
        
        for day in root:
            day_name = day.attrib.get('Name', '')
            
            for lecture in day:
                code = lecture.attrib.get('Code', '')
                if target_course_code.upper() in code.upper():
                    found_any_matching_code = True
                    
                    sch_id = lecture.attrib.get('SchId')
                    l_type = lecture.attrib.get('Type')
                    period = lecture.attrib.get('Period')
                    period_label = format_period_with_time(period)
                    
                    is_match = False
                    if specific_sections:
                        # Check if this lecture matches ANY of the target specs
                        for (target_day, target_period) in specific_sections:
                            day_match = target_day.lower() in day_name.lower()
                            # If period is '*', we match ANY period on that day
                            period_match = (target_period == '*') or (target_period == period)
                            if day_match and period_match:
                                is_match = True
                                break
                    else:
                        # No specific sections = math ALL sections (risky but default)
                        is_match = True

                    if is_match:
                        logger.info(f"Found MATCH: {code} ({l_type}) on {day_name} {period_label} [SchId={sch_id}]")
                        found_lectures.append(sch_id)
                    else:
                        logger.info(f"Skipping: {code} ({l_type}) on {day_name} {period_label} (Did not match targets)")

        
        if not found_lectures:
            if found_any_matching_code:
                logger.error(f"Course {target_course_code} found, but no sections matched your criteria.")
            else:
                logger.error(f"Course {target_course_code} not found in timetable!")
                available_codes = sorted({
                    lecture.attrib.get('Code', '')
                    for day in root
                    for lecture in day
                    if lecture.attrib.get('Code')
                })
                if available_codes:
                    logger.info(f"Available course codes in timetable: {', '.join(available_codes)}")
            return False

        return self.set_selected_lectures(root, found_lectures, specific_sections=specific_sections)

    def get_timetable_and_select_schedule_plan(self, planned_sections):
        """Fetch SIS timetable and select sections imported from schedule-plan."""
        root = self.fetch_timetable()
        if root is None:
            return False

        sis_lectures = self.collect_lectures(root)
        selected_schids = map_schedule_plan_sections_to_sis(planned_sections, sis_lectures)
        if not selected_schids:
            logger.error("No schedule-plan sections could be mapped to SIS sections.")
            return False

        return self.set_selected_lectures(root, selected_schids, specific_sections=[("schedule-plan", "*")])

    def submit_selection(self):
        """Step 2: Submit the selected lectures."""
        logger.info("Step 2: Submitting course selection...")
        url = urljoin(BASE_URL, REGISTRATION_PATH)
        
        payload = {
            '__EVENTTARGET': '',
            '__EVENTARGUMENT': '',
            '__VIEWSTATE': self.viewstate,
            '__VIEWSTATEGENERATOR': self.viewstategenerator,
            'ctl07$HiddenField1': SLOT_TIME_BOUNDS,
            'ctl07$nextPage': 'Next >>',
            'StdSelectedLecs': self.selected_lectures_str,
            'StdSelectedLecs1': self.selected_lectures_str,
            'BData': ''
        }
        if self.eventvalidation:
            payload['__EVENTVALIDATION'] = self.eventvalidation
        logger.info("Selection submit payload:")
        logger.info(json.dumps(payload, indent=2, default=str)[:1200])
        
        resp = self.session.post(url, data=payload)
        if resp.status_code != 200:
            logger.error("Failed to submit selection.")
            self._save_debug_html("debug_selection_submit_response.html", resp.text)
            self._log_page_text_preview(resp.text, "Selection submit response")
            return False

        # Update ViewState for final step
        soup = BeautifulSoup(resp.text, 'html.parser')
        self._save_debug_html("debug_selection_submit_response.html", resp.text)
        self._log_page_text_preview(resp.text, "Selection submit response")
        if not self._update_page_state(soup, "selection submit"):
            self._save_debug_html("debug_selection_submit_response.html", resp.text)
            return False
        self.final_soup = soup # Store for captcha guid extraction
        return True

    def solve_captcha(self):
        """Step 3a: Get captcha image."""
        img = self.get_captcha_image()
        if img is None:
            return None
        img.show()

        captcha_text = input("Enter Captcha Text: ").strip()
        return captcha_text

    def get_captcha_image(self):
        """Download the captcha image from the final page."""
        # Find captcha GUID
        # Pattern: CaptchaImage.aspx?guid=...
        captcha_img = self.final_soup.find('img', src=re.compile(r'CaptchaImage\.aspx'))
        if not captcha_img:
            logger.error("Captcha image not found on final page!")
            return None
            
        captcha_url = urljoin(urljoin(BASE_URL, "/SIS/Modules/"), captcha_img['src'])
        logger.info(f"Downloading Captcha: {captcha_url}")
        
        resp = self.session.get(captcha_url)
        return Image.open(BytesIO(resp.content))

    @staticmethod
    def _extract_course_codes_from_text(text):
        return sorted(set(re.findall(r"\b[A-Z]{3,5}\d{3}\b", text or "")))

    def summarize_registration_result(self, html):
        self._save_debug_html("debug_final_registration_response.html", html)
        soup = BeautifulSoup(html, "html.parser")
        text = " ".join(soup.get_text(" ", strip=True).split())
        lowered = text.lower()

        print("\nSIS final registration response:")
        print(text[:1200] if text else "(No visible text found in response.)")
        if len(text) > 1200:
            print("...response text truncated in terminal; full HTML saved to debug_final_registration_response.html")

        course_codes = self._extract_course_codes_from_text(text)
        if course_codes:
            print("\nCourse codes visible in final response/schedule:")
            for code in course_codes:
                print(f"  - {code}")
        else:
            print("\nNo course codes were visible in the final response text.")

        selected_payload_codes = sorted({
            normalize_course_code(lec["code"])
            for lec in self.selected_lecture_details
            if lec.get("code")
        })
        if selected_payload_codes:
            print("\nCourses you attempted to submit:")
            for code in selected_payload_codes:
                visible = code in course_codes
                print(f"  - {code}: {'visible in result' if visible else 'not visible in result'}")

        if any(word in lowered for word in ["no places", "no place", "full", "waiting", "rejected"]):
            logger.warning("Final response contains a possible no-seat/waiting/rejected message. Check the saved HTML.")
        if course_codes:
            logger.info("Final response appears to contain a non-empty schedule/status result.")
        else:
            logger.warning("Final response did not show course codes; the resulting schedule may be empty.")

    def submit_payload_in_browser(self, url, payload):
        if not self.browser_driver:
            logger.warning("No browser driver is available; falling back to HTTP final request.")
            return None

        logger.info("Submitting final registration request in visible Chrome so the website shows the result page.")
        form_inputs = "".join(
            f'<input type="hidden" name="{html_lib.escape(str(name), quote=True)}" '
            f'value="{html_lib.escape(str(value), quote=True)}">'
            for name, value in payload.items()
        )
        escaped_url = html_lib.escape(url, quote=True)
        html = f"""
        <html>
          <body>
            <form id="finalRegistrationForm" method="post" action="{escaped_url}">
              {form_inputs}
            </form>
            <script>document.getElementById('finalRegistrationForm').submit();</script>
          </body>
        </html>
        """
        self.browser_driver.execute_script("document.open(); document.write(arguments[0]); document.close();", html)
        time.sleep(3)
        result_html = self.browser_driver.page_source
        self._save_debug_html("debug_final_registration_browser_response.html", result_html)
        logger.info("Visible Chrome should now be showing the SIS final schedule/status page.")
        return result_html

    def finalize_registration(self, captcha_text, password):
        """Step 3b: Final Registration (DRY RUN SAFEGUARD)."""
        logger.info("Preparing Final Registration Request...")
        
        url = urljoin(BASE_URL, REGISTRATION_PATH)
        
        payload = {
            '__EVENTTARGET': '',
            '__EVENTARGUMENT': '',
            '__VIEWSTATE': self.viewstate,
            '__VIEWSTATEGENERATOR': self.viewstategenerator,
            'ctl07$HiddenField1': SLOT_TIME_BOUNDS,
            'ctl07$txtEmail': '',
            'ctl07$txtTel': '',
            'ctl07$txtComment': '',
            'ctl07$txtPassword': password,
            'ctl07$CaptchaControl1': captcha_text,
            'ctl07$ButAccept': 'Register',
            'StdSelectedLecs2': self.selected_lectures_str,
            'BData': ''
        }
        if self.eventvalidation:
            payload['__EVENTVALIDATION'] = self.eventvalidation
        
        if self.dry_run:
            logger.info("!!! DRY RUN MODE ACTIVE !!!")
            logger.info("The following payload WOULD be sent:")
            safe_payload = payload.copy()
            safe_payload["ctl07$txtPassword"] = "***"
            safe_payload["ctl07$CaptchaControl1"] = "***"
            logger.info(json.dumps(safe_payload, indent=2, default=str)[:500] + "...")
            logger.info("Request NOT sent.")
            return True
        else:
            if self.browser_driver:
                result_html = self.submit_payload_in_browser(url, payload)
                if not result_html:
                    return False
                logger.info("Registration request submitted in visible Chrome. Check the browser result.")
                self.summarize_registration_result(result_html)
                if "success" in result_html.lower() or "registered" in result_html.lower():
                    logger.info("SUCCESS: Registration likely successful.")
                else:
                    logger.warning("WARNING: Success message not strictly found. Verify manually in Chrome.")
                return True
            else:
                logger.info("SENDING REGISTRATION REQUEST...")
                resp = self.session.post(url, data=payload)
                if resp.status_code == 200:
                    logger.info("Registration request sent! Check result.")
                    self.summarize_registration_result(resp.text)
                    if "success" in resp.text.lower() or "registered" in resp.text.lower():
                         logger.info("SUCCESS: Registration likely successful.")
                    else:
                         logger.warning("WARNING: Success message not strictly found. Verify manually.")
                    return True
                else:
                    logger.error(f"Registration failed: {resp.status_code}")
                    return False

def collect_cli_options(user_id):
    print("\nSIS Registration Bot")
    print("Answer the prompts below. No command-line flags needed.")
    if len(sys.argv) > 1:
        print("Note: command-line options are ignored in this interactive version.")

    if os.path.exists(CREDENTIALS_FILE):
        print(f"Saved credentials file: {os.path.abspath(CREDENTIALS_FILE)}")

    mode = prompt_choice(
        "How do you want to choose sections?",
        [
            ("gui", "Open schedule-plan, then import what you picked"),
            ("saved_schedule", "Import your saved schedule-plan schedule"),
            ("interactive", "Choose from SIS after login: prompts for course, then section numbers"),
            ("manual", "Type before login: course code, then filters like Sunday 4-6"),
        ],
    )

    options = {
        "course": None,
        "specific_sections": [],
        "interactive": mode == "interactive",
        "use_schedule_plan": mode in {"gui", "saved_schedule"},
        "schedule_plan_student_id": user_id,
        "schedule_plan_name": None,
        "live": False,
    }

    if mode == "gui":
        logger.info(f"Opening schedule-plan: {SCHEDULE_PLAN_URL}")
        webbrowser.open(SCHEDULE_PLAN_URL)
        print("\nConfigure your schedule in schedule-plan.")
        print("Save it there, then come back here.")
        print(f"The bot will import the saved schedule for Student ID {user_id}.")
        maybe_name = input("Enter schedule name if not default (or press Enter): ").strip()
        if maybe_name:
            options["schedule_plan_name"] = maybe_name
    elif mode == "saved_schedule":
        print(f"The bot will import the saved schedule for Student ID {user_id}.")
        maybe_name = input("Enter schedule name if not default (or press Enter): ").strip()
        if maybe_name:
            options["schedule_plan_name"] = maybe_name
    elif mode == "interactive":
        print("\nAfter login, the bot will load the SIS timetable.")
        print("It will prompt: choose a course by number or code, then choose section numbers like 1,3 or 2-4.")
    elif mode == "manual":
        while not options["course"]:
            options["course"] = input("Enter target course code, for example CMPS211: ").strip()

        print("\nAdd section filters one at a time, for example:")
        print("  Sunday 4-6")
        print("  Monday 9:10")
        print("Press Enter on a blank line when you are done.")
        section_texts = []
        while True:
            section_text = input("Section filter: ").strip()
            if not section_text:
                break
            section_texts.append(section_text)
        options["specific_sections"] = parse_section_targets(section_texts)
        if not options["specific_sections"]:
            logger.warning("No section filters entered. The bot will select every SIS section matching that course code.")
            if not parse_yes_no("Continue with every matching section?", default=False):
                logger.info("Cancelled before login.")
                sys.exit(0)

    options["live"] = parse_yes_no(
        "Send the real final registration request? Choose No for dry-run.",
        default=False,
    )
    if options["live"]:
        logger.warning("LIVE mode will send the final registration request after captcha/password.")
        if not parse_yes_no("Continue in LIVE mode?", default=False):
            logger.info("Cancelled before login.")
            sys.exit(0)

    return options


def main():
    credential_store = CredentialStore(remember=True)
    user_id, password = credential_store.ensure_login_credentials()
    options = collect_cli_options(user_id)
    planned_sections = None

    if options["use_schedule_plan"]:
        try:
            planned_sections = load_schedule_plan_selections(
                student_id=options["schedule_plan_student_id"],
                schedule_name=options["schedule_plan_name"],
            )
        except Exception as exc:
            logger.error(f"Failed to import schedule-plan schedule: {exc}")
            sys.exit(1)
        source_label = f"student ID {options['schedule_plan_student_id']}"
        if options["schedule_plan_name"]:
            source_label += f" / schedule {options['schedule_plan_name']}"
        log_schedule_plan_preview(planned_sections, source_label)
        logger.info(f"Imported {len(planned_sections)} selected sections from schedule-plan.")

    # 1. Login
    browser_driver = None
    cookies, std_guid, registration_html, browser_driver = login_with_selenium(user_id, password)
    if not cookies:
        sys.exit(1)
        
    client = RegistrationClient(
        cookies,
        std_guid=std_guid,
        dry_run=not options["live"],
        credential_store=credential_store,
        registration_ready=bool(registration_html),
        registration_html=registration_html,
        browser_driver=browser_driver,
    )
    
    # 2. Get Page
    if not client.get_registration_page():
        sys.exit(1)
        
    # 3. Accept Approval
    if not client.accept_approval():
        sys.exit(1)
        
    # 4. Get Timetable & Select
    if options["use_schedule_plan"]:
        if not client.get_timetable_and_select_schedule_plan(planned_sections):
            sys.exit(1)
    elif not client.get_timetable_and_select_course(
        options["course"],
        options["specific_sections"],
        interactive=options["interactive"],
    ):
        sys.exit(1)

    if not client.confirm_selection_before_submit():
        logger.info("Cancelled before submitting any selected lectures to SIS.")
        sys.exit(0)
        
    # 5. Submit Selection
    if not client.submit_selection():
        sys.exit(1)
        
    # 6. Captcha
    captcha_text = client.solve_captcha()
    if not captcha_text:
        sys.exit(1)

    if not client.confirm_final_registration_request():
        logger.info("Cancelled before final registration request.")
        sys.exit(0)
        
    # 7. Finalize
    client.finalize_registration(captcha_text, password)

    if browser_driver:
        logger.info("Chrome is still open for inspection. Close it manually when you are done.")

if __name__ == "__main__":
    main()
