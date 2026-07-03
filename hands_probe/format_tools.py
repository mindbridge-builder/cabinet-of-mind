def trim_or_none(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def slugify(value: str) -> str:
    if value is None or not isinstance(value, str):
        return ""
    value = value.strip().lower()
    if not value:
        return ""
    
    # Replace runs of non-alphanumeric characters with a single hyphen
    result = []
    for i, char in enumerate(value):
        if char.isalnum():
            result.append(char)
        elif i > 0 and result and result[-1] != '-':
            result.append('-')
    
    # Convert list to string and strip leading/trailing hyphens
    slug = ''.join(result)
    return slug.strip('-')


def join_nonblank(parts: list[str], separator: str = " ") -> str:
    cleaned = []
    for part in parts:
        if not isinstance(part, str):
            continue
        stripped = part.strip()
        if stripped:
            cleaned.append(stripped)
    return separator.join(cleaned)


def format_phone_us(value: str) -> str | None:
    if value is None or not isinstance(value, str):
        return None
    
    # Keep only digits
    digits_only = ''.join(filter(str.isdigit, value))
    
    # Check for valid phone number lengths
    if len(digits_only) == 10:
        # Format as (123) 456-7890
        return f"({digits_only[:3]}) {digits_only[3:6]}-{digits_only[6:]}"
    elif len(digits_only) == 11 and digits_only.startswith('1'):
        # Format as +1 (234) 567-8901
        return f"+1 ({digits_only[1:4]}) {digits_only[4:7]}-{digits_only[7:]}"
    
    # Return None for invalid cases
    return None


def normalize_date_iso(value: str | None) -> str | None:
    if value is None or not isinstance(value, str):
        return None
    
    value = value.strip()
    if not value:
        return None
    
    # Try to parse different date formats
    import re
    from datetime import datetime
    
    # YYYY-MM-DD format (ISO)
    iso_match = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})$', value)
    if iso_match:
        try:
            year, month, day = map(int, iso_match.groups())
            dt = datetime(year, month, day)
            return f"{dt.strftime('%Y-%m-%d')}"
        except ValueError:
            return None
    
    # DD.MM.YYYY format
    dot_match = re.match(r'^(\d{1,2})\.(\d{1,2})\.(\d{4})$', value)
    if dot_match:
        try:
            day, month, year = map(int, dot_match.groups())
            dt = datetime(year, month, day)
            return f"{dt.strftime('%Y-%m-%d')}"
        except ValueError:
            return None
    
    # MM/DD/YYYY format
    slash_match = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})$', value)
    if slash_match:
        try:
            month, day, year = map(int, slash_match.groups())
            dt = datetime(year, month, day)
            return f"{dt.strftime('%Y-%m-%d')}"
        except ValueError:
            return None
    
    # Unsupported format
    return None


def dedupe_labels(labels: list[str]) -> list[str]:
    seen = set()
    result = []
    for label in labels:
        if not isinstance(label, str):
            continue
        stripped = label.strip()
        if not stripped:
            continue
        lower_label = stripped.lower()
        if lower_label not in seen:
            seen.add(lower_label)
            result.append(stripped)
    return result


def render_markdown_table(rows: list[dict], columns: list[str]) -> str:
    if not columns:
        return ""
    
    # Process rows to ensure all keys exist and clean values
    processed_rows = []
    for row in rows:
        if not isinstance(row, dict):
            row = {}
        processed_row = []
        for col in columns:
            value = row.get(col)
            if value is None:
                value = ""
            elif not isinstance(value, str):
                value = str(value)
            # Strip and escape pipe characters
            value = value.strip().replace("|", "\\|")
            processed_row.append(value)
        processed_rows.append(processed_row)
    
    # Build table
    header = " | ".join(columns)
    separator = " | ".join("---" for _ in columns)
    body_rows = []
    for row in processed_rows:
        body_rows.append(" | ".join(row))
    
    return "\n".join([header, separator] + body_rows) if body_rows else f"{header}\n{separator}"
