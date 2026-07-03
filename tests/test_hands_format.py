from hands_probe.format_tools import join_nonblank, trim_or_none, slugify, format_phone_us, normalize_date_iso, dedupe_labels, render_markdown_table


def test_trim_or_none():
    assert trim_or_none("  value  ") == "value"
    assert trim_or_none("   ") is None
    assert trim_or_none(None) is None


def test_join_nonblank():
    assert join_nonblank([" Alice ", "", "Smith"]) == "Alice Smith"
    assert join_nonblank(["a", " b ", None], separator="-") == "a-b"
    assert join_nonblank([]) == ""


def test_slugify():
    # Test basic functionality
    assert slugify("Hello World") == "hello-world"
    
    # Test with special characters
    assert slugify("Hello@World#Test") == "hello-world-test"
    
    # Test multiple consecutive non-alphanumeric chars
    assert slugify("a!!!b") == "a-b"
    
    # Test leading/trailing spaces and case conversion
    assert slugify("  Hello World  ") == "hello-world"
    
    # Test None input
    assert slugify(None) == ""
    
    # Test non-string input
    assert slugify(123) == ""
    
    # Test empty string
    assert slugify("") == ""
    
    # Test blank string with whitespace
    assert slugify("   ") == ""
    
    # Test no alphanumeric characters
    assert slugify("!@#$%") == ""
    
    # Test only alphanumeric characters
    assert slugify("abc123def") == "abc123def"
    
    # Test mixed case and special chars
    assert slugify("Hello, World! 123") == "hello-world-123"


def test_format_phone_us():
    # Valid 10-digit number
    assert format_phone_us("1234567890") == "(123) 456-7890"
    
    # Valid 11-digit number starting with 1
    assert format_phone_us("12345678901") == "+1 (234) 567-8901"
    
    # Valid 10-digit number with spaces and dashes
    assert format_phone_us("123-456-7890") == "(123) 456-7890"
    
    # Valid 11-digit number with spaces and dashes
    assert format_phone_us("1-234-567-8901") == "+1 (234) 567-8901"
    
    # Invalid: None input
    assert format_phone_us(None) is None
    
    # Invalid: non-string input
    assert format_phone_us(123) is None
    
    # Invalid: blank string
    assert format_phone_us("") is None
    
    # Invalid: 9 digits
    assert format_phone_us("123456789") is None
    
    # Invalid: 12 digits
    assert format_phone_us("123456789012") is None
    
    # Invalid: doesn't start with 1 for 11-digit number
    assert format_phone_us("22345678901") is None
    
    # Invalid: contains non-digits (other than separators)
    assert format_phone_us("123-456-789a") is None


def test_normalize_date_iso():
    # Valid ISO format
    assert normalize_date_iso("2023-01-15") == "2023-01-15"
    
    # Valid DD.MM.YYYY format
    assert normalize_date_iso("15.01.2023") == "2023-01-15"
    
    # Valid MM/DD/YYYY format
    assert normalize_date_iso("01/15/2023") == "2023-01-15"
    
    # Invalid: None input
    assert normalize_date_iso(None) is None
    
    # Invalid: non-string input
    assert normalize_date_iso(123) is None
    
    # Invalid: blank string
    assert normalize_date_iso("") is None
    assert normalize_date_iso("   ") is None
    
    # Invalid: impossible date (e.g., Feb 30)
    assert normalize_date_iso("2023-02-30") is None
    
    # Invalid: invalid leap day
    assert normalize_date_iso("2023-02-29") is None
    
    # Invalid format
    assert normalize_date_iso("2023/01/15") is None
    assert normalize_date_iso("15-01-2023") is None


def test_dedupe_labels():
    # Test basic functionality - case insensitive deduping
    input_labels = ["apple", "Apple", "APPLE", "banana", "Banana"]
    result = dedupe_labels(input_labels)
    assert result == ["apple", "banana"]
    
    # Test order preservation
    input_labels = ["zebra", "apple", "ZEBRA", "banana"]
    result = dedupe_labels(input_labels)
    assert result == ["zebra", "apple", "banana"]
    
    # Test blank values ignored
    input_labels = ["apple", "", "  ", None, "banana", "Apple"]
    result = dedupe_labels(input_labels)
    assert result == ["apple", "banana"]
    
    # Test no mutation of input list
    input_labels = ["apple", "Apple", "banana"]
    original = input_labels.copy()
    result = dedupe_labels(input_labels)
    assert input_labels == original  # Input unchanged
    
    # Test all blank values
    input_labels = ["", "  ", None, "   "]
    result = dedupe_labels(input_labels)
    assert result == []
    
    # Test empty list
    result = dedupe_labels([])
    assert result == []


def test_render_markdown_table():
    # Basic functionality with data
    rows = [
        {"name": "Alice", "age": 30, "city": "New York"},
        {"name": "Bob", "age": 25, "city": "Los Angeles"}
    ]
    columns = ["name", "age", "city"]
    result = render_markdown_table(rows, columns)
    expected = "name | age | city\n--- | --- | ---\nAlice | 30 | New York\nBob | 25 | Los Angeles"
    assert result == expected
    
    # With None/missing values
    rows = [
        {"name": "Charlie", "age": None, "city": "Chicago"},
        {"name": "David", "age": 40}
    ]
    columns = ["name", "age", "city"]
    result = render_markdown_table(rows, columns)
    expected = "name | age | city\n--- | --- | ---\nCharlie |  | Chicago\nDavid | 40 | "
    assert result == expected
    
    # With pipe characters in values
    rows = [
        {"name": "Alice|Smith", "age": 30}
    ]
    columns = ["name", "age"]
    result = render_markdown_table(rows, columns)
    expected = "name | age\n--- | ---\nAlice\\|Smith | 30"
    assert result == expected
    
    # Empty columns list
    rows = [{"name": "Alice"}]
    columns = []
    result = render_markdown_table(rows, columns)
    assert result == ""
    
    # Empty rows list
    rows = []
    columns = ["name"]
    result = render_markdown_table(rows, columns)
    expected = "name\n---"
    assert result == expected
    
    # Single row
    rows = [{"name": "Eve", "age": 28}]
    columns = ["name", "age"]
    result = render_markdown_table(rows, columns)
    expected = "name | age\n--- | ---\nEve | 28"
    assert result == expected
    
    # Row with empty string values
    rows = [{"name": "", "age": ""}]
    columns = ["name", "age"]
    result = render_markdown_table(rows, columns)
    expected = "name | age\n--- | ---\n | "
    assert result == expected
    
    # Non-string values in row
    rows = [{"count": 42, "active": True}]
    columns = ["count", "active"]
    result = render_markdown_table(rows, columns)
    expected = "count | active\n--- | ---\n42 | True"
    assert result == expected
