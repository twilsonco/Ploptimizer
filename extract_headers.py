#!/usr/bin/env python3
"""
Extract HPGL header commands from paired PLT reference files.

This script:
1. Groups PLT files by parameter name (removing value suffix)
2. For each parameter pair, diffs the files to extract the command prefix
3. Parses headers.txt to get metadata (type, default, min, max, units)
4. Updates job-config.json with the extracted tool_options

The tool_options structure supports dual defaults (text vs borders_holes)
to allow different engraver settings for text vs structural toolpaths.
"""

import json
import re
import subprocess
from pathlib import Path
from collections import defaultdict


def parse_headers_txt(headers_path: Path) -> dict:
    """Parse headers.txt into structured metadata.
    
    Format: <name> [<units>] <type> <default> [<min> <max>]
    
    Args:
        headers_path: Path to headers.txt
        
    Returns:
        Dict mapping underscored names to metadata dicts
    """
    headers = {}
    with open(headers_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            
            # Parse: "cutting velocity [in/sec] float 0.8 0.05 3.0"
            parts = line.split()
            
            # Extract name (everything before [units])
            name_parts = []
            idx = 0
            while idx < len(parts) and not parts[idx].startswith('['):
                name_parts.append(parts[idx])
                idx += 1
            
            name = ' '.join(name_parts)
            underscored_name = name.replace(' ', '_')
            
            # Extract units (inside brackets)
            units = None
            if idx < len(parts) and parts[idx].startswith('['):
                units = parts[idx][1:-1]  # Remove brackets
                idx += 1
            
            # Extract type, default, min, max
            param_type = parts[idx]
            idx += 1
            
            default_str = parts[idx]
            idx += 1
            
            # Parse type-specific default
            if param_type == 'bool':
                default = default_str.lower() == 'true'
                min_val = None
                max_val = None
            elif param_type == 'int':
                default = int(default_str)
                min_val = int(parts[idx])
                max_val = int(parts[idx + 1])
                idx += 2
            elif param_type == 'float':
                default = float(default_str)
                min_val = float(parts[idx])
                max_val = float(parts[idx + 1])
                idx += 2
            else:
                raise ValueError(f"Unknown type: {param_type}")
            
            headers[underscored_name] = {
                'name': name,
                'type': param_type,
                'default': default,
                'min': min_val,
                'max': max_val,
                'units': units,
            }
    
    return headers


def group_plt_files(headers_dir: Path) -> dict:
    """Group PLT files by parameter name.
    
    Args:
        headers_dir: Directory containing PLT files
        
    Returns:
        Dict mapping parameter name to list of (file_path, value_str) tuples
    """
    groups = defaultdict(list)
    
    for plt_file in headers_dir.glob('*.plt'):
        # Extract name and value, e.g. "dwell time 50.plt" -> ("dwell time", "50")
        name_match = re.match(r'^(.+?)\s+([^\s]+)\.plt$', plt_file.name)
        if not name_match:
            continue
        
        param_name = name_match.group(1)
        value_str = name_match.group(2)
        
        groups[param_name].append((plt_file, value_str))
    
    return dict(groups)


def extract_command_from_diff(file1: Path, file2: Path) -> str:
    """Extract the HPGL command prefix by diffing two files.
    
    Finds the line that changed and extracts the command prefix.
    E.g., "VS0.80;" vs "VS1.50;" -> "VS"
         "ZO124,50;" vs "ZO124,125;" -> "ZO124,"
    
    Args:
        file1, file2: Two PLT files to diff
        
    Returns:
        The command prefix (e.g., "VS", "ZO124,", "ZU")
    """
    result = subprocess.run(
        ['diff', '-u', str(file1), str(file2)],
        capture_output=True,
        text=True,
    )
    
    # Extract lines that were removed (-) and added (+), skip diff headers
    diff_lines = result.stdout.split('\n')
    removed_lines = [l[1:].strip() for l in diff_lines if l.startswith('-') and not l.startswith('---')]
    added_lines = [l[1:].strip() for l in diff_lines if l.startswith('+') and not l.startswith('+++')]
    
    # We should have exactly one removed and one added line
    if len(removed_lines) != 1 or len(added_lines) != 1:
        raise ValueError(
            f"Expected 1 removed and 1 added line in diff of {file1.name} and {file2.name}, "
            f"got {len(removed_lines)} removed and {len(added_lines)} added.\n"
            f"Removed: {removed_lines}\nAdded: {added_lines}"
        )
    
    line1 = removed_lines[0]
    line2 = added_lines[0]
    
    # Remove trailing semicolon
    if line1.endswith(';'):
        line1 = line1[:-1]
    if line2.endswith(';'):
        line2 = line2[:-1]
    
    # Find common prefix, but stop at HPGL command boundaries (comma)
    # E.g., "ZO100,12000" vs "ZO100,16000" should stop at "ZO100,"
    prefix_len = 0
    for c1, c2 in zip(line1, line2):
        if c1 == c2:
            prefix_len += 1
            # Stop after a comma (command parameter separator)
            if c1 == ',':
                break
        else:
            break
    
    prefix = line1[:prefix_len]
    
    # Ensure we got a valid command
    if not prefix or len(prefix) < 2:
        raise ValueError(
            f"Could not extract command from diff:\n{line1};\nvs\n{line2};"
        )
    
    return prefix


def extract_all_commands(headers_dir: Path) -> dict:
    """Extract commands for all parameters.
    
    Args:
        headers_dir: Directory containing PLT files
        
    Returns:
        Dict mapping parameter name to command prefix
    """
    groups = group_plt_files(headers_dir)
    commands = {}
    
    for param_name, files in sorted(groups.items()):
        if len(files) < 2:
            print(f"Warning: {param_name} has only {len(files)} file(s), skipping")
            continue
        
        file1, val1 = files[0]
        file2, val2 = files[1]
        
        command = extract_command_from_diff(file1, file2)
        commands[param_name] = command
        print(f"✓ {param_name:25} -> {command}")
    
    return commands


def build_tool_options(headers: dict, commands: dict) -> dict:
    """Build the tool_options structure for job-config.json.
    
    Args:
        headers: Output from parse_headers_txt (keyed by underscored name)
        commands: Output from extract_all_commands (keyed by original param name)
        
    Returns:
        Dict with tool_options ready for job-config.json
    """
    # Build reverse mapping from original names to underscored names
    name_mapping = {}
    for underscored_name, header_info in headers.items():
        original_name = header_info['name']
        name_mapping[original_name] = underscored_name
    
    tool_options = {}
    
    for original_name, command in sorted(commands.items()):
        # Map original name to underscored version
        if original_name not in name_mapping:
            print(f"Warning: Could not find underscored name for {original_name}")
            continue
        
        underscored_name = name_mapping[original_name]
        header_info = headers[underscored_name]
        
        param_type = header_info['type']
        default = header_info['default']
        
        # Build dual defaults (text and borders_holes use same default)
        dual_default = {
            'text': default,
            'borders_holes': default,
        }
        
        option = {
            'name': original_name,
            'description': '',
            'command': command,
            'type': param_type,
            'default': dual_default,
        }
        
        # Add bounds for int/float types
        if param_type in ('int', 'float'):
            option['min'] = header_info['min']
            option['max'] = header_info['max']
        
        tool_options[underscored_name] = option
    
    return tool_options


def update_job_config(job_config_path: Path, tool_options: dict) -> None:
    """Update job-config.json with tool_options.
    
    Args:
        job_config_path: Path to job-config.json
        tool_options: Tool options dict to insert
    """
    with open(job_config_path) as f:
        config = json.load(f)
    
    config['tool_options'] = tool_options
    
    with open(job_config_path, 'w') as f:
        json.dump(config, f, indent=2)
    
    print(f"\n✓ Updated {job_config_path} with {len(tool_options)} tool options")


def main():
    """Main extraction and update flow."""
    repo_root = Path(__file__).parent
    headers_dir = repo_root / 'examples' / 'plt headers'
    headers_file = headers_dir / 'headers.txt'
    job_config_file = repo_root / 'job-config.json'
    
    print("=" * 60)
    print("PLT Headers Extraction")
    print("=" * 60)
    
    # Step 1: Parse headers.txt
    print("\n1. Parsing headers.txt...")
    headers = parse_headers_txt(headers_file)
    print(f"   Found {len(headers)} headers:")
    for name in sorted(headers.keys()):
        print(f"     - {name}")
    
    # Step 2: Extract commands from PLT files
    print("\n2. Extracting commands from PLT file pairs...")
    commands = extract_all_commands(headers_dir)
    
    # Step 3: Build tool_options structure
    print("\n3. Building tool_options structure...")
    tool_options = build_tool_options(headers, commands)
    print(f"   Created {len(tool_options)} tool options")
    
    # Step 4: Update job-config.json
    print("\n4. Updating job-config.json...")
    update_job_config(job_config_file, tool_options)
    
    # Summary
    print("\n" + "=" * 60)
    print("Summary:")
    print(f"  Headers parsed: {len(headers)}")
    print(f"  Commands extracted: {len(commands)}")
    print(f"  Tool options created: {len(tool_options)}")
    print("=" * 60 + "\n")
    
    return 0


if __name__ == '__main__':
    exit(main())
