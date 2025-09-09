#!/bin/bash

#############################################
# Agent Installation Testing Script
# Tests deployed agents to ensure proper installation
# Used by various SUP installers for validation
#############################################

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log() {
    echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

success() {
    echo -e "${GREEN}✓${NC} $1"
}

usage() {
    echo "Usage: $0 --agent=AGENT_TYPE --path=INSTALL_PATH"
    echo ""
    echo "Options:"
    echo "  --agent=TYPE      Agent type to test (MCHP, SMOL, or BU)"
    echo "  --path=PATH       Base installation directory"
    echo "  --help            Display this help message"
    echo ""
    echo "Examples:"
    echo "  $0 --agent=MCHP --path=/home/user/DOLOS-DEPLOY"
    echo "  $0 --agent=SMOL --path=/home/user/DOLOS-DEPLOY"
    echo "  $0 --agent=BU --path=/home/user/DOLOS-DEPLOY"
}

# Parse arguments
AGENT_TYPE=""
INSTALL_PATH=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --agent=*)
            AGENT_TYPE="${1#*=}"
            ;;
        --path=*)
            INSTALL_PATH="${1#*=}"
            ;;
        --help)
            usage
            exit 0
            ;;
        *)
            error "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
    shift
done

# Validate arguments
if [ -z "$AGENT_TYPE" ]; then
    error "Agent type is required"
    usage
    exit 1
fi

if [ -z "$INSTALL_PATH" ]; then
    error "Installation path is required"
    usage
    exit 1
fi

# Set agent-specific paths
case $AGENT_TYPE in
    MCHP)
        AGENT_DIR="$INSTALL_PATH/deployed_sups/MCHP"
        RUN_SCRIPT="$AGENT_DIR/run_mchp.sh"
        MAIN_SCRIPT="$AGENT_DIR/pyhuman/human.py"
        SERVICE_NAME="mchp"
        ;;
    SMOL)
        AGENT_DIR="$INSTALL_PATH/deployed_sups/SMOL"
        RUN_SCRIPT="$AGENT_DIR/run_smol.sh"
        MAIN_SCRIPT="$AGENT_DIR/agent.py"
        SERVICE_NAME="smol"
        ;;
    BU)
        AGENT_DIR="$INSTALL_PATH/deployed_sups/BU"
        RUN_SCRIPT="$AGENT_DIR/run_bu.sh"
        MAIN_SCRIPT="$AGENT_DIR/agent.py"
        SERVICE_NAME="bu"
        ;;
    *)
        error "Unknown agent type: $AGENT_TYPE"
        echo "Valid options: MCHP, SMOL, BU"
        exit 1
        ;;
esac

log "Starting $AGENT_TYPE agent installation test..."
log "Installation path: $INSTALL_PATH"
log "Agent directory: $AGENT_DIR"

# Test 1: Check directory structure
log "Test 1: Checking directory structure..."
if [ ! -d "$AGENT_DIR" ]; then
    error "$AGENT_TYPE directory not found at $AGENT_DIR"
    exit 1
fi

if [ ! -d "$AGENT_DIR/logs" ]; then
    error "Logs directory not found at $AGENT_DIR/logs"
    exit 1
fi

if [ ! -d "$AGENT_DIR/venv" ]; then
    error "Virtual environment not found at $AGENT_DIR/venv"
    exit 1
fi

success "Directory structure is correct"

# Test 2: Check required scripts exist
log "Test 2: Checking required scripts..."
if [ ! -f "$RUN_SCRIPT" ]; then
    error "Run script not found at $RUN_SCRIPT"
    exit 1
fi

if [ ! -x "$RUN_SCRIPT" ]; then
    error "Run script is not executable: $RUN_SCRIPT"
    exit 1
fi

if [ ! -f "$MAIN_SCRIPT" ]; then
    error "Main agent script not found at $MAIN_SCRIPT"
    exit 1
fi

success "Required scripts are present and executable"

# Test 3: Check virtual environment
log "Test 3: Testing virtual environment..."
if [ ! -f "$AGENT_DIR/venv/bin/activate" ]; then
    error "Virtual environment activation script not found"
    exit 1
fi

if [ ! -f "$AGENT_DIR/venv/bin/python3" ]; then
    error "Python3 not found in virtual environment"
    exit 1
fi

# Test Python version
PYTHON_VERSION=$(cd "$AGENT_DIR" && source venv/bin/activate && python3 --version 2>&1)
log "Python version: $PYTHON_VERSION"

success "Virtual environment is properly configured"

# Test 4: Test Python dependencies
log "Test 4: Testing Python dependencies..."
if [ "$AGENT_TYPE" = "SMOL" ]; then
    # Test SMOL dependencies
    TEST_RESULT=$(cd "$AGENT_DIR" && source venv/bin/activate && python3 -c "
import sys
try:
    import smolagents
    print('✓ smolagents imported successfully')
    import litellm  
    print('✓ litellm imported successfully')
    import os
    print('✓ os imported successfully')
    print('SUCCESS: All SMOL dependencies available')
except ImportError as e:
    print(f'ERROR: Missing dependency - {e}')
    sys.exit(1)
" 2>&1)
elif [ "$AGENT_TYPE" = "BU" ]; then
    # Test BU dependencies  
    TEST_RESULT=$(cd "$AGENT_DIR" && source venv/bin/activate && python3 -c "
import sys
import os
try:
    import browser_use
    print('✓ browser_use imported successfully')
    import selenium
    print('✓ selenium imported successfully')
    import playwright
    print('✓ playwright imported successfully')
    # Set dummy display for pyautogui in headless environments
    if 'DISPLAY' not in os.environ:
        os.environ['DISPLAY'] = ':0'
    try:
        import pyautogui
        print('✓ pyautogui imported successfully')
    except Exception as e:
        print('⚠ pyautogui import skipped (headless environment)')
    import requests
    print('✓ requests imported successfully')
    print('SUCCESS: All BU dependencies available')
except ImportError as e:
    print(f'ERROR: Missing dependency - {e}')
    sys.exit(1)
" 2>&1)
else
    # Test MCHP dependencies  
    TEST_RESULT=$(cd "$AGENT_DIR" && source venv/bin/activate && python3 -c "
import sys
import os
try:
    import selenium
    print('✓ selenium imported successfully')
    # Set dummy display for pyautogui in headless environments
    if 'DISPLAY' not in os.environ:
        os.environ['DISPLAY'] = ':0'
    try:
        import pyautogui
        print('✓ pyautogui imported successfully')
    except Exception as e:
        print('⚠ pyautogui import skipped (headless environment)')
    import time
    print('✓ time imported successfully')
    print('SUCCESS: All MCHP dependencies available')
except ImportError as e:
    print(f'ERROR: Missing dependency - {e}')
    sys.exit(1)
" 2>&1)
fi

if [[ $TEST_RESULT == *"ERROR:"* ]]; then
    error "Python dependency test failed:"
    echo "$TEST_RESULT"
    exit 1
fi

echo "$TEST_RESULT"
success "Python dependencies test passed"

# Test 5: Syntax check on main script
log "Test 5: Performing syntax check on main script..."
# Get relative path from agent directory
RELATIVE_SCRIPT=$(realpath --relative-to="$AGENT_DIR" "$MAIN_SCRIPT")
SYNTAX_CHECK=$(cd "$AGENT_DIR" && source venv/bin/activate && python3 -m py_compile "$RELATIVE_SCRIPT" 2>&1) || SYNTAX_EXIT_CODE=$?

if [ "${SYNTAX_EXIT_CODE:-0}" -ne 0 ]; then
    error "Syntax check failed for $MAIN_SCRIPT:"
    echo "$SYNTAX_CHECK"
    exit 1
fi

success "Syntax check passed"

# Test 6: Quick runtime test (5 second timeout)
log "Test 6: Performing quick runtime test (5 seconds)..."
RUNTIME_TEST=$(cd "$AGENT_DIR" && timeout 5 bash -c "source venv/bin/activate && python3 \"$RELATIVE_SCRIPT\"" 2>&1 || true)

# Check if it's a timeout (expected) vs actual error
if [[ $RUNTIME_TEST == *"Traceback"* ]] && [[ $RUNTIME_TEST != *"KeyboardInterrupt"* ]] && [[ $RUNTIME_TEST != *"TimeoutError"* ]]; then
    error "Runtime test revealed errors in $MAIN_SCRIPT:"
    echo "$RUNTIME_TEST"
    exit 1
fi

success "Runtime test passed (script started without immediate errors)"

# Test 7: Ollama connection for SMOL and BU agents
if [ "$AGENT_TYPE" = "SMOL" ] || [ "$AGENT_TYPE" = "BU" ]; then
    log "Test 7: Testing Ollama connection..."
    if command -v ollama >/dev/null 2>&1; then
        if ollama list >/dev/null 2>&1; then
            OLLAMA_MODELS=$(ollama list | tail -n +2 | wc -l)
            log "Ollama is running with $OLLAMA_MODELS model(s) available"
            success "Ollama connection test passed"
        else
            error "Ollama service is not responding"
            exit 1
        fi
    else
        error "Ollama command not found"
        exit 1
    fi
fi

# Test 8: Check run script functionality
log "Test 8: Testing run script (dry run)..."
# Test that the run script has the correct structure
if grep -q "source.*venv/bin/activate" "$RUN_SCRIPT" && grep -q "python3.*$(basename "$MAIN_SCRIPT")" "$RUN_SCRIPT"; then
    success "Run script has correct structure"
else
    error "Run script does not have expected structure"
    exit 1
fi

# Final summary
echo ""
echo -e "${GREEN}================================${NC}"
echo -e "${GREEN}   ALL TESTS PASSED!${NC}"
echo -e "${GREEN}================================${NC}"
echo ""
success "$AGENT_TYPE agent installation is ready for deployment"
echo ""
echo "Installation Details:"
echo "  • Agent Type: $AGENT_TYPE"
echo "  • Installation Path: $AGENT_DIR"
echo "  • Run Script: $RUN_SCRIPT"
echo "  • Main Script: $MAIN_SCRIPT"
echo "  • Service Name: $SERVICE_NAME"
echo ""
echo "The agent is ready to be started as a systemd service."
echo ""

exit 0