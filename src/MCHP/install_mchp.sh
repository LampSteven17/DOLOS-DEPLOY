#!/bin/bash

#############################################
# MCHP Installation Script - Default Firefox Setup
# Self-contained deployment with virtual environment
#############################################

set -e

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
mkdir -p "$INSTALL_DIR/MCHP"
mkdir -p "$INSTALL_DIR/MCHP/LOGS"
mkdir -p "$INSTALL_DIR/MCHP/Downloads"

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
    tar -xvzf geckodriver-v0.34.0-linux64.tar.gz -C "$INSTALL_DIR/MCHP/"
    rm -f geckodriver-v0.34.0-linux64.tar.gz
elif [[ "$ARCH" == "armv7l" ]]; then
    log "Downloading Geckodriver for ARMv7..."
    wget https://github.com/jamesmortensen/geckodriver-arm-binaries/releases/download/v0.34.0/geckodriver-v0.34.0-linux-armv7l.tar.gz
    tar -xvzf geckodriver-v0.34.0-linux-armv7l.tar.gz -C "$INSTALL_DIR/MCHP/"
    rm -f geckodriver-v0.34.0-linux-armv7l.tar.gz
else
    warning "Architecture $ARCH not directly supported, attempting Linux x64 version..."
    wget https://github.com/mozilla/geckodriver/releases/download/v0.34.0/geckodriver-v0.34.0-linux64.tar.gz
    tar -xvzf geckodriver-v0.34.0-linux64.tar.gz -C "$INSTALL_DIR/MCHP/"
    rm -f geckodriver-v0.34.0-linux64.tar.gz
fi

setup_default() {
    log "Setting up default MCHP deployment..."
    
    log "Creating Python virtual environment..."
    python3 -m venv "$INSTALL_DIR/MCHP/venv"
    
    log "Activating virtual environment and installing Python packages..."
    source "$INSTALL_DIR/MCHP/venv/bin/activate"
    
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
    
    if [ -d "$INSTALL_DIR/DOLOS-DEPLOY/src/MCHP/DEFAULT/pyhuman" ]; then
        log "Copying DEFAULT pyhuman files..."
        cp -r "$INSTALL_DIR/DOLOS-DEPLOY/src/MCHP/DEFAULT/pyhuman" "$INSTALL_DIR/MCHP/"
    elif [ -d "$INSTALL_DIR/DOLOS-DEPLOY/src/MCHP/pyhuman" ]; then
        log "Using base pyhuman files..."
        cp -r "$INSTALL_DIR/DOLOS-DEPLOY/src/MCHP/pyhuman" "$INSTALL_DIR/MCHP/"
    else
        error "pyhuman directory not found"
        return 1
    fi
    
    create_run_script
    create_systemd_service
    
    success "Default MCHP setup complete"
}

create_run_script() {
    local run_script="$INSTALL_DIR/MCHP/run_mchp.sh"
    
    log "Creating run script..."
    
    cat > "$run_script" << EOF
#!/bin/bash
# MCHP Default Run Script

MCHP_DIR="$INSTALL_DIR/MCHP"
LOG_FILE="\$MCHP_DIR/LOGS/mchp_\$(date '+%Y-%m-%d_%H-%M-%S').log"

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
WorkingDirectory=$INSTALL_DIR/MCHP
ExecStart=/bin/bash $INSTALL_DIR/MCHP/run_mchp.sh
Restart=on-failure
RestartSec=5s
StandardOutput=append:$INSTALL_DIR/MCHP/LOGS/mchp_systemd.log
StandardError=append:$INSTALL_DIR/MCHP/LOGS/mchp_systemd_error.log

[Install]
WantedBy=multi-user.target
EOF
    
    sudo systemctl daemon-reload
    sudo systemctl enable mchp.service
    
    log "Service mchp enabled"
}

success() {
    echo -e "${GREEN}✓${NC} $1"
}

main() {
    log "Starting MCHP installation..."
    
    setup_default
    
    success "Installation complete!"
    echo ""
    echo "MCHP installed at: $INSTALL_DIR/MCHP"
    echo ""
    echo "To run MCHP:"
    echo "  Manual: $INSTALL_DIR/MCHP/run_mchp.sh"
    echo "  Systemd: sudo systemctl start mchp"
    echo "  Status: sudo systemctl status mchp"
    echo "  Stop: sudo systemctl stop mchp"
    echo ""
    echo "Logs are stored in: $INSTALL_DIR/MCHP/LOGS/"
}

main