#!/bin/bash

#############################################
# Ollama Installation Script
# Installs and configures Ollama for local model support
# Used by various components requiring local LLM capabilities
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

log "Starting Ollama installation for local model support..."

# Check if Ollama is already installed
if command -v ollama &> /dev/null; then
    log "Ollama is already installed"
    ollama --version
else
    log "Installing Ollama..."
    curl -fsSL https://ollama.com/install.sh | sh
    success "Ollama installed successfully"
fi

# Start and enable Ollama service
log "Configuring Ollama service..."
if systemctl is-enabled ollama &> /dev/null; then
    log "Ollama service already enabled"
else
    sudo systemctl enable ollama
    success "Ollama service enabled"
fi

if systemctl is-active --quiet ollama; then
    log "Ollama service is already running"
else
    log "Starting Ollama service..."
    sudo systemctl start ollama
    success "Ollama service started"
fi

# Wait for Ollama to be ready
log "Waiting for Ollama to be ready..."
sleep 5

# Check if we can connect to Ollama
max_attempts=10
attempt=1
while [ $attempt -le $max_attempts ]; do
    if ollama list &> /dev/null; then
        success "Ollama is ready"
        break
    else
        log "Waiting for Ollama service... (attempt $attempt/$max_attempts)"
        sleep 2
        attempt=$((attempt + 1))
    fi
done

if [ $attempt -gt $max_attempts ]; then
    error "Ollama service failed to start properly"
    exit 1
fi

# Pull models if specified
MODELS_TO_INSTALL="${OLLAMA_MODELS:-${OLLAMA_DEFAULT_MODEL:-llama2}}"

if [ -n "$MODELS_TO_INSTALL" ]; then
    # Convert comma-separated list to array
    IFS=',' read -ra MODEL_ARRAY <<< "$MODELS_TO_INSTALL"
    
    for model in "${MODEL_ARRAY[@]}"; do
        # Trim whitespace
        model=$(echo "$model" | xargs)
        
        if [ -n "$model" ]; then
            log "Checking for model ($model)..."
            
            # Check if model is already installed (handle both with and without :latest tag)
            if ollama list | grep -q "^$model" || ollama list | grep -q "^$model:latest"; then
                log "$model model already available"
            else
                log "Pulling $model model (this may take several minutes)..."
                ollama pull "$model"
                success "$model model pulled successfully"
            fi
        fi
    done
else
    log "No models specified for installation"
fi

# Show available models
log "Available Ollama models:"
ollama list

success "Ollama installation and configuration complete!"
echo ""
echo "Ollama is now ready for local model usage with the following:"
echo "  • Service: ollama.service (enabled and running)"
if [ -n "$MODELS_TO_INSTALL" ]; then
    echo "  • Installed models: $MODELS_TO_INSTALL"
fi
echo "  • API endpoint: http://localhost:11434"
echo ""
echo "You can manage Ollama with:"
echo "  • Check status: sudo systemctl status ollama"
echo "  • View logs: sudo journalctl -u ollama -f"
echo "  • List models: ollama list"
echo "  • Pull new models: ollama pull <model-name>"
echo ""
echo "Common models you can install:"
echo "  • ollama pull llama2         # Meta Llama 2 (7B)"
echo "  • ollama pull mistral        # Mistral 7B"
echo "  • ollama pull codellama      # Code Llama"
echo "  • ollama pull llama2:13b     # Larger Llama 2 model"
echo "  • ollama pull phi            # Microsoft Phi-2"