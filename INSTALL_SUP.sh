#!/bin/bash

# DOLOS-DEPLOY: SUP Installer (MCHP & SMOL)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    echo "Usage: $0 --mchp"
    echo "       $0 --smol"
    echo "       $0 --help"
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
        echo "Installing SMOL..."
        
        if [ -f "$SCRIPT_DIR/src/SMOL/install_smol.sh" ]; then
            cd "$SCRIPT_DIR/src/SMOL"
            chmod +x install_smol.sh
            ./install_smol.sh --installpath="$SCRIPT_DIR"
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