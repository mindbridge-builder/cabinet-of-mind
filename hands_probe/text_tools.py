def is_valid_email(value: str) -> bool:
    if not isinstance(value, str):
        return False
    value = value.strip()
    if not value:
        return False
    
    # Check for exactly one '@'
    if value.count('@') != 1:
        return False
        
    local_part, domain = value.split('@')
    
    # Check that both parts are non-empty
    if not local_part or not domain:
        return False
        
    # Check that domain contains at least one dot
    if '.' not in domain:
        return False
    
    return True

def is_valid_phone(value: str) -> bool:
    if not isinstance(value, str):
        return False
    value = value.strip()
    if not value:
        return False
        
    digits_only = ''.join(char for char in value if char.isdigit())
    
    # Must have at least 7 digits
    if len(digits_only) < 7:
        return False
    
    return True

def normalize_email(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if not is_valid_email(value):
        return None
    return value.lower()

def normalize_phone(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    digits_only = ''.join(char for char in value if char.isdigit())
    if len(digits_only) < 7:
        return None
    if digits_only[0] == '1':
        digits_only = '+' + digits_only
    elif digits_only[0] != '+':
        digits_only = '+' + digits_only
    return digits_only

def normalize_name(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    # Collapse inner whitespace runs to single space
    import re
    return re.sub(r'\s+', ' ', value)

def normalize_label(label: str) -> str:
    return label.strip().lower()

def count_words(text: str) -> int:
    words = text.split()
    return len(words)

def is_blank(value: str) -> bool:
    return not value or value.isspace()

def parse_candidate_info(text: str) -> dict[str, str | None]:
    result = {"name": None, "email": None, "company": None, "phone": None}
    aliases = {
        "e-mail": "email",
        "mail": "email",
        "organization": "company",
        "org": "company",
        "tel": "phone",
        "telephone": "phone",
    }
    
    for line in text.strip().split('\n'):
        if ':' not in line:
            continue
            
        key, value = line.split(':', 1)
        key = normalize_label(key)
        value = value.strip()
        
        if key in aliases:
            key = aliases[key]

        if key in result:
            result[key] = value if value else None
    
    return result

def parse_candidates(text: str) -> list[dict[str, str | None]]:
    if not text or not text.strip():
        return []

    blocks = []
    current_block = ""

    for line in text.split('\n'):
        if line.strip() == "":
            if current_block.strip():
                blocks.append(current_block)
                current_block = ""
        else:
            current_block += line + "\n"

    if current_block.strip():
        blocks.append(current_block)

    result = []
    for block in blocks:
        parsed = parse_candidate_info(block)
        if any(parsed.values()):
            result.append(parsed)

    return result

def extract_candidate_names(candidates: list[dict[str, str | None]]) -> list[str]:
    names = []
    for candidate in candidates:
        name = candidate.get("name")
        if name is not None and name.strip() != "":
            names.append(name.strip())
    return names

def get_candidate_contact_methods(info: dict[str, str | None]) -> list[str]:
    methods = []
    email = info.get("email")
    phone = info.get("phone")
    
    if email and not is_blank(email):
        methods.append("email")
        
    if phone and not is_blank(phone):
        methods.append("phone")
        
    return methods

def has_complete_contact(info: dict[str, str | None]) -> bool:
    email = info.get("email")
    phone = info.get("phone")
    
    return bool(email and not is_blank(email) and phone and not is_blank(phone))

def summarize_candidate(info: dict[str, str | None]) -> str:
    name = info.get("name")
    company = info.get("company")
    email = info.get("email")
    phone = info.get("phone")

    if name is None or name == "":
        result = "Unknown candidate"
    else:
        result = name

    if company:
        result += f" at {company}"

    contact_parts = []
    if email:
        contact_parts.append(email)
    if phone:
        contact_parts.append(phone)

    if contact_parts:
        result += f" ({', '.join(contact_parts)})"

    return result

def filter_candidates_by_company(candidates: list[dict[str, str | None]], company: str) -> list[dict[str, str | None]]:
    if not company or not company.strip():
        return []
    
    company = company.strip().lower()
    result = []
    
    for candidate in candidates:
        candidate_company = candidate.get("company")
        if candidate_company is not None and candidate_company.strip() != "":
            if candidate_company.strip().lower() == company:
                result.append(candidate)
    
    return result

def dedupe_candidates_by_email(candidates: list[dict[str, str | None]]) -> list[dict[str, str | None]]:
    result = []
    seen_emails = set()

    for candidate in candidates:
        email = candidate.get("email")
        if email is None:
            continue

        normalized_email = email.strip().lower()
        if not normalized_email or normalized_email in seen_emails:
            continue

        seen_emails.add(normalized_email)
        result.append(candidate)

    return result

def group_candidate_names_by_company(candidates: list[dict[str, str | None]]) -> dict[str, list[str]]:
    result = {}
    
    for candidate in candidates:
        name = candidate.get("name")
        company = candidate.get("company")
        
        if not name or is_blank(name):
            continue
            
        if not company or is_blank(company):
            continue
            
        company_key = company.strip().lower()
        name_stripped = name.strip()
        
        if company_key not in result:
            result[company_key] = []
            
        result[company_key].append(name_stripped)
    
    return result

def redact_contact(info: dict[str, str | None]) -> dict[str, str | None]:
    # Create a copy of the input dictionary
    result = info.copy()
    
    # Redact email if present and non-blank
    email = result.get("email")
    if isinstance(email, str) and email.strip():
        email = email.strip()
        at_index = email.find('@')
        if at_index != -1:
            local_part = email[:at_index]
            domain = email[at_index + 1:]
            # Replace local part with first character followed by ***
            result["email"] = f"{local_part[0]}***@{domain}"
    
    # Redact phone if present and non-blank
    phone = result.get("phone")
    if isinstance(phone, str) and phone.strip():
        phone = phone.strip()
        digits_only = ''.join(char for char in phone if char.isdigit())
        if len(digits_only) >= 4:
            last_four = digits_only[-4:]
            # Replace all but last four digits with ***
            result["phone"] = f"***{last_four}"
    
    return result

def format_candidate_report(candidates: list[dict[str, str | None]]) -> str:
    lines = []
    
    for candidate in candidates:
        raw_name = candidate.get("name")
        raw_company = candidate.get("company")
        name = raw_name.strip() if raw_name else ""
        company = raw_company.strip() if raw_company else ""
        
        if not name and not company:
            continue
            
        if not name:
            display_name = "Unknown candidate"
        else:
            display_name = name
            
        if not company:
            display_company = "Unknown company"
        else:
            display_company = company
            
        raw_email = candidate.get("email")
        raw_phone = candidate.get("phone")
        email = raw_email.strip() if raw_email else ""
        phone = raw_phone.strip() if raw_phone else ""
        
        contacts = []
        if email and not is_blank(email):
            contacts.append(email)
        if phone and not is_blank(phone):
            contacts.append(phone)
            
        contact_str = ", ".join(contacts) if contacts else "no contact"
        
        lines.append(f"{display_name} - {display_company} - {contact_str}")
    
    return "\n".join(lines)
