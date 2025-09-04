#!/bin/bash

# DOLOS-DEPLOY: MCHP Installer

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$SCRIPT_DIR/deployments"

usage() {
    echo "Usage: $0 --mchp"
    echo "       $0 --help"
}

if [[ $# -eq 0 ]]; then
    usage
    exit 1
fi

case $1 in
    --mchp)
        echo "Installing MCHP..."
        mkdir -p "$DEPLOY_DIR/mchp"
        
        if [ -f "$SCRIPT_DIR/src/MCHP/install_mchp.sh" ]; then
            cd "$SCRIPT_DIR/src/MCHP"
            chmod +x install_mchp.sh
            ./install_mchp.sh --installpath="$DEPLOY_DIR/mchp"
        else
            echo "Error: install_mchp.sh not found at $SCRIPT_DIR/src/MCHP/"
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