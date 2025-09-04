#!/bin/bash

#############################################
# MCHP Installation Script - Default Firefox Setup
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

log "MCHP will be installed at: $INSTALL_DIR"

cd "$INSTALL_DIR"

log "Creating base directory structure..."
mkdir -p "$INSTALL_DIR/deployed_sups/MCHP"
mkdir -p "$INSTALL_DIR/deployed_sups/MCHP/logs"
mkdir -p "$HOME/Downloads"

log "Updating system packages..."
sudo apt-get update -y

log "Installing system dependencies..."
sudo apt-get install -y \
    python3-pip \
    python3-venv \
    xvfb \
    xdg-utils \
    libxml2-dev \
    libxslt-dev \
    python3-tk \
    python3-dev \
    firefox

log "Installing Firefox Geckodriver..."
ARCH=$(uname -m)
if [[ "$ARCH" == "x86_64" ]]; then
    log "Downloading Geckodriver for Linux x64..."
    wget https://github.com/mozilla/geckodriver/releases/download/v0.34.0/geckodriver-v0.34.0-linux64.tar.gz
    tar -xvzf geckodriver-v0.34.0-linux64.tar.gz -C "$INSTALL_DIR/deployed_sups/MCHP/"
    rm -f geckodriver-v0.34.0-linux64.tar.gz
elif [[ "$ARCH" == "armv7l" ]]; then
    log "Downloading Geckodriver for ARMv7..."
    wget https://github.com/jamesmortensen/geckodriver-arm-binaries/releases/download/v0.34.0/geckodriver-v0.34.0-linux-armv7l.tar.gz
    tar -xvzf geckodriver-v0.34.0-linux-armv7l.tar.gz -C "$INSTALL_DIR/deployed_sups/MCHP/"
    rm -f geckodriver-v0.34.0-linux-armv7l.tar.gz
else
    warning "Architecture $ARCH not directly supported, attempting Linux x64 version..."
    wget https://github.com/mozilla/geckodriver/releases/download/v0.34.0/geckodriver-v0.34.0-linux64.tar.gz
    tar -xvzf geckodriver-v0.34.0-linux64.tar.gz -C "$INSTALL_DIR/deployed_sups/MCHP/"
    rm -f geckodriver-v0.34.0-linux64.tar.gz
fi

setup_default() {
    log "Setting up default MCHP deployment..."
    
    log "Creating Python virtual environment..."
    python3 -m venv "$INSTALL_DIR/deployed_sups/MCHP/venv"
    
    log "Activating virtual environment and installing Python packages..."
    source "$INSTALL_DIR/deployed_sups/MCHP/venv/bin/activate"
    
    python3 -m pip install --upgrade pip
    python3 -m pip install \
        selenium \
        beautifulsoup4 \
        webdriver-manager \
        lxml \
        pyautogui \
        lorem \
        certifi \
        chardet \
        colorama \
        configparser \
        crayons \
        idna \
        requests \
        urllib3
    
    deactivate
    
    if [ -d "$SCRIPT_DIR/DEFAULT/pyhuman" ]; then
        log "Copying DEFAULT pyhuman files..."
        cp -r "$SCRIPT_DIR/DEFAULT/pyhuman" "$INSTALL_DIR/deployed_sups/MCHP/"
    elif [ -d "$SCRIPT_DIR/pyhuman" ]; then
        log "Using base pyhuman files..."
        cp -r "$SCRIPT_DIR/pyhuman" "$INSTALL_DIR/deployed_sups/MCHP/"
    else
        error "pyhuman directory not found"
        return 1
    fi
    
    create_run_script
    create_systemd_service
    
    success "Default MCHP setup complete"
}

create_run_script() {
    local run_script="$INSTALL_DIR/deployed_sups/MCHP/run_mchp.sh"
    
    log "Creating run script..."
    
    cat > "$run_script" << EOF
#!/bin/bash
# MCHP Default Run Script

MCHP_DIR="$INSTALL_DIR/deployed_sups/MCHP"
LOG_FILE="\$MCHP_DIR/logs/mchp_\$(date '+%Y-%m-%d_%H-%M-%S').log"

cd "\$MCHP_DIR"
source "\$MCHP_DIR/venv/bin/activate"

echo "Starting MCHP at \$(date)" >> "\$LOG_FILE"
xvfb-run -a python3 "\$MCHP_DIR/pyhuman/human.py" >> "\$LOG_FILE" 2>&1

deactivate
EOF
    
    chmod +x "$run_script"
    log "Run script created at: $run_script"
}

create_systemd_service() {
    local service_file="/etc/systemd/system/mchp.service"
    
    log "Creating systemd service..."
    
    sudo tee "$service_file" > /dev/null << EOF
[Unit]
Description=MCHP Default Service
After=network.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$INSTALL_DIR/deployed_sups/MCHP
ExecStart=/bin/bash $INSTALL_DIR/deployed_sups/MCHP/run_mchp.sh
Restart=always
RestartSec=5s
StandardOutput=append:$INSTALL_DIR/deployed_sups/MCHP/logs/mchp_systemd.log
StandardError=append:$INSTALL_DIR/deployed_sups/MCHP/logs/mchp_systemd_error.log

[Install]
WantedBy=multi-user.target
EOF
    
    sudo systemctl daemon-reload
    sudo systemctl enable mchp.service
    
    log "Service mchp enabled"
    
    log "Starting mchp service..."
    sudo systemctl start mchp.service
    
    sleep 2
    if sudo systemctl is-active --quiet mchp.service; then
        success "MCHP service started successfully"
    else
        warning "MCHP service failed to start. Check logs with: sudo systemctl status mchp"
    fi
}

success() {
    echo -e "${GREEN}✓${NC} $1"
}

main() {
    log "Starting MCHP installation..."
    
    setup_default
    
    success "Installation complete!"
    echo ""
    echo "MCHP installed at: $INSTALL_DIR/deployed_sups/MCHP"
    echo ""
    if sudo systemctl is-active --quiet mchp.service; then
        echo "MCHP service is currently: ${GREEN}RUNNING${NC}"
    else
        echo "MCHP service is currently: ${RED}STOPPED${NC}"
    fi
    echo ""
    echo "Service commands:"
    echo "  Status: sudo systemctl status mchp"
    echo "  Stop: sudo systemctl stop mchp"
    echo "  Start: sudo systemctl start mchp"
    echo "  Restart: sudo systemctl restart mchp"
    echo ""
    echo "Manual run: $INSTALL_DIR/deployed_sups/MCHP/run_mchp.sh"
    echo "Logs: $INSTALL_DIR/deployed_sups/MCHP/logs/"
}

main