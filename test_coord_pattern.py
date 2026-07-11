import re

COORD_PATTERN = re.compile(r"^(-?\d+\.?\d*),(-?\d+\.?\d*)$")

# Test various formats
test_strings = [
    "1284.191,459.692;",
    "1284.191,459.692",
    ";1284.191,459.692",
]

for test in test_strings:
    match = COORD_PATTERN.match(test)
    print(f"{repr(test)}: {'matches' if match else 'no match'}")

# Now test what happens with the rest string
rest = "1284.191,459.692;"
print(f"\nParsing rest='{rest}':")
match = COORD_PATTERN.match(rest)
if not match:
    print("  No match - this is the problem!")
    # But the extraction code handles this differently - it doesn't include the semicolon
    rest_trimmed = rest.rstrip(";")
    match2 = COORD_PATTERN.match(rest_trimmed)
    print(f"  After rstrip: '{rest_trimmed}' -> {'matches' if match2 else 'no match'}")
