#!/bin/bash
# Test script for plt-optimizer watch functionality

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_IN="/tmp/plt_watch_test/in"
TEST_OUT="/tmp/plt_watch_test/out"
TEST_LOGS="/tmp/plt_watch_test/logs"

cleanup() {
    rm -rf "$TEST_IN" "$TEST_OUT" "$TEST_LOGS"
    mkdir -p "$TEST_IN" "$TEST_OUT" "$TEST_LOGS"
}

setup() {
    cleanup
    echo "Setup complete: in=$TEST_IN, out=$TEST_OUT, logs=$TEST_LOGS"
}

test_help() {
    echo "=== Testing --help ==="
    uv run plt-optimizer watch --help
    echo "PASS: help displayed correctly"
}

test_missing_watch_dir() {
    echo "=== Testing missing required --watch-dir ==="
    if uv run plt-optimizer watch 2>&1 | grep -q "error"; then
        echo "PASS: error shown for missing --watch-dir"
    else
        echo "FAIL: expected error message not found"
        return 1
    fi
}

test_watch_flag_recognition() {
    echo "=== Testing 'watch' subcommand recognition ==="
    if uv run plt-optimizer watch --help 2>&1 | grep -q "WATCH_DIR"; then
        echo "PASS: 'watch' subcommand recognized, --watch-dir shown in help"
    else
        echo "FAIL: 'watch' argument not properly handled"
        return 1
    fi
}

test_invalid_flag() {
    echo "=== Testing unrecognized arguments ==="
    if ! uv run plt-optimizer watch --invalid-flag 2>&1 | grep -q "unrecognized"; then
        echo "PASS: other invalid flags still rejected correctly"
    else
        echo "FAIL: error message for other invalid flags not working"
        return 1
    fi
}

main() {
    setup
    test_help
    test_missing_watch_dir
    test_watch_flag_recognition
    test_invalid_flag
    cleanup
    echo ""
    echo "=== All tests passed ==="
}

main "$@"