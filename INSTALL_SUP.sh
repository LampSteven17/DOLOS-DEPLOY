#!/bin/bash

# DOLOS-DEPLOY: SUP Installer (MCHP, SMOL & BU)

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
    echo "       $0 --bu --default [--model=MODEL]"
    echo "       $0 --bu --mchp [--model=MODEL]"
    echo "       $0 --bu --improved [--model=MODEL]"
    echo "       $0 --help"
    echo ""
    echo "Options:"
    echo "  --mchp                    Install MCHP (Human simulation)"
    echo "  --smol --default          Install SMOL agent with basic configuration"
    echo "  --smol --mchp             Install SMOL agent with MCHP-like behavior patterns"
    echo "  --smol --improved         Install SMOL agent with PHASE-improved configuration"
    echo "  --bu --default            Install BU (Browser Use) agent with basic configuration"
    echo "  --bu --mchp               Install BU (Browser Use) agent with MCHP-like behavior patterns"
    echo "  --bu --improved           Install BU (Browser Use) agent with PHASE-improved configuration"
    echo "  --model=MODEL             Override default model for SMOL and BU installations"
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
            
            # Create and start systemd service after testing passes
            log "Creating systemd service for SMOL..."
            cd "$SCRIPT_DIR/src/SMOL" || {
                log_error "Failed to change to SMOL directory"
                exit 1
            }
            
            # Create service file
            sudo tee /etc/systemd/system/smol.service > /dev/null << EOF
[Unit]
Description=SMOL Agents Service
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$SCRIPT_DIR/deployed_sups/SMOL
ExecStart=/bin/bash $SCRIPT_DIR/deployed_sups/SMOL/run_smol.sh
Restart=always
RestartSec=5s
StandardOutput=append:$SCRIPT_DIR/deployed_sups/SMOL/logs/smol_systemd.log
StandardError=append:$SCRIPT_DIR/deployed_sups/SMOL/logs/smol_systemd_error.log

[Install]
WantedBy=multi-user.target
EOF
            
            sudo systemctl daemon-reload
            sudo systemctl enable smol.service
            log "SMOL service enabled"
            
            log "Starting SMOL service..."
            sudo systemctl start smol.service
            
            sleep 2
            if sudo systemctl is-active --quiet smol.service; then
                log "SMOL service started successfully"
                
                echo ""
                echo -e "${GREEN}================================${NC}"
                echo -e "${GREEN}   SMOL INSTALLATION COMPLETE!${NC}"
                echo -e "${GREEN}================================${NC}"
                echo ""
                echo "SMOL ($SMOL_CONFIG configuration) is now running"
                echo ""
                echo "Installation Details:"
                echo "  • Agent Type: SMOL ($SMOL_CONFIG)"
                echo "  • Installation Path: $SCRIPT_DIR/deployed_sups/SMOL"
                echo "  • Service Status: RUNNING"
                echo ""
                echo "Service Management:"
                echo "  • Status: sudo systemctl status smol"
                echo "  • Stop: sudo systemctl stop smol"
                echo "  • Start: sudo systemctl start smol"
                echo "  • Restart: sudo systemctl restart smol"
                echo "  • Logs: sudo journalctl -u smol -f"
                echo ""
                echo "Manual Testing:"
                echo "  • Run directly: $SCRIPT_DIR/deployed_sups/SMOL/run_smol.sh"
                echo "  • View logs: tail -f $SCRIPT_DIR/deployed_sups/SMOL/logs/*.log"
                echo ""
            else
                log_error "SMOL service failed to start. Check logs with: sudo systemctl status smol"
                exit 1
            fi
        else
            log_error "install_smol.sh not found at $SCRIPT_DIR/src/SMOL/"
            exit 1
        fi
        ;;
    --bu)
        # Check if a configuration was specified as second argument
        BU_CONFIG=""
        if [[ $# -ge 2 ]]; then
            case $2 in
                --default)
                    BU_CONFIG="default"
                    ;;
                --mchp)
                    BU_CONFIG="mchp"
                    ;;
                --improved)
                    BU_CONFIG="improved"
                    ;;
                *)
                    log_error "Invalid BU configuration '$2'"
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
            log_error "BU configuration required"
            echo "Please specify one of: --default, --mchp, --improved"
            usage
            exit 1
        fi
        
        log "Starting BU installation with $BU_CONFIG configuration..."
        
        # Install Ollama for BU agents (browser_use can work with local models)
        log "Setting up Ollama for local model support with model: $DEFAULT_OLLAMA_MODEL"
        if [ -f "$SCRIPT_DIR/src/install_scripts/install_ollama.sh" ]; then
            log "Found Ollama installer script"
            chmod +x "$SCRIPT_DIR/src/install_scripts/install_ollama.sh" || {
                log_error "Failed to make install_ollama.sh executable"
                exit 1
            }
            # Use configured model for BU agents
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
        
        # Install BU agent
        if [ -f "$SCRIPT_DIR/src/BU/install_bu.sh" ]; then
            log "Found BU installer script"
            cd "$SCRIPT_DIR/src/BU" || {
                log_error "Failed to change to BU directory"
                exit 1
            }
            chmod +x install_bu.sh || {
                log_error "Failed to make install_bu.sh executable"
                exit 1
            }
            log "Executing BU installation script..."
            export OLLAMA_MODEL_DEFAULT="$DEFAULT_OLLAMA_MODEL"
            ./install_bu.sh --installpath="$SCRIPT_DIR" --config="$BU_CONFIG" || {
                log_error "BU installation script failed"
                exit 1
            }
            log "BU installation completed successfully"
            
            # Test BU installation  
            log "Testing BU installation..."
            if [ -f "$SCRIPT_DIR/src/install_scripts/test_agent.sh" ]; then
                chmod +x "$SCRIPT_DIR/src/install_scripts/test_agent.sh"
                "$SCRIPT_DIR/src/install_scripts/test_agent.sh" --agent=BU --path="$SCRIPT_DIR" || {
                    log_error "BU installation test failed"
                    exit 1
                }
            else
                log_error "test_agent.sh not found at $SCRIPT_DIR/src/install_scripts/"
                exit 1
            fi
            
            # Create and start systemd service after testing passes
            log "Creating systemd service for BU..."
            cd "$SCRIPT_DIR/src/BU" || {
                log_error "Failed to change to BU directory"
                exit 1
            }
            
            # Create service file
            sudo tee /etc/systemd/system/bu.service > /dev/null << EOF
[Unit]
Description=BU Agents Service
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$SCRIPT_DIR/deployed_sups/BU
Environment="DISPLAY=:99"
ExecStart=/bin/bash $SCRIPT_DIR/deployed_sups/BU/run_bu.sh
Restart=always
RestartSec=5s
StandardOutput=append:$SCRIPT_DIR/deployed_sups/BU/logs/bu_systemd.log
StandardError=append:$SCRIPT_DIR/deployed_sups/BU/logs/bu_systemd_error.log

[Install]
WantedBy=multi-user.target
EOF
            
            sudo systemctl daemon-reload
            sudo systemctl enable bu.service
            log "BU service enabled"
            
            log "Starting BU service..."
            sudo systemctl start bu.service
            
            sleep 2
            if sudo systemctl is-active --quiet bu.service; then
                log "BU service started successfully"
                
                echo ""
                echo -e "${GREEN}================================${NC}"
                echo -e "${GREEN}   BU INSTALLATION COMPLETE!${NC}"
                echo -e "${GREEN}================================${NC}"
                echo ""
                echo "BU ($BU_CONFIG configuration) is now running"
                echo ""
                echo "Installation Details:"
                echo "  • Agent Type: BU ($BU_CONFIG)"
                echo "  • Installation Path: $SCRIPT_DIR/deployed_sups/BU"
                echo "  • Service Status: RUNNING"
                echo ""
                echo "Service Management:"
                echo "  • Status: sudo systemctl status bu"
                echo "  • Stop: sudo systemctl stop bu"
                echo "  • Start: sudo systemctl start bu"
                echo "  • Restart: sudo systemctl restart bu"
                echo "  • Logs: sudo journalctl -u bu -f"
                echo ""
                echo "Manual Testing:"
                echo "  • Run directly: $SCRIPT_DIR/deployed_sups/BU/run_bu.sh"
                echo "  • View logs: tail -f $SCRIPT_DIR/deployed_sups/BU/logs/*.log"
                echo ""
            else
                log_error "BU service failed to start. Check logs with: sudo systemctl status bu"
                exit 1
            fi
        else
            log_error "install_bu.sh not found at $SCRIPT_DIR/src/BU/"
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