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

while [[ $# -gt 0 ]]; do
    case $1 in
        --installpath=*)
            INSTALL_DIR="${1#*=}"
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

setup_default() {
    log "Setting up default SMOL deployment..."
    
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
        requests
    
    deactivate
    
    if [ -d "$SCRIPT_DIR/default" ]; then
        log "Copying default SMOL agent files..."
        cp -r "$SCRIPT_DIR/default"/* "$INSTALL_DIR/deployed_sups/SMOL/"
    else
        error "default directory not found"
        return 1
    fi
    
    create_run_script
    create_systemd_service
    
    success "Default SMOL setup complete"
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
    
    setup_default
    
    success "Installation complete!"
    echo ""
    echo "SMOL installed at: $INSTALL_DIR/deployed_sups/SMOL"
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