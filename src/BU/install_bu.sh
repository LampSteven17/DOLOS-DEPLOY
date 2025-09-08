#!/bin/bash

#############################################
# BU Installation Script - Browser Use Agents Setup
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
    echo "  --config=CONFIG       BU configuration (default|mchp|improved)"
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
BU_CONFIG="default"

while [[ $# -gt 0 ]]; do
    case $1 in
        --installpath=*)
            INSTALL_DIR="${1#*=}"
            ;;
        --config=*)
            BU_CONFIG="${1#*=}"
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
case $BU_CONFIG in
    default|mchp|improved)
        log "Using BU configuration: $BU_CONFIG"
        ;;
    *)
        error "Invalid BU configuration: $BU_CONFIG"
        error "Valid options are: default, mchp, improved"
        exit 1
        ;;
esac

log "BU will be installed at: $INSTALL_DIR"

cd "$INSTALL_DIR"

log "Creating base directory structure..."
mkdir -p "$INSTALL_DIR/deployed_sups/BU"
mkdir -p "$INSTALL_DIR/deployed_sups/BU/logs"

log "Updating system packages..."
sudo apt-get update -y

log "Installing system dependencies..."
sudo apt-get install -y \
    python3-pip \
    python3-venv \
    python3-dev \
    build-essential \
    xvfb \
    x11-utils \
    xfonts-100dpi \
    xfonts-75dpi \
    xfonts-scalable \
    xfonts-cyrillic \
    curl

# Install uv for browser-use dependency management
log "Installing uv package manager..."
curl -LsSf https://astral.sh/uv/install.sh | sh || true
export PATH="$HOME/.cargo/bin:$PATH"

setup_bu() {
    log "Setting up BU deployment with $BU_CONFIG configuration..."
    
    log "Creating Python virtual environment..."
    python3 -m venv "$INSTALL_DIR/deployed_sups/BU/venv"
    
    log "Activating virtual environment and installing Python packages..."
    source "$INSTALL_DIR/deployed_sups/BU/venv/bin/activate"
    
    python3 -m pip install --upgrade pip
    
    # Install uvx for browser-use
    python3 -m pip install uv
    
    python3 -m pip install \
        browser-use \
        selenium \
        playwright \
        pyautogui \
        beautifulsoup4 \
        requests \
        numpy \
        pandas \
        pillow \
        opencv-python
    
    # Install playwright browsers - Firefox only
    log "Installing Playwright Firefox browser..."
    python3 -m playwright install firefox
    python3 -m playwright install-deps firefox
    
    # Create symlink for uvx if uv is installed
    if [ -f "$HOME/.cargo/bin/uv" ]; then
        log "Creating uvx symlink for browser-use compatibility..."
        ln -sf "$HOME/.cargo/bin/uv" "$INSTALL_DIR/deployed_sups/BU/venv/bin/uvx" || true
    fi
    
    deactivate
    
    # Copy agent files based on configuration
    local config_dir=""
    case $BU_CONFIG in
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
        log "Copying $BU_CONFIG BU agent files from $config_dir..."
        cp -r "$SCRIPT_DIR/$config_dir"/* "$INSTALL_DIR/deployed_sups/BU/"
        
        # No model configuration needed for BU agents
    else
        error "$config_dir directory not found"
        return 1
    fi
    
    create_run_script
    
    success "$BU_CONFIG BU setup complete"
}

create_run_script() {
    local run_script="$INSTALL_DIR/deployed_sups/BU/run_bu.sh"
    
    log "Creating run script..."
    
    cat > "$run_script" << EOF
#!/bin/bash
# BU Default Run Script with Xvfb support

BU_DIR="$INSTALL_DIR/deployed_sups/BU"
LOG_FILE="\$BU_DIR/logs/bu_\$(date '+%Y-%m-%d_%H-%M-%S').log"

cd "\$BU_DIR"

# Start Xvfb on display :99 if not already running
if ! pgrep -x "Xvfb" > /dev/null; then
    echo "Starting Xvfb virtual display..." >> "\$LOG_FILE"
    Xvfb :99 -screen 0 1920x1080x24 > /dev/null 2>&1 &
    sleep 2
fi

# Set display for headless operation
export DISPLAY=:99

source "\$BU_DIR/venv/bin/activate"

# Add uv to PATH if it exists
export PATH="\$HOME/.cargo/bin:\$PATH"

# Set environment variables for BU agent
export OLLAMA_MODEL="\${OLLAMA_MODEL:-llama3.1:8b}"

# Tell browser-use to use firefox
export BROWSER_USE_BROWSER_TYPE="firefox"
export PLAYWRIGHT_BROWSERS_PATH="\$BU_DIR/venv"

echo "Starting BU at \$(date) with model: \$OLLAMA_MODEL on display \$DISPLAY" >> "\$LOG_FILE"
echo "Browser type: firefox" >> "\$LOG_FILE"
echo "PATH: \$PATH" >> "\$LOG_FILE"

python3 "\$BU_DIR/agent.py" >> "\$LOG_FILE" 2>&1

deactivate
EOF
    
    chmod +x "$run_script"
    log "Run script created at: $run_script"
}

create_systemd_service() {
    local service_file="/etc/systemd/system/bu.service"
    
    log "Creating systemd service..."
    
    sudo tee "$service_file" > /dev/null << EOF
[Unit]
Description=BU Agents Service
After=network.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$INSTALL_DIR/deployed_sups/BU
ExecStart=/bin/bash $INSTALL_DIR/deployed_sups/BU/run_bu.sh
Restart=always
RestartSec=5s
StandardOutput=append:$INSTALL_DIR/deployed_sups/BU/logs/bu_systemd.log
StandardError=append:$INSTALL_DIR/deployed_sups/BU/logs/bu_systemd_error.log

[Install]
WantedBy=multi-user.target
EOF
    
    sudo systemctl daemon-reload
    sudo systemctl enable bu.service
    
    log "Service bu enabled"
    
    log "Starting bu service..."
    sudo systemctl start bu.service
    
    sleep 2
    if sudo systemctl is-active --quiet bu.service; then
        success "BU service started successfully"
    else
        warning "BU service failed to start. Check logs with: sudo systemctl status bu"
    fi
}

success() {
    echo -e "${GREEN}${NC} $1"
}

main() {
    log "Starting BU installation..."
    
    setup_bu
    
    success "Installation complete!"
    echo ""
    echo "BU ($BU_CONFIG configuration) installed at: $INSTALL_DIR/deployed_sups/BU"
    echo ""
    echo "Next steps:"
    echo "  • Agent files are ready for testing and systemd service creation"
    echo "  • Manual run: $INSTALL_DIR/deployed_sups/BU/run_bu.sh"
    echo "  • Logs will be at: $INSTALL_DIR/deployed_sups/BU/logs/"
    echo ""
}

main