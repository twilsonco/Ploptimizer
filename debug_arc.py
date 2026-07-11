from plt_optimizer.core.parser import PLTParser
from pathlib import Path

# Get the actual tokens around the problem area
parser = PLTParser()
content = Path('examples/2026-07-10 SW0914 1111sheet1.plt').read_text()

# Find the arc token
import re
COMMAND_PATTERN = re.compile(r'([A-Z][A-Z0-9,.\-:]*?;)')
tokens_raw = [m.group(1) for m in COMMAND_PATTERN.finditer(content)]

# Find 1102.971
for i, tok in enumerate(tokens_raw):
    if '1102.971' in tok:
        print(f"Token {i}: {tok}")
        if i > 0:
            print(f"Token {i-1}: {tokens_raw[i-1]}")
        if i < len(tokens_raw) - 1:
            print(f"Token {i+1}: {tokens_raw[i+1]}")
        if i < len(tokens_raw) - 2:
            print(f"Token {i+2}: {tokens_raw[i+2]}")

# Parse and check arc endpoint
print("\n" + "="*60)
print("Parsing with debugger:")

# Let's manually trace what happens
last_pos = None

# Find where we are before the arc
for i, tok in enumerate(tokens_raw):
    if tok == "PU1102.971,513.720,12.574;":
        print(f"Found token {i}: {tok}")
        # Before this is probably PD
        if i > 0:
            print(f"Previous token {i-1}: {tokens_raw[i-1]}")
    if '1102.971' in tok and 'AA' in tok:
        print(f"\nFound arc at token {i}: {tok}")
        # The AA parameters are after AA
        params_str = tok[2:].rstrip(';')  # Remove AA and ;
        print(f"Arc parameters: {params_str}")
        
        # To compute the arc endpoint, we need the starting position
        # Which should be the last PD coordinate
        break
