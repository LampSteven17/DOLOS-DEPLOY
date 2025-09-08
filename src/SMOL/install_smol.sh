#!/bin/bash

#############################################
# SMOL Installation Script - Hugging Face Smol Agents Setup
# Self-contained deployment with virtual environment
#############################################

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

usage() {
    echo "Usage: $0 [options]"
    echo "Options:"
    echo "  --installpath=PATH    Base installation directory (default: \$HOME)"
    echo "  --config=CONFIG       SMOL configuration (default|mchp|improved)"
    echo "  --help                Display this help message"
}

log() {
    echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

INSTALL_DIR="$HOME"
USER_NAME="$USER"
SMOL_CONFIG="default"

while [[ $# -gt 0 ]]; do
    case $1 in
        --installpath=*)
            INSTALL_DIR="${1#*=}"
            ;;
        --config=*)
            SMOL_CONFIG="${1#*=}"
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

# Validate configuration
case $SMOL_CONFIG in
    default|mchp|improved)
        log "Using SMOL configuration: $SMOL_CONFIG"
        ;;
    *)
        error "Invalid SMOL configuration: $SMOL_CONFIG"
        error "Valid options are: default, mchp, improved"
        exit 1
        ;;
esac

log "SMOL will be installed at: $INSTALL_DIR"

cd "$INSTALL_DIR"

log "Creating base directory structure..."
mkdir -p "$INSTALL_DIR/deployed_sups/SMOL"
mkdir -p "$INSTALL_DIR/deployed_sups/SMOL/logs"

log "Updating system packages..."
sudo apt-get update -y

log "Installing system dependencies..."
sudo apt-get install -y \
    python3-pip \
    python3-venv \
    python3-dev \
    build-essential

setup_smol() {
    log "Setting up SMOL deployment with $SMOL_CONFIG configuration..."
    
    log "Creating Python virtual environment..."
    python3 -m venv "$INSTALL_DIR/deployed_sups/SMOL/venv"
    
    log "Activating virtual environment and installing Python packages..."
    source "$INSTALL_DIR/deployed_sups/SMOL/venv/bin/activate"
    
    python3 -m pip install --upgrade pip
    python3 -m pip install \
        smolagents \
        litellm \
        torch \
        transformers \
        datasets \
        numpy \
        pandas \
        requests \
        ddgs
    
    deactivate
    
    # Copy agent files based on configuration
    local config_dir=""
    case $SMOL_CONFIG in
        default)
            config_dir="default"
            ;;
        mchp)
            config_dir="mchp-like"
            ;;
        improved)
            config_dir="PHASE-improved"
            ;;
    esac
    
    if [ -d "$SCRIPT_DIR/$config_dir" ]; then
        log "Copying $SMOL_CONFIG SMOL agent files from $config_dir..."
        cp -r "$SCRIPT_DIR/$config_dir"/* "$INSTALL_DIR/deployed_sups/SMOL/"
        
        # Update agent.py with the correct model from installation
        if [ -f "$INSTALL_DIR/deployed_sups/SMOL/agent.py" ] && [ -n "$OLLAMA_MODELS" ]; then
            log "Configuring agent.py with model: $OLLAMA_MODELS"
            sed -i "s/os.getenv(\"LITELLM_MODEL\", \".*\")/os.getenv(\"LITELLM_MODEL\", \"ollama\/$OLLAMA_MODELS\")/g" "$INSTALL_DIR/deployed_sups/SMOL/agent.py"
        fi
    else
        error "$config_dir directory not found"
        return 1
    fi
    
    create_run_script
    create_systemd_service
    
    success "$SMOL_CONFIG SMOL setup complete"
}

create_run_script() {
    local run_script="$INSTALL_DIR/deployed_sups/SMOL/run_smol.sh"
    
    log "Creating run script..."
    
    cat > "$run_script" << EOF
#!/bin/bash
# SMOL Default Run Script

SMOL_DIR="$INSTALL_DIR/deployed_sups/SMOL"
LOG_FILE="\$SMOL_DIR/logs/smol_\$(date '+%Y-%m-%d_%H-%M-%S').log"

cd "\$SMOL_DIR"
source "\$SMOL_DIR/venv/bin/activate"

echo "Starting SMOL at \$(date)" >> "\$LOG_FILE"
python3 "\$SMOL_DIR/agent.py" >> "\$LOG_FILE" 2>&1

deactivate
EOF
    
    chmod +x "$run_script"
    log "Run script created at: $run_script"
}

create_systemd_service() {
    local service_file="/etc/systemd/system/smol.service"
    
    log "Creating systemd service..."
    
    sudo tee "$service_file" > /dev/null << EOF
[Unit]
Description=SMOL Agents Service
After=network.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$INSTALL_DIR/deployed_sups/SMOL
ExecStart=/bin/bash $INSTALL_DIR/deployed_sups/SMOL/run_smol.sh
Restart=always
RestartSec=5s
StandardOutput=append:$INSTALL_DIR/deployed_sups/SMOL/logs/smol_systemd.log
StandardError=append:$INSTALL_DIR/deployed_sups/SMOL/logs/smol_systemd_error.log

[Install]
WantedBy=multi-user.target
EOF
    
    sudo systemctl daemon-reload
    sudo systemctl enable smol.service
    
    log "Service smol enabled"
    
    log "Starting smol service..."
    sudo systemctl start smol.service
    
    sleep 2
    if sudo systemctl is-active --quiet smol.service; then
        success "SMOL service started successfully"
    else
        warning "SMOL service failed to start. Check logs with: sudo systemctl status smol"
    fi
}

success() {
    echo -e "${GREEN}${NC} $1"
}

main() {
    log "Starting SMOL installation..."
    
    setup_smol
    
    success "Installation complete!"
    echo ""
    echo "SMOL ($SMOL_CONFIG configuration) installed at: $INSTALL_DIR/deployed_sups/SMOL"
    echo ""
    if sudo systemctl is-active --quiet smol.service; then
        echo "SMOL service is currently: ${GREEN}RUNNING${NC}"
    else
        echo "SMOL service is currently: ${RED}STOPPED${NC}"
    fi
    echo ""
    echo "Service commands:"
    echo "  Status: sudo systemctl status smol"
    echo "  Stop: sudo systemctl stop smol"
    echo "  Start: sudo systemctl start smol"
    echo "  Restart: sudo systemctl restart smol"
    echo ""
    echo "Manual run: $INSTALL_DIR/deployed_sups/SMOL/run_smol.sh"
    echo "Logs: $INSTALL_DIR/deployed_sups/SMOL/logs/"
}

main