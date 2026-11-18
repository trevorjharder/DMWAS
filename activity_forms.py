import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


CATEGORIES = ("Rock climbing", "Bouldering", "Hiking", "Mountaineering",
              "Trail development and maintenance", "Outdoor education", "Other")
LOCATIONS = {
    "mount_waddington": "Mount Waddington area",
    "other_area": "Outside Mount Waddington",
    "online": "Online",
}
TIME_ZONES = {
    "America/Vancouver": "Pacific (Vancouver)",
    "America/Edmonton": "Mountain (Edmonton)",
    "America/Toronto": "Eastern (Toronto)",
    "UTC": "UTC",
}
REGISTRATION_MODES = {
    "immediate": "Immediate confirmation",
    "organizer_approval": "Organizer approval",
}
PUBLIC_TEXT_FIELDS = {
    "title": ("Activity title", 120),
    "summary": ("Public summary", 800),
}
ORGANIZER_TEXT_FIELDS = {
    "meeting_details": ("Meeting location or online link", 800),
    "accessibility": ("Accessibility", 1200),
    "equipment": ("Equipment", 1200),
    "requirements": ("Participant requirements", 1200),
    "safety": ("Safety information", 2000),
}


def read_activity_form(form):
    public = {name: form.get(name, "").strip() for name in (
        *PUBLIC_TEXT_FIELDS, "category", "location", "time_zone", "starts", "ends")}
    organizer = {name: form.get(name, "").strip() for name in (
        *ORGANIZER_TEXT_FIELDS, "capacity", "registration_mode")}
    return public, organizer


def local_to_utc(value, zone):
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}", value):
        raise ValueError
    local = datetime.fromisoformat(value)
    # Require a clock time with exactly one corresponding instant.
    instants = set()
    for fold in (0, 1):
        instant = local.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
        if instant.astimezone(zone).replace(tzinfo=None) == local:
            instants.add(instant)
    if len(instants) != 1:
        raise ValueError
    return instants.pop()


def validate_activity(public, organizer, *, submitting=False):
    errors = []
    for values, fields in ((public, PUBLIC_TEXT_FIELDS), (organizer, ORGANIZER_TEXT_FIELDS)):
        for name, (label, limit) in fields.items():
            if len(values[name]) > limit or "\x00" in values[name]:
                errors.append(f"{label} must be valid text of up to {limit} characters.")
    for value, choices, label in (
        (public["category"], CATEGORIES, "a category"),
        (public["location"], LOCATIONS, "a public location"),
        (public["time_zone"], TIME_ZONES, "a time zone"),
        (organizer["registration_mode"], REGISTRATION_MODES, "a registration mode"),
    ):
        if (value or submitting) and value not in choices:
            errors.append(f"Choose {label}.")

    capacity = organizer["capacity"]
    if (capacity or submitting) and (
        not re.fullmatch(r"[0-9]{1,4}", capacity) or not 1 <= int(capacity) <= 1000
    ):
        errors.append("Enter a capacity between 1 and 1,000.")

    starts_at = ends_at = None
    if public["starts"] or public["ends"] or submitting:
        try:
            zone = ZoneInfo(public["time_zone"])
            if public["starts"]:
                starts_at = local_to_utc(public["starts"], zone)
            if public["ends"]:
                ends_at = local_to_utc(public["ends"], zone)
        except (ValueError, ZoneInfoNotFoundError, OverflowError):
            errors.append("Enter dates and times that occur exactly once in the selected time zone.")
        if starts_at and ends_at and ends_at <= starts_at:
            errors.append("The end must be after the start.")
        if submitting and (not starts_at or not ends_at or starts_at <= datetime.now(timezone.utc)):
            errors.append("Enter a future start and an end date and time.")

    if submitting:
        for values, name, label in (
            (public, "title", "an activity title"),
            (public, "summary", "a public summary"),
            (organizer, "meeting_details", "a meeting location or online link for review"),
            (organizer, "accessibility", "accessibility information"),
        ):
            if not values[name]:
                errors.append(f"Enter {label}.")
        if public["location"] in {"mount_waddington", "other_area"} and not organizer["safety"]:
            errors.append("Enter safety information for the in-person activity.")
    return errors, starts_at, ends_at
