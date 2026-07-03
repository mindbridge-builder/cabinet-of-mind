import pytest
from hands_probe.text_tools import normalize_label, count_words, is_blank, parse_candidate_info, parse_candidates, summarize_candidate, get_candidate_contact_methods, has_complete_contact, extract_candidate_names, filter_candidates_by_company, dedupe_candidates_by_email, group_candidate_names_by_company, format_candidate_report, normalize_email, normalize_phone, normalize_name, is_valid_email, is_valid_phone, redact_contact

def test_normalize_label():
    assert normalize_label("  Name  ") == "name"
    assert normalize_label("EMAIL") == "email"
    assert normalize_label("") == ""

def test_count_words():
    assert count_words("hello world") == 2
    assert count_words("   ") == 0
    assert count_words("") == 0

def test_is_blank():
    assert is_blank("") == True
    assert is_blank("   ") == True
    assert is_blank("not blank") == False

def test_parse_candidate_info():
    text = """
Name: Ivan Petrov
Email: ivan@example.com
Company: Megalift
"""
    result = parse_candidate_info(text)
    assert result["name"] == "Ivan Petrov"
    assert result["email"] == "ivan@example.com"
    assert result["company"] == "Megalift"
    assert result["phone"] is None

def test_parse_candidate_info_with_spaces():
    text = """
Name:   Ivan Petrov   
Email:   ivan@example.com   
Company: Megalift
"""
    result = parse_candidate_info(text)
    assert result["name"] == "Ivan Petrov"
    assert result["email"] == "ivan@example.com"
    assert result["company"] == "Megalift"
    assert result["phone"] is None

def test_parse_candidate_info_missing_field():
    text = """
Name: Ivan Petrov
Company: Megalift
"""
    result = parse_candidate_info(text)
    assert result["name"] == "Ivan Petrov"
    assert result["email"] is None
    assert result["company"] == "Megalift"
    assert result["phone"] is None

def test_parse_candidate_info_case_insensitive():
    text = """
name: Ivan Petrov
EMAIL: ivan@example.com
COMPANY: Megalift
"""
    result = parse_candidate_info(text)
    assert result["name"] == "Ivan Petrov"
    assert result["email"] == "ivan@example.com"
    assert result["company"] == "Megalift"
    assert result["phone"] is None

def test_parse_candidate_info_ignore_unknown():
    text = """
Name: Ivan Petrov
Email: ivan@example.com
Company: Megalift
UnknownField: some value
"""
    result = parse_candidate_info(text)
    assert result["name"] == "Ivan Petrov"
    assert result["email"] == "ivan@example.com"
    assert result["company"] == "Megalift"
    assert result["phone"] is None

def test_parse_candidate_info_with_phone():
    text = """
Name: Ivan Petrov
Email: ivan@example.com
Company: Megalift
Phone: 123-456-7890
"""
    result = parse_candidate_info(text)
    assert result["name"] == "Ivan Petrov"
    assert result["email"] == "ivan@example.com"
    assert result["company"] == "Megalift"
    assert result["phone"] == "123-456-7890"

def test_parse_candidate_info_missing_phone():
    text = """
Name: Ivan Petrov
Email: ivan@example.com
Company: Megalift
"""
    result = parse_candidate_info(text)
    assert result["name"] == "Ivan Petrov"
    assert result["email"] == "ivan@example.com"
    assert result["company"] == "Megalift"
    assert result["phone"] is None

def test_parse_candidate_info_phone_case_insensitive():
    text = """
Name: Ivan Petrov
Email: ivan@example.com
Company: Megalift
PHONE: 123-456-7890
"""
    result = parse_candidate_info(text)
    assert result["name"] == "Ivan Petrov"
    assert result["email"] == "ivan@example.com"
    assert result["company"] == "Megalift"
    assert result["phone"] == "123-456-7890"

def test_parse_candidate_info_empty_phone():
    text = "\n".join([
        "Name: Ivan Petrov",
        "Email: ivan@example.com",
        "Company: Megalift",
        "Phone: ",
    ])
    result = parse_candidate_info(text)
    assert result["name"] == "Ivan Petrov"
    assert result["email"] == "ivan@example.com"
    assert result["company"] == "Megalift"
    assert result["phone"] is None

def test_parse_candidate_info_email_aliases():
    text = """
Name: Ivan Petrov
E-mail: ivan@example.com
Company: Megalift
"""
    result = parse_candidate_info(text)
    assert result["name"] == "Ivan Petrov"
    assert result["email"] == "ivan@example.com"
    assert result["company"] == "Megalift"
    assert result["phone"] is None

def test_parse_candidate_info_company_aliases():
    text = """
Name: Ivan Petrov
Organization: Megalift
Email: ivan@example.com
"""
    result = parse_candidate_info(text)
    assert result["name"] == "Ivan Petrov"
    assert result["email"] == "ivan@example.com"
    assert result["company"] == "Megalift"
    assert result["phone"] is None

def test_parse_candidate_info_phone_aliases():
    text = """
Name: Ivan Petrov
Email: ivan@example.com
Company: Megalift
Tel: 123-456-7890
"""
    result = parse_candidate_info(text)
    assert result["name"] == "Ivan Petrov"
    assert result["email"] == "ivan@example.com"
    assert result["company"] == "Megalift"
    assert result["phone"] == "123-456-7890"

def test_parse_candidate_info_case_insensitive_aliases():
    text = """
Name: Ivan Petrov
E-MAIL: ivan@example.com
ORG: Megalift
TELEPHONE: 123-456-7890
"""
    result = parse_candidate_info(text)
    assert result["name"] == "Ivan Petrov"
    assert result["email"] == "ivan@example.com"
    assert result["company"] == "Megalift"
    assert result["phone"] == "123-456-7890"

def test_parse_candidate_info_mixed_aliases_and_canonical():
    text = """
Name: Ivan Petrov
Mail: ivan@example.com
Organization: Megalift
Tel: 123-456-7890
"""
    result = parse_candidate_info(text)
    assert result["name"] == "Ivan Petrov"
    assert result["email"] == "ivan@example.com"
    assert result["company"] == "Megalift"
    assert result["phone"] == "123-456-7890"

def test_parse_candidates_empty_input():
    assert parse_candidates("") == []
    assert parse_candidates("   ") == []

def test_parse_candidates_single_candidate():
    text = """
Name: Ivan Petrov
Email: ivan@example.com
Company: Megalift
"""
    result = parse_candidates(text)
    assert len(result) == 1
    assert result[0]["name"] == "Ivan Petrov"
    assert result[0]["email"] == "ivan@example.com"
    assert result[0]["company"] == "Megalift"
    assert result[0]["phone"] is None

def test_parse_candidates_multiple_candidates():
    text = """
Name: Ivan Petrov
Email: ivan@example.com
Company: Megalift

Name: John Smith
Phone: 123-456-7890
"""
    result = parse_candidates(text)
    assert len(result) == 2
    assert result[0]["name"] == "Ivan Petrov"
    assert result[0]["email"] == "ivan@example.com"
    assert result[0]["company"] == "Megalift"
    assert result[0]["phone"] is None
    assert result[1]["name"] == "John Smith"
    assert result[1]["email"] is None
    assert result[1]["company"] is None
    assert result[1]["phone"] == "123-456-7890"

def test_parse_candidates_with_aliases():
    text = """
Name: Ivan Petrov
E-mail: ivan@example.com
Organization: Megalift

Name: John Smith
Tel: 123-456-7890
"""
    result = parse_candidates(text)
    assert len(result) == 2
    assert result[0]["name"] == "Ivan Petrov"
    assert result[0]["email"] == "ivan@example.com"
    assert result[0]["company"] == "Megalift"
    assert result[1]["name"] == "John Smith"
    assert result[1]["phone"] == "123-456-7890"

def test_parse_candidates_ignore_empty_blocks():
    text = """
Name: Ivan Petrov
Email: ivan@example.com

UnknownField: some value

Name: John Smith
Phone: 123-456-7890
"""
    result = parse_candidates(text)
    assert len(result) == 2
    assert result[0]["name"] == "Ivan Petrov"
    assert result[1]["name"] == "John Smith"

def test_parse_candidates_ignore_blocks_with_only_unknown_fields():
    text = """
Name: Ivan Petrov
Email: ivan@example.com

UnknownField: some value

OnlyUnknownField: another value

Name: John Smith
Phone: 123-456-7890
"""
    result = parse_candidates(text)
    assert len(result) == 2
    assert result[0]["name"] == "Ivan Petrov"
    assert result[1]["name"] == "John Smith"

def test_summarize_candidate():
    info = {"name": "Ivan Petrov", "company": "Megalift", "email": "ivan@example.com", "phone": "123"}
    assert summarize_candidate(info) == "Ivan Petrov at Megalift (ivan@example.com, 123)"

    info = {"name": "Ivan Petrov", "company": None, "email": None, "phone": None}
    assert summarize_candidate(info) == "Ivan Petrov"

    info = {"name": None, "company": "Megalift", "email": "ivan@example.com", "phone": None}
    assert summarize_candidate(info) == "Unknown candidate at Megalift (ivan@example.com)"

    info = {"name": "Ivan Petrov", "company": "Megalift", "email": None, "phone": None}
    assert summarize_candidate(info) == "Ivan Petrov at Megalift"

    info = {"name": "Ivan Petrov", "company": None, "email": None, "phone": "123"}
    assert summarize_candidate(info) == "Ivan Petrov (123)"

    info = {"name": "", "company": "Megalift", "email": "ivan@example.com", "phone": None}
    assert summarize_candidate(info) == "Unknown candidate at Megalift (ivan@example.com)"

def test_get_candidate_contact_methods():
    # Email only
    assert get_candidate_contact_methods({"email": "test@example.com", "phone": None}) == ["email"]
    
    # Phone only
    assert get_candidate_contact_methods({"email": None, "phone": "123-456-7890"}) == ["phone"]
    
    # Both
    assert get_candidate_contact_methods({"email": "test@example.com", "phone": "123-456-7890"}) == ["email", "phone"]
    
    # Neither
    assert get_candidate_contact_methods({"email": None, "phone": None}) == []
    
    # Missing keys
    assert get_candidate_contact_methods({}) == []
    
    # Whitespace-only values
    assert get_candidate_contact_methods({"email": "   ", "phone": "123"}) == ["phone"]
    assert get_candidate_contact_methods({"email": "test@example.com", "phone": "   "}) == ["email"]
    assert get_candidate_contact_methods({"email": "   ", "phone": "   "}) == []
    
    # Empty string values
    assert get_candidate_contact_methods({"email": "", "phone": "123"}) == ["phone"]
    assert get_candidate_contact_methods({"email": "test@example.com", "phone": ""}) == ["email"]
    assert get_candidate_contact_methods({"email": "", "phone": ""}) == []

def test_has_complete_contact():
    # Complete contact
    assert has_complete_contact({"email": "test@example.com", "phone": "123-456-7890"}) == True
    
    # Email only
    assert has_complete_contact({"email": "test@example.com", "phone": None}) == False
    assert has_complete_contact({"email": "test@example.com", "phone": ""}) == False
    assert has_complete_contact({"email": "test@example.com", "phone": "   "}) == False
    
    # Phone only
    assert has_complete_contact({"email": None, "phone": "123-456-7890"}) == False
    assert has_complete_contact({"email": "", "phone": "123-456-7890"}) == False
    assert has_complete_contact({"email": "   ", "phone": "123-456-7890"}) == False
    
    # Missing keys
    assert has_complete_contact({}) == False
    assert has_complete_contact({"email": "test@example.com"}) == False
    assert has_complete_contact({"phone": "123-456-7890"}) == False
    
    # None values
    assert has_complete_contact({"email": None, "phone": None}) == False
    
    # Empty strings
    assert has_complete_contact({"email": "", "phone": ""}) == False
    
    # Whitespace-only strings
    assert has_complete_contact({"email": "   ", "phone": "   "}) == False

def test_extract_candidate_names():
    candidates = [
        {"name": "Ivan Petrov", "email": "ivan@example.com"},
        {"name": None, "email": "john@example.com"},
        {"name": "", "email": "jane@example.com"},
        {"name": "   ", "email": "bob@example.com"},
        {"name": "Ivan Petrov", "email": "ivan2@example.com"},
        {"name": "  John Smith  ", "email": "john2@example.com"}
    ]
    
    result = extract_candidate_names(candidates)
    assert result == ["Ivan Petrov", "Ivan Petrov", "John Smith"]
    
    # Test with empty list
    assert extract_candidate_names([]) == []
    
    # Test with all None/empty names
    candidates_all_empty = [
        {"name": None, "email": "ivan@example.com"},
        {"name": "", "email": "john@example.com"},
        {"name": "   ", "email": "jane@example.com"}
    ]
    assert extract_candidate_names(candidates_all_empty) == []

def test_extract_candidate_names_edge_cases():
    # Normal names
    candidates = [
        {"name": "Alice", "email": "alice@example.com"},
        {"name": "Bob", "email": "bob@example.com"}
    ]
    assert extract_candidate_names(candidates) == ["Alice", "Bob"]
    
    # Missing/None names
    candidates = [
        {"name": None, "email": "alice@example.com"},
        {"name": "Bob", "email": "bob@example.com"},
        {"name": "", "email": "charlie@example.com"}
    ]
    assert extract_candidate_names(candidates) == ["Bob"]
    
    # Whitespace trimming
    candidates = [
        {"name": "  Alice  ", "email": "alice@example.com"},
        {"name": "\tBob\n", "email": "bob@example.com"}
    ]
    assert extract_candidate_names(candidates) == ["Alice", "Bob"]
    
    # Empty names
    candidates = [
        {"name": "", "email": "alice@example.com"},
        {"name": "Bob", "email": "bob@example.com"},
        {"name": "   ", "email": "charlie@example.com"}
    ]
    assert extract_candidate_names(candidates) == ["Bob"]
    
    # Duplicate names
    candidates = [
        {"name": "Alice", "email": "alice1@example.com"},
        {"name": "Alice", "email": "alice2@example.com"},
        {"name": "Bob", "email": "bob@example.com"}
    ]
    assert extract_candidate_names(candidates) == ["Alice", "Alice", "Bob"]

def test_filter_candidates_by_company():
    candidates = [
        {"name": "Ivan Petrov", "company": "Megalift"},
        {"name": "John Smith", "company": "Acme Corp"},
        {"name": "Jane Doe", "company": "megalift"},
        {"name": "Bob Wilson", "company": "   Megalift   "},
        {"name": "Alice Brown", "company": None},
        {"name": "Charlie Davis", "company": ""},
        {"name": "David Miller", "company": "   "},
    ]
    
    # Case insensitive match
    result = filter_candidates_by_company(candidates, "megalift")
    assert len(result) == 3
    assert result[0]["name"] == "Ivan Petrov"
    assert result[1]["name"] == "Jane Doe"
    assert result[2]["name"] == "Bob Wilson"
    
    # Whitespace trimming
    result = filter_candidates_by_company(candidates, "   megalift   ")
    assert len(result) == 3
    assert result[0]["name"] == "Ivan Petrov"
    assert result[1]["name"] == "Jane Doe"
    assert result[2]["name"] == "Bob Wilson"
    
    # Empty search
    result = filter_candidates_by_company(candidates, "")
    assert result == []
    
    # Whitespace-only search
    result = filter_candidates_by_company(candidates, "   ")
    assert result == []
    
    # Missing company
    result = filter_candidates_by_company(candidates, "Nonexistent")
    assert result == []
    
    # None company
    result = filter_candidates_by_company(candidates, "Acme Corp")
    assert len(result) == 1
    assert result[0]["name"] == "John Smith"
    
    # Empty company
    result = filter_candidates_by_company(candidates, "   ")
    assert result == []
    
    # Preserve original dictionary identity
    original = candidates[0]
    result = filter_candidates_by_company(candidates, "megalift")
    assert result[0] is original

def test_dedupe_candidates_by_email():
    first = {"name": "Ivan", "email": " Ivan@Example.com "}
    duplicate = {"name": "Ivan Duplicate", "email": "ivan@example.com"}
    second = {"name": "Anna", "email": "anna@example.com"}
    candidates = [
        {"name": "No Email"},
        first,
        {"name": "Blank", "email": "   "},
        duplicate,
        second,
        {"name": "None", "email": None},
        {"name": "Anna Duplicate", "email": " ANNA@EXAMPLE.COM "},
    ]

    result = dedupe_candidates_by_email(candidates)

    assert result == [first, second]
    assert result[0] is first
    assert result[1] is second

def test_dedupe_candidates_by_email_empty_cases():
    assert dedupe_candidates_by_email([]) == []
    assert dedupe_candidates_by_email(
        [
            {"name": "No Email"},
            {"email": None},
            {"email": ""},
            {"email": "   "},
        ]
    ) == []

def test_group_candidate_names_by_company():
    candidates = [
        {"name": "Ivan Petrov", "company": "Megalift"},
        {"name": "John Smith", "company": "Acme Corp"},
        {"name": "Jane Doe", "company": "megalift"},
        {"name": "Bob Wilson", "company": "   Megalift   "},
        {"name": "Alice Brown", "company": None},
        {"name": "Charlie Davis", "company": ""},
        {"name": "David Miller", "company": "   "},
        {"name": "  Eve Green  ", "company": "Acme Corp"},
        {"name": "Frank White", "company": "Megalift"},
        {"name": "", "company": "Test"},
        {"name": "Grace Black", "company": "Test"},
    ]
    
    result = group_candidate_names_by_company(candidates)
    
    assert len(result) == 3
    assert "megalift" in result
    assert "acme corp" in result
    assert "test" in result
    
    # Check Megalift names (order preserved, duplicates allowed)
    assert result["megalift"] == ["Ivan Petrov", "Jane Doe", "Bob Wilson", "Frank White"]
    
    # Check Acme Corp names (order preserved, duplicates allowed)
    assert result["acme corp"] == ["John Smith", "Eve Green"]

    # Check Test names skip blank names but keep valid names
    assert result["test"] == ["Grace Black"]
    
    # Test with empty list
    assert group_candidate_names_by_company([]) == {}
    
    # Test with all invalid candidates
    invalid_candidates = [
        {"name": None, "company": "Test"},
        {"name": "", "company": "Test"},
        {"name": "  ", "company": "Test"},
        {"name": "John", "company": None},
        {"name": "Jane", "company": ""},
        {"name": "Bob", "company": "   "},
    ]
    assert group_candidate_names_by_company(invalid_candidates) == {}

def test_group_candidate_names_by_company_case_insensitive():
    candidates = [
        {"name": "Ivan Petrov", "company": "MEGALIFT"},
        {"name": "John Smith", "company": "megalift"},
        {"name": "Jane Doe", "company": "Megalift"},
    ]
    
    result = group_candidate_names_by_company(candidates)
    
    assert len(result) == 1
    assert "megalift" in result
    assert result["megalift"] == ["Ivan Petrov", "John Smith", "Jane Doe"]

def test_group_candidate_names_by_company_whitespace():
    candidates = [
        {"name": "  Ivan Petrov  ", "company": "   Megalift   "},
        {"name": "\tJohn Smith\n", "company": "Acme Corp"},
    ]
    
    result = group_candidate_names_by_company(candidates)
    
    assert len(result) == 2
    assert result["megalift"] == ["Ivan Petrov"]
    assert result["acme corp"] == ["John Smith"]

def test_group_candidate_names_by_company_duplicate_names():
    candidates = [
        {"name": "Ivan Petrov", "company": "Megalift"},
        {"name": "Ivan Petrov", "company": "Megalift"},
        {"name": "John Smith", "company": "Megalift"},
    ]
    
    result = group_candidate_names_by_company(candidates)
    
    assert len(result) == 1
    assert result["megalift"] == ["Ivan Petrov", "Ivan Petrov", "John Smith"]

def test_normalize_email():
    assert normalize_email(" Test@Example.Com ") == "test@example.com"
    assert normalize_email("") is None
    assert normalize_email("   ") is None
    assert normalize_email(None) is None
    assert normalize_email(123) is None

def test_normalize_phone():
    assert normalize_phone(" 123-456-7890 ") == "+1234567890"
    assert normalize_phone("+1 (234) 567-8901") == "+12345678901"
    assert normalize_phone("123456") is None  # fewer than 7 digits
    assert normalize_phone("") is None
    assert normalize_phone("   ") is None
    assert normalize_phone(None) is None
    assert normalize_phone(123) is None

def test_is_valid_email():
    # Valid emails
    assert is_valid_email("test@example.com") == True
    assert is_valid_email("user.name@domain.co.uk") == True
    assert is_valid_email("a@b.co") == True
    
    # Invalid: no @
    assert is_valid_email("testexample.com") == False
    
    # Invalid: multiple @
    assert is_valid_email("test@@example.com") == False
    
    # Invalid: empty local part
    assert is_valid_email("@example.com") == False
    
    # Invalid: empty domain
    assert is_valid_email("test@") == False
    
    # Invalid: no dot in domain
    assert is_valid_email("test@example") == False
    
    # Invalid: blank
    assert is_valid_email("") == False
    assert is_valid_email("   ") == False
    
    # Invalid: None/numeric
    assert is_valid_email(None) == False
    assert is_valid_email(123) == False

def test_is_valid_phone():
    # Valid phones (7+ digits after cleaning)
    assert is_valid_phone("123-456-7890") == True
    assert is_valid_phone("+1 (234) 567-8901") == True
    assert is_valid_phone("1234567") == True
    
    # Invalid: fewer than 7 digits
    assert is_valid_phone("123456") == False
    assert is_valid_phone("123") == False
    
    # Invalid: blank
    assert is_valid_phone("") == False
    assert is_valid_phone("   ") == False
    
    # Invalid: None/numeric
    assert is_valid_phone(None) == False
    assert is_valid_phone(123) == False

def test_normalize_email():
    assert normalize_email(" Test@Example.Com ") == "test@example.com"
    assert normalize_email("") is None
    assert normalize_email("   ") is None
    assert normalize_email(None) is None
    assert normalize_email(123) is None
    assert normalize_email("invalid.email") is None
    assert normalize_email("@example.com") is None
    assert normalize_email("test@") is None

def test_normalize_phone():
    assert normalize_phone(" 123-456-7890 ") == "+1234567890"
    assert normalize_phone("+1 (234) 567-8901") == "+12345678901"
    assert normalize_phone("123456") is None  # fewer than 7 digits
    assert normalize_phone("") is None
    assert normalize_phone("   ") is None
    assert normalize_phone(None) is None
    assert normalize_phone(123) is None

def test_format_candidate_report():
    # Test with complete records
    candidates = [
        {"name": "Ivan Petrov", "company": "Megalift", "email": "ivan@example.com", "phone": "123-456-7890"},
        {"name": "John Smith", "company": "Acme Corp", "email": "john@example.com", "phone": ""},
    ]
    
    result = format_candidate_report(candidates)
    expected = "Ivan Petrov - Megalift - ivan@example.com, 123-456-7890\nJohn Smith - Acme Corp - john@example.com"
    assert result == expected
    
    # Test with missing name and company
    candidates = [
        {"name": "", "company": "", "email": "ivan@example.com", "phone": "123-456-7890"},
        {"name": "John Smith", "company": "Acme Corp", "email": "", "phone": "123-456-7890"},
    ]
    
    result = format_candidate_report(candidates)
    expected = "John Smith - Acme Corp - 123-456-7890"
    assert result == expected
    
    # Test with no contact info
    candidates = [
        {"name": "Ivan Petrov", "company": "Megalift", "email": "", "phone": ""},
    ]
    
    result = format_candidate_report(candidates)
    expected = "Ivan Petrov - Megalift - no contact"
    assert result == expected
    
    # Test with whitespace-only fields
    candidates = [
        {"name": "  Ivan Petrov  ", "company": "   Megalift   ", "email": "  ivan@example.com  ", "phone": "  123-456-7890  "},
    ]
    
    result = format_candidate_report(candidates)
    expected = "Ivan Petrov - Megalift - ivan@example.com, 123-456-7890"
    assert result == expected
    
    # Test with empty input
    result = format_candidate_report([])
    assert result == ""
    
    # Test with all blank records (should be skipped)
    candidates = [
        {"name": "", "company": "", "email": "", "phone": ""},
        {"name": "   ", "company": "   ", "email": "   ", "phone": "   "},
    ]
    
    result = format_candidate_report(candidates)
    assert result == ""

def test_redact_contact():
    # Test with email only
    info = {"name": "Ivan Petrov", "email": "ivan@example.com", "company": "Megalift"}
    result = redact_contact(info)
    assert result["email"] == "i***@example.com"
    assert result["name"] == "Ivan Petrov"
    assert result["company"] == "Megalift"
    # Original dict should not be modified
    assert info["email"] == "ivan@example.com"

    # Test with phone only
    info = {"name": "John Smith", "phone": "123-456-7890"}
    result = redact_contact(info)
    assert result["phone"] == "***7890"
    assert result["name"] == "John Smith"

    # Test with both email and phone
    info = {"email": "test@example.com", "phone": "+1 (234) 567-8901"}
    result = redact_contact(info)
    assert result["email"] == "t***@example.com"
    assert result["phone"] == "***8901"

    # Test with blank values
    info = {"email": "", "phone": "   "}
    result = redact_contact(info)
    assert result["email"] == ""
    assert result["phone"] == "   "

    # Test with None values
    info = {"email": None, "phone": None}
    result = redact_contact(info)
    assert result["email"] is None
    assert result["phone"] is None

    # Test with no contact info
    info = {"name": "Ivan Petrov", "company": "Megalift"}
    result = redact_contact(info)
    assert result == info

    # Test that original dict is not mutated
    original_info = {"email": "ivan@example.com", "phone": "123-456-7890"}
    original_copy = {"email": "ivan@example.com", "phone": "123-456-7890"}
    redact_contact(original_info)
    assert original_info == original_copy
    
    # Test with mixed complete/incomplete records
    candidates = [
        {"name": "Ivan Petrov", "company": "Megalift", "email": "ivan@example.com", "phone": "123-456-7890"},
        {"name": "", "company": "Acme Corp", "email": "", "phone": ""},
        {"name": "John Smith", "company": "", "email": "john@example.com", "phone": ""},
        {"name": "", "company": "", "email": "", "phone": ""},
    ]
    
    result = format_candidate_report(candidates)
    expected = "Ivan Petrov - Megalift - ivan@example.com, 123-456-7890\nUnknown candidate - Acme Corp - no contact\nJohn Smith - Unknown company - john@example.com"
    assert result == expected
