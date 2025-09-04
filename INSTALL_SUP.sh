#!/bin/bash

#############################################
# DOLOS-DEPLOY: Synthetic User Persona Installer
# Automated deployment for virtual synthetic users
# Version: 1.0.0
#############################################

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$SCRIPT_DIR/deployments"
CONFIG_DIR="$SCRIPT_DIR/configs"
LOGS_DIR="$SCRIPT_DIR/logs"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
LOG_FILE="$LOGS_DIR/install_${TIMESTAMP}.log"

print_banner() {
    echo -e "${PURPLE}"
    echo "============================================"
    echo "     DOLOS SYNTHETIC USER DEPLOYER         "
    echo "============================================"
    echo -e "${NC}"
}

log() {
    echo -e "[$(date +'%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

info() {
    echo -e "${BLUE}[INFO]${NC} $1" | tee -a "$LOG_FILE"
}

success() {
    echo -e "${GREEN}[✓]${NC} $1" | tee -a "$LOG_FILE"
}

warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1" | tee -a "$LOG_FILE"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1" | tee -a "$LOG_FILE"
}

setup_directories() {
    info "Setting up directory structure..."
    
    mkdir -p "$DEPLOY_DIR"
    mkdir -p "$CONFIG_DIR"
    mkdir -p "$LOGS_DIR"
    mkdir -p "$DEPLOY_DIR/caldera"
    mkdir -p "$DEPLOY_DIR/browser_use"
    mkdir -p "$DEPLOY_DIR/smol_agents"
    
    success "Directory structure created"
}

check_system() {
    info "Checking system information..."
    
    echo -e "${CYAN}System Information:${NC}"
    echo "OS: $(uname -s)"
    echo "Architecture: $(uname -m)"
    echo "Kernel: $(uname -r)"
    echo "Hostname: $(hostname)"
    
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        if [ -f /etc/os-release ]; then
            . /etc/os-release
            echo "Distribution: $NAME $VERSION"
        fi
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        echo "macOS Version: $(sw_vers -productVersion)"
    fi
    
    echo ""
}

check_requirements() {
    info "Checking system requirements..."
    
    local missing_deps=()
    
    declare -A tools=(
        ["python3"]="Python 3.x"
        ["pip3"]="pip for Python 3"
        ["git"]="Git version control"
        ["curl"]="cURL for downloads"
        ["wget"]="wget for downloads"
        ["docker"]="Docker (optional)"
    )
    
    for cmd in "${!tools[@]}"; do
        if command -v "$cmd" &> /dev/null; then
            version=$($cmd --version 2>&1 | head -n1)
            success "${tools[$cmd]} found: $version"
        else
            if [[ "$cmd" == "docker" ]]; then
                warning "${tools[$cmd]} not found (optional)"
            else
                error "${tools[$cmd]} not found"
                missing_deps+=("$cmd")
            fi
        fi
    done
    
    if [ ${#missing_deps[@]} -ne 0 ]; then
        error "Missing required dependencies: ${missing_deps[*]}"
        echo ""
        echo "Please install missing dependencies:"
        echo "  Ubuntu/Debian: sudo apt-get install ${missing_deps[*]}"
        echo "  CentOS/RHEL: sudo yum install ${missing_deps[*]}"
        echo "  macOS: brew install ${missing_deps[*]}"
        return 1
    fi
    
    success "All required dependencies are installed"
    return 0
}

show_menu() {
    echo ""
    echo -e "${CYAN}Select deployment option:${NC}"
    echo "1) Install ALL components"
    echo "2) MITRE Caldera Human Plugin"
    echo "3) Browser Use Agent"
    echo "4) Hugging Face Smol Agents"
    echo "5) Check system requirements only"
    echo "6) Verify existing installations"
    echo "0) Exit"
    echo ""
    read -p "Enter choice [0-6]: " choice
}

install_caldera() {
    info "Installing MITRE Caldera Human Plugin..."
    
    cd "$DEPLOY_DIR/caldera"
    
    if [ ! -d "caldera" ]; then
        info "Cloning Caldera repository..."
        git clone https://github.com/mitre/caldera.git . || {
            error "Failed to clone Caldera repository"
            return 1
        }
    fi
    
    info "Installing Python dependencies..."
    pip3 install -r requirements.txt --user || {
        warning "Some dependencies might have failed, continuing..."
    }
    
    info "Setting up Human plugin..."
    cd plugins
    if [ ! -d "human" ]; then
        git clone https://github.com/mitre/human.git || {
            error "Failed to clone Human plugin"
            return 1
        }
    fi
    
    if [ -f "human/requirements.txt" ]; then
        pip3 install -r human/requirements.txt --user || {
            warning "Some Human plugin dependencies might have failed"
        }
    fi
    
    success "Caldera Human Plugin installed"
    cd "$SCRIPT_DIR"
}

install_browser_use() {
    info "Installing Browser Use Agent..."
    
    cd "$DEPLOY_DIR/browser_use"
    
    info "Installing browser automation dependencies..."
    pip3 install playwright selenium pyautogui --user || {
        error "Failed to install browser dependencies"
        return 1
    }
    
    info "Installing Playwright browsers..."
    python3 -m playwright install || {
        warning "Playwright browsers installation might need sudo"
    }
    
    info "Creating browser agent configuration..."
    cat > browser_config.json << 'EOF'
{
    "agent_type": "browser_use",
    "browsers": ["chromium", "firefox"],
    "headless": false,
    "user_agents": [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    ],
    "viewport": {
        "width": 1920,
        "height": 1080
    }
}
EOF
    
    success "Browser Use Agent installed"
    cd "$SCRIPT_DIR"
}

install_smol_agents() {
    info "Installing Hugging Face Smol Agents..."
    
    cd "$DEPLOY_DIR/smol_agents"
    
    info "Installing ML dependencies..."
    pip3 install transformers torch accelerate datasets huggingface-hub --user || {
        error "Failed to install ML dependencies"
        return 1
    }
    
    info "Creating agent configuration..."
    cat > smol_config.json << 'EOF'
{
    "agent_type": "smol_agents",
    "models": [
        "microsoft/DialoGPT-small",
        "distilgpt2"
    ],
    "max_agents": 5,
    "conversation_logging": true
}
EOF
    
    success "Smol Agents installed"
    cd "$SCRIPT_DIR"
}

verify_installations() {
    info "Verifying installations..."
    echo ""
    
    if [ -d "$DEPLOY_DIR/caldera/plugins/human" ]; then
        success "Caldera Human Plugin: INSTALLED"
    else
        error "Caldera Human Plugin: NOT FOUND"
    fi
    
    if [ -f "$DEPLOY_DIR/browser_use/browser_config.json" ]; then
        success "Browser Use Agent: CONFIGURED"
    else
        error "Browser Use Agent: NOT CONFIGURED"
    fi
    
    if [ -f "$DEPLOY_DIR/smol_agents/smol_config.json" ]; then
        success "Smol Agents: CONFIGURED"
    else
        error "Smol Agents: NOT CONFIGURED"
    fi
    
    echo ""
    info "Checking Python packages..."
    
    python3 -c "import transformers" 2>/dev/null && success "transformers: installed" || warning "transformers: not installed"
    python3 -c "import playwright" 2>/dev/null && success "playwright: installed" || warning "playwright: not installed"
    python3 -c "import selenium" 2>/dev/null && success "selenium: installed" || warning "selenium: not installed"
}

main() {
    print_banner
    setup_directories
    check_system
    
    if ! check_requirements; then
        error "Requirements check failed. Exiting..."
        exit 1
    fi
    
    while true; do
        show_menu
        
        case $choice in
            1)
                info "Installing ALL components..."
                install_caldera
                install_browser_use
                install_smol_agents
                verify_installations
                ;;
            2)
                install_caldera
                ;;
            3)
                install_browser_use
                ;;
            4)
                install_smol_agents
                ;;
            5)
                check_requirements
                ;;
            6)
                verify_installations
                ;;
            0)
                info "Exiting installer..."
                exit 0
                ;;
            *)
                error "Invalid option"
                ;;
        esac
    done
}

if [ "$EUID" -eq 0 ]; then
    warning "Running as root is not recommended. Consider running as a regular user."
    read -p "Continue anyway? (y/n): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

main "$@"