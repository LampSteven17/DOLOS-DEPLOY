#!/bin/bash

# DOLOS-DEPLOY: SUP Installer (MCHP & SMOL)

set -e  # Exit on any command failure
set -u  # Exit on undefined variables
set -o pipefail  # Exit on pipe failures

# Configuration: Default model for SMOL agents (can be modified by user)
# Examples: llama2, mistral, qwen2.5:7b, codellama, phi, llama3:8b
DEFAULT_OLLAMA_MODEL="${DEFAULT_OLLAMA_MODEL:-llama3:8b}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Colors for error reporting
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Error handling function
error_handler() {
    local exit_code=$?
    local line_number=$1
    echo ""
    echo -e "${RED}================================${NC}"
    echo -e "${RED}    INSTALLATION FAILED!${NC}"
    echo -e "${RED}================================${NC}"
    echo ""
    echo -e "${RED}Error occurred at line ${line_number} with exit code ${exit_code}${NC}"
    echo -e "${RED}Command that failed: ${BASH_COMMAND}${NC}"
    echo ""
    echo -e "${YELLOW}Stack trace:${NC}"
    local frame=0
    while caller $frame; do
        ((frame++))
    done
    echo ""
    echo -e "${YELLOW}Please report this error with the following information:${NC}"
    echo "  - Exit code: ${exit_code}"
    echo "  - Failed line: ${line_number}"
    echo "  - Failed command: ${BASH_COMMAND}"
    echo "  - Script arguments: $0 $*"
    echo "  - Working directory: $(pwd)"
    echo "  - User: $(whoami)"
    echo "  - Date: $(date)"
    echo ""
    echo -e "${RED}Installation has been cancelled.${NC}"
    exit $exit_code
}

# Set up error trap
trap 'error_handler ${LINENO}' ERR

# Function to log with timestamp
log() {
    echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1"
}

# Function to log errors
log_error() {
    echo -e "${RED}[$(date +'%Y-%m-%d %H:%M:%S')] ERROR:${NC} $1" >&2
}

usage() {
    echo "Usage: $0 --mchp"
    echo "       $0 --smol --default [--model=MODEL]"
    echo "       $0 --smol --mchp [--model=MODEL]"
    echo "       $0 --smol --improved [--model=MODEL]"
    echo "       $0 --help"
    echo ""
    echo "Options:"
    echo "  --mchp                    Install MCHP (Human simulation)"
    echo "  --smol --default          Install SMOL agent with basic configuration"
    echo "  --smol --mchp             Install SMOL agent with MCHP-like behavior patterns"
    echo "  --smol --improved         Install SMOL agent with PHASE-improved configuration"
    echo "  --model=MODEL             Override default model for SMOL installations"
    echo "                            (e.g., --model=qwen2.5:7b, --model=mistral)"
    echo "  --help                    Display this help message"
}

if [[ $# -eq 0 ]]; then
    usage
    exit 1
fi

case $1 in
    --mchp)
        log "Starting MCHP installation..."
        
        if [ -f "$SCRIPT_DIR/src/MCHP/install_mchp.sh" ]; then
            log "Found MCHP installer script"
            cd "$SCRIPT_DIR/src/MCHP" || {
                log_error "Failed to change to MCHP directory"
                exit 1
            }
            chmod +x install_mchp.sh || {
                log_error "Failed to make install_mchp.sh executable"
                exit 1
            }
            log "Executing MCHP installation script..."
            ./install_mchp.sh --installpath="$SCRIPT_DIR" || {
                log_error "MCHP installation script failed"
                exit 1
            }
            log "MCHP installation completed successfully"
            
            # Test MCHP installation
            log "Testing MCHP installation..."
            if [ -f "$SCRIPT_DIR/src/install_scripts/test_agent.sh" ]; then
                chmod +x "$SCRIPT_DIR/src/install_scripts/test_agent.sh"
                "$SCRIPT_DIR/src/install_scripts/test_agent.sh" --agent=MCHP --path="$SCRIPT_DIR" || {
                    log_error "MCHP installation test failed"
                    exit 1
                }
            else
                log_error "test_agent.sh not found at $SCRIPT_DIR/src/install_scripts/"
                exit 1
            fi
        else
            log_error "install_mchp.sh not found at $SCRIPT_DIR/src/MCHP/"
            exit 1
        fi
        ;;
    --smol)
        # Check if a configuration was specified as second argument
        SMOL_CONFIG=""
        if [[ $# -ge 2 ]]; then
            case $2 in
                --default)
                    SMOL_CONFIG="default"
                    ;;
                --mchp)
                    SMOL_CONFIG="mchp"
                    ;;
                --improved)
                    SMOL_CONFIG="improved"
                    ;;
                *)
                    log_error "Invalid SMOL configuration '$2'"
                    echo "Valid options are: --default, --mchp, --improved"
                    usage
                    exit 1
                    ;;
            esac
            
            # Check for additional --model flag
            if [[ $# -ge 3 && $3 == --model=* ]]; then
                CUSTOM_MODEL="${3#*=}"
                if [ -n "$CUSTOM_MODEL" ]; then
                    DEFAULT_OLLAMA_MODEL="$CUSTOM_MODEL"
                    log "Using custom model: $CUSTOM_MODEL"
                else
                    log_error "--model flag requires a value (e.g., --model=qwen2.5:7b)"
                    exit 1
                fi
            fi
        else
            log_error "SMOL configuration required"
            echo "Please specify one of: --default, --mchp, --improved"
            usage
            exit 1
        fi
        
        log "Starting SMOL installation with $SMOL_CONFIG configuration..."
        
        # Install Ollama for SMOL agents (local model support)
        log "Setting up Ollama for local model support with model: $DEFAULT_OLLAMA_MODEL"
        if [ -f "$SCRIPT_DIR/src/install_scripts/install_ollama.sh" ]; then
            log "Found Ollama installer script"
            chmod +x "$SCRIPT_DIR/src/install_scripts/install_ollama.sh" || {
                log_error "Failed to make install_ollama.sh executable"
                exit 1
            }
            # Use configured model for SMOL agents
            export OLLAMA_MODELS="$DEFAULT_OLLAMA_MODEL"
            log "Executing Ollama installation script..."
            "$SCRIPT_DIR/src/install_scripts/install_ollama.sh" || {
                log_error "Ollama installation script failed"
                exit 1
            }
            log "Ollama installation completed successfully"
        else
            log_error "install_ollama.sh not found at $SCRIPT_DIR/src/install_scripts/"
            exit 1
        fi
        
        # Install SMOL agent
        if [ -f "$SCRIPT_DIR/src/SMOL/install_smol.sh" ]; then
            log "Found SMOL installer script"
            cd "$SCRIPT_DIR/src/SMOL" || {
                log_error "Failed to change to SMOL directory"
                exit 1
            }
            chmod +x install_smol.sh || {
                log_error "Failed to make install_smol.sh executable"
                exit 1
            }
            log "Executing SMOL installation script..."
            ./install_smol.sh --installpath="$SCRIPT_DIR" --config="$SMOL_CONFIG" || {
                log_error "SMOL installation script failed"
                exit 1
            }
            log "SMOL installation completed successfully"
            
            # Test SMOL installation  
            log "Testing SMOL installation..."
            if [ -f "$SCRIPT_DIR/src/install_scripts/test_agent.sh" ]; then
                chmod +x "$SCRIPT_DIR/src/install_scripts/test_agent.sh"
                "$SCRIPT_DIR/src/install_scripts/test_agent.sh" --agent=SMOL --path="$SCRIPT_DIR" || {
                    log_error "SMOL installation test failed"
                    exit 1
                }
            else
                log_error "test_agent.sh not found at $SCRIPT_DIR/src/install_scripts/"
                exit 1
            fi
        else
            log_error "install_smol.sh not found at $SCRIPT_DIR/src/SMOL/"
            exit 1
        fi
        ;;
    --help|-h)
        usage
        exit 0
        ;;
    *)
        echo "Unknown option: $1"
        usage
        exit 1
        ;;
esac