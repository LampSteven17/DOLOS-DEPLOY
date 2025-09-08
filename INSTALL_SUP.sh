#!/bin/bash

# DOLOS-DEPLOY: SUP Installer (MCHP & SMOL)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    echo "Usage: $0 --mchp"
    echo "       $0 --smol --default"
    echo "       $0 --smol --mchp"
    echo "       $0 --smol --improved"
    echo "       $0 --help"
    echo ""
    echo "Options:"
    echo "  --mchp                    Install MCHP (Human simulation)"
    echo "  --smol --default          Install SMOL agent with basic configuration"
    echo "  --smol --mchp             Install SMOL agent with MCHP-like behavior patterns"
    echo "  --smol --improved         Install SMOL agent with PHASE-improved configuration"
    echo "  --help                    Display this help message"
}

if [[ $# -eq 0 ]]; then
    usage
    exit 1
fi

case $1 in
    --mchp)
        echo "Installing MCHP..."
        
        if [ -f "$SCRIPT_DIR/src/MCHP/install_mchp.sh" ]; then
            cd "$SCRIPT_DIR/src/MCHP"
            chmod +x install_mchp.sh
            ./install_mchp.sh --installpath="$SCRIPT_DIR"
        else
            echo "Error: install_mchp.sh not found at $SCRIPT_DIR/src/MCHP/"
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
                    echo "Error: Invalid SMOL configuration '$2'"
                    echo "Valid options are: --default, --mchp, --improved"
                    usage
                    exit 1
                    ;;
            esac
        else
            echo "Error: SMOL configuration required"
            echo "Please specify one of: --default, --mchp, --improved"
            usage
            exit 1
        fi
        
        echo "Installing SMOL with $SMOL_CONFIG configuration..."
        
        if [ -f "$SCRIPT_DIR/src/SMOL/install_smol.sh" ]; then
            cd "$SCRIPT_DIR/src/SMOL"
            chmod +x install_smol.sh
            ./install_smol.sh --installpath="$SCRIPT_DIR" --config="$SMOL_CONFIG"
        else
            echo "Error: install_smol.sh not found at $SCRIPT_DIR/src/SMOL/"
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