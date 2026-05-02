import time
import re
import json
import logging
import argparse
import sys
import os
import base64
import ctypes
import getpass
import webbrowser
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

    def save_user_id(self, user_id):
        if not self.remember or not user_id:
            return
        self.data["user_id"] = user_id
        self._save()

    def get_password(self):
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
        self.data["password_dpapi"] = protect_secret(password)
        self._save()


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


def parse_yes_no(prompt, default=False):
    suffix = " [Y/n]: " if default else " [y/N]: "
    answer = input(prompt + suffix).strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


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


def split_multi_session_sections(courses):
    """Mirror schedule-plan's frontend normalization for section IDs."""
    normalized_courses = []
    for course in courses:
        normalized_sections = []
        for section in course.get("sections", []):
            sessions = section.get("sessions") or []
            if len(sessions) <= 1:
                normalized_sections.append(section)
                continue
            for idx, session in enumerate(sessions):
                copied = section.copy()
                copied["id"] = (
                    f"{section.get('id')}-{session.get('day')}-"
                    f"{session.get('startString')}-{session.get('endString')}-{idx}"
                )
                copied["sessions"] = [session]
                normalized_sections.append(copied)
        copied_course = course.copy()
        copied_course["sections"] = normalized_sections
        normalized_courses.append(copied_course)
    return normalized_courses


def fetch_schedule_plan_courses():
    resp = requests.get(f"{SCHEDULE_PLAN_API_BASE}/courses", timeout=30)
    resp.raise_for_status()
    return split_multi_session_sections(resp.json())


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
    selections = []
    for item in raw_items:
        course_code = item.get("courseCode") or item.get("c")
        if not course_code:
            continue
        selections.append({
            "courseCode": course_code,
            "selectedLectureId": item.get("selectedLectureId") or item.get("l"),
            "selectedTutorialId": item.get("selectedTutorialId") or item.get("t"),
            "selectedLabId": item.get("selectedLabId") or item.get("b"),
            "selectedMthsGroup": item.get("selectedMthsGroup") or item.get("m"),
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
        for section_id in [sid for sid in selected_ids if sid]:
            section = next((section for section in sections if section.get("id") == section_id), None)
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

def login_with_selenium():
    """Use Selenium to handle login and extract session cookies."""
    logger.info("Launching Selenium Chrome for login...")
    options = webdriver.ChromeOptions()
    # options.add_argument('--headless')  # Comment out to see the browser
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    
    try:
        driver.get(BASE_URL)
        logger.info("Please log in manually in the browser window.")
        logger.info("Waiting for login to complete (redirect to /SIS/Default.aspx)...")
        
        # Wait up to 120 seconds for user to login
        WebDriverWait(driver, 120).until(
            EC.url_contains("/SIS/Default.aspx")
        )
        logger.info("Login detected! Extracting cookies...")
        
        selenium_cookies = driver.get_cookies()
        session_cookies = {}
        for cookie in selenium_cookies:
            session_cookies[cookie['name']] = cookie['value']
            
        logger.info(f"Extracted {len(session_cookies)} cookies.")
        
        # Extract Student GUID and User ID from dashboard source
        page_source = driver.page_source
        
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
            
        return session_cookies, std_guid
        
    except Exception as e:
        logger.error(f"Login failed or timed out: {e}")
        return None, None
    finally:
        driver.quit()

class RegistrationClient:
    def __init__(self, cookies, std_guid=None, dry_run=True, credential_store=None):
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
        self.stdid_candidates = []
        if std_guid:
            self._add_stdid_candidate(std_guid)

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
        fields = {
            "viewstate": "__VIEWSTATE",
            "eventvalidation": "__EVENTVALIDATION",
            "viewstategenerator": "__VIEWSTATEGENERATOR",
        }

        values = {}
        missing = []
        for attr, field_id in fields.items():
            element = soup.find("input", {"id": field_id})
            if not element or element.get("value") is None:
                missing.append(field_id)
            else:
                values[attr] = element.get("value")

        if missing:
            logger.error(f"Could not find required form fields after {source}: {', '.join(missing)}")
            return False

        self.viewstate = values["viewstate"]
        self.eventvalidation = values["eventvalidation"]
        self.viewstategenerator = values["viewstategenerator"]
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

    def get_registration_page(self):
        """Fetch the registration page and extract VIEWSTATE."""
        url = urljoin(BASE_URL, REGISTRATION_PATH)
        logger.info(f"Fetching registration page: {url}")
        
        resp = self.session.get(url)
        if resp.status_code != 200:
            logger.error(f"Failed to fetch registration page: {resp.status_code}")
            return False
            
        soup = BeautifulSoup(resp.text, 'html.parser')
        if not self._update_page_state(soup, "registration page fetch"):
            return False
        self._extract_identifiers_from_html(resp.text, source="registration page")
             
        return True

    def accept_approval(self):
        """Step 1: Click 'Next' on the approval message."""
        logger.info("Step 1: Accepting approval message...")
        url = urljoin(BASE_URL, REGISTRATION_PATH)
        
        payload = {
            '__EVENTTARGET': '',
            '__EVENTARGUMENT': '',
            '__VIEWSTATE': self.viewstate,
            '__VIEWSTATEGENERATOR': self.viewstategenerator,
            '__EVENTVALIDATION': self.eventvalidation,
            'ctl07$ButMessageShown': 'Next>>',
            'ctl07$HiddenField1': SLOT_TIME_BOUNDS,
            'BData': ''
        }
        
        resp = self.session.post(url, data=payload)
        if resp.status_code != 200:
            logger.error("Failed to accept approval.")
            self._save_debug_html("debug_approval_response.html", resp.text)
            self._log_page_text_preview(resp.text, "Approval response")
            return False
            
        # Update ViewState for next step
        soup = BeautifulSoup(resp.text, 'html.parser')
        if not self._update_page_state(soup, "approval response"):
            self._save_debug_html("debug_approval_response.html", resp.text)
            self._log_page_text_preview(resp.text, "Approval response")
            return False
        self._extract_identifiers_from_html(resp.text, source="approval response")
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
                if lecture.attrib.get('Selected') == '1':
                    existing_selected.append(lecture.attrib.get('SchId'))
        return existing_selected

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

        existing_selected = self.collect_existing_selected(root)
        logger.info(f"Preserving {len(existing_selected)} existing registrations.")

        all_schids = [sch_id for sch_id in dict.fromkeys(existing_selected + found_lectures) if sch_id]
        self.selected_lectures_str = "," + ",".join(all_schids) + ","
        logger.info(f"Final Selection String: {self.selected_lectures_str}")

        return True

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
            '__EVENTVALIDATION': self.eventvalidation,
            'ctl07$HiddenField1': SLOT_TIME_BOUNDS,
            'ctl07$nextPage': 'Next+>>',
            'StdSelectedLecs': self.selected_lectures_str,
            'StdSelectedLecs1': self.selected_lectures_str,
            'BData': ''
        }
        
        resp = self.session.post(url, data=payload)
        if resp.status_code != 200:
            logger.error("Failed to submit selection.")
            return False

        # Update ViewState for final step
        soup = BeautifulSoup(resp.text, 'html.parser')
        if not self._update_page_state(soup, "selection submit"):
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

    def finalize_registration(self, captcha_text, password):
        """Step 3b: Final Registration (DRY RUN SAFEGUARD)."""
        logger.info("Preparing Final Registration Request...")
        
        url = urljoin(BASE_URL, REGISTRATION_PATH)
        
        payload = {
            '__EVENTTARGET': '',
            '__EVENTARGUMENT': '',
            '__VIEWSTATE': self.viewstate,
            '__VIEWSTATEGENERATOR': self.viewstategenerator,
            '__EVENTVALIDATION': self.eventvalidation,
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
            logger.info("SENDING REGISTRATION REQUEST...")
            resp = self.session.post(url, data=payload)
            if resp.status_code == 200:
                logger.info("Registration request sent! Check result.")
                logger.info(resp.text[:500])
                if "success" in resp.text.lower() or "registered" in resp.text.lower():
                     logger.info("SUCCESS: Registration likely successful.")
                else:
                     logger.warning("WARNING: Success message not strictly found. Verify manually.")
                return True
            else:
                logger.error(f"Registration failed: {resp.status_code}")
                return False

def main():
    parser = argparse.ArgumentParser(description="SIS Registration Bot")
    parser.add_argument('--course', type=str, help="Target Course Code")
    parser.add_argument('--add-section', action='append', help="Add a target section (Format: 'Day Period', e.g. 'Monday 2:3'). Can be used multiple times.")
    parser.add_argument('-i', '--interactive', action='store_true', help="Choose course and sections from the loaded timetable.")
    parser.add_argument('--schedule-plan-url', help="Import selected courses from a schedule-plan share URL.")
    parser.add_argument('--schedule-plan-student-id', help="Import the saved schedule-plan schedule for this student ID.")
    parser.add_argument('--schedule-plan-name', help="Saved schedule-plan schedule name, if not the default.")
    parser.add_argument('--remember-credentials', action='store_true', help="Remember Student ID and final SIS password locally. Password is protected with Windows DPAPI.")
    parser.add_argument('--forget-credentials', action='store_true', help="Delete saved Student ID/password and exit.")
    parser.add_argument('--gui', action='store_true', help="Open schedule-plan in your browser, then import the finished schedule.")
    parser.add_argument('--live', action='store_true', help="DISABLE Dry Run (Send real requests)")
    args = parser.parse_args()

    credential_store = CredentialStore(remember=args.remember_credentials)
    if args.forget_credentials:
        credential_store.forget()
        sys.exit(0)

    use_schedule_plan = bool(args.gui or args.schedule_plan_url or args.schedule_plan_student_id)
    planned_sections = None

    if args.gui:
        logger.info(f"Opening schedule-plan: {SCHEDULE_PLAN_URL}")
        webbrowser.open(SCHEDULE_PLAN_URL)
        print("\nConfigure your schedule in schedule-plan.")
        print("Save it there, then come back here.")
        saved_id = input("Enter schedule-plan student ID (press Enter to paste a Share link instead): ").strip()
        if saved_id:
            args.schedule_plan_student_id = saved_id
            maybe_name = input("Enter schedule name if not default (or press Enter): ").strip()
            if maybe_name:
                args.schedule_plan_name = maybe_name
        else:
            args.schedule_plan_url = input("Paste schedule-plan Share link: ").strip()

    if use_schedule_plan or args.schedule_plan_url or args.schedule_plan_student_id:
        try:
            planned_sections = load_schedule_plan_selections(
                share_url=args.schedule_plan_url,
                student_id=args.schedule_plan_student_id,
                schedule_name=args.schedule_plan_name,
            )
        except Exception as exc:
            logger.error(f"Failed to import schedule-plan schedule: {exc}")
            sys.exit(1)
        if args.schedule_plan_url:
            source_label = "share URL"
        else:
            source_label = f"student ID {args.schedule_plan_student_id}"
            if args.schedule_plan_name:
                source_label += f" / schedule {args.schedule_plan_name}"
        log_schedule_plan_preview(planned_sections, source_label)
        logger.info(f"Imported {len(planned_sections)} selected sections from schedule-plan.")
        use_schedule_plan = True

    interactive = args.interactive or (not args.course and not args.add_section and not use_schedule_plan)

    if args.live:
        logger.warning("LIVE mode will send the final registration request after captcha/password.")
        if not parse_yes_no("Continue in LIVE mode?", default=False):
            logger.info("Cancelled before login.")
            sys.exit(0)

    if not interactive and not args.course and not use_schedule_plan:
        logger.error("Please provide --course, or run with --interactive to choose from the timetable.")
        sys.exit(1)
    
    # Parse section targets
    # input: ["Monday 2:3", "Sunday 4:5"]
    # output: [("Monday", "2:3"), ("Sunday", "4:5")]
    specific_sections = []
    if args.add_section:
        for s in args.add_section:
            parts = s.strip().split()
            if len(parts) >= 2:
                # "Monday 2:3" -> day="Monday", period="2:3"
                # If period has spaces? Unlikely based on XML.
                day = parts[0]
                period, note = normalize_period_input(parts[1])
                if note:
                    logger.info(note)
                mapped = period_to_time_range(period)
                if mapped and re.fullmatch(r"\d+:\d+", period):
                    logger.info(f"Section filter '{day} {period}' maps to actual time {mapped[0]}-{mapped[1]}.")
                specific_sections.append((day, period))
            else:
                logger.warning(f"Invalid section format: '{s}'. Expected 'Day Period'. Ignoring.")

    # 1. Login
    cookies, std_guid = login_with_selenium()
    if not cookies:
        sys.exit(1)
        
    client = RegistrationClient(
        cookies,
        std_guid=std_guid,
        dry_run=not args.live,
        credential_store=credential_store,
    )
    
    # 2. Get Page
    if not client.get_registration_page():
        sys.exit(1)
        
    # 3. Accept Approval
    if not client.accept_approval():
        sys.exit(1)
        
    # 4. Get Timetable & Select
    if use_schedule_plan:
        if not client.get_timetable_and_select_schedule_plan(planned_sections):
            sys.exit(1)
    elif not client.get_timetable_and_select_course(args.course, specific_sections, interactive=interactive):
        sys.exit(1)
        
    # 5. Submit Selection
    if not client.submit_selection():
        sys.exit(1)
        
    # 6. Captcha
    captcha_text = client.solve_captcha()
    if not captcha_text:
        sys.exit(1)
        
    saved_password = credential_store.get_password()
    if saved_password and parse_yes_no("Use saved SIS password for final registration step?", default=True):
        password = saved_password
    else:
        password = getpass.getpass("Enter your SIS Password: ").strip()
        if args.remember_credentials:
            credential_store.save_password(password)
    
    # 7. Finalize
    client.finalize_registration(captcha_text, password)

if __name__ == "__main__":
    main()
