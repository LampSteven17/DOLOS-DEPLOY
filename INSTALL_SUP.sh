#!/bin/bash

# RUSE: Unified SUP Installer
# Architecture: Brain + Model + Calibration
#
# Naming Scheme:
#   [Brain][Version].[Model]
#   Brain:    M = MCHP, B = BrowserUse, S = SmolAgents
#   Version:  0 = baseline (B/S only, no timing)
#             1 = baseline (MCHP only, no timing)
#             2 = calibrated to summer24
#             3 = calibrated to fall24
#             4 = calibrated to spring25
#   Models:   llama (llama3.1:8b), gemma (gemma3:1b)
#
# Usage:
#   ./INSTALL_SUP.sh --M1                    # Config key shorthand
#   ./INSTALL_SUP.sh --B3.gemma --runner     # Run directly without install
#   ./INSTALL_SUP.sh --brain browseruse --model llama --calibration fall24

set -e
set -u
set -o pipefail

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Error handling
error_handler() {
    local exit_code=$?
    local line_number=$1
    echo ""
    echo -e "${RED}================================${NC}"
    echo -e "${RED}    INSTALLATION FAILED!${NC}"
    echo -e "${RED}================================${NC}"
    echo -e "${RED}Error at line ${line_number}, exit code ${exit_code}${NC}"
    exit $exit_code
}
trap 'error_handler ${LINENO}' ERR

log() { echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1"; }
log_error() { echo -e "${RED}[$(date +'%Y-%m-%d %H:%M:%S')] ERROR:${NC} $1" >&2; }
log_info() { echo -e "${BLUE}[$(date +'%Y-%m-%d %H:%M:%S')] INFO:${NC} $1"; }

usage() {
    cat << 'EOF'
RUSE Unified Installer

Usage: ./INSTALL_SUP.sh <CONFIG> [OPTIONS]

=== Configuration Keys ===

  Canonical Workflow Controls:
    --scripted-cpu       Scripted Brain control (CPU)
    --mchp-cpu           MCHP Brain control (CPU)
    --browseruse-gpu     BrowserUse control (GPU, gemma4:26b)
    --smolagents-gpu     SmolAgents control (GPU, gemma4:26b)

  Control Series:
    --C0                Bare Ubuntu VM (no software - pure control)

  M Series - MCHP Brain (no LLM, no GPU):
    --M0                Upstream MITRE pyhuman (control - DO NOT MODIFY)
    --M1                MCHP baseline (no timing)
    --M2                MCHP + summer24 calibrated timing
    --M3                MCHP + fall24 calibrated timing
    --M4                MCHP + spring25 calibrated timing

  B Series - BrowserUse Brain (GPU):
    --B0.llama          BrowserUse + llama (no timing)
    --B0.gemma          BrowserUse + gemma (no timing)
    --B2.llama          BrowserUse + llama + summer24 timing
    --B2.gemma          BrowserUse + gemma + summer24 timing
    --B3.llama          BrowserUse + llama + fall24 timing
    --B3.gemma          BrowserUse + gemma + fall24 timing
    --B4.llama          BrowserUse + llama + spring25 timing
    --B4.gemma          BrowserUse + gemma + spring25 timing

  S Series - SmolAgents Brain (GPU):
    --S0.llama          SmolAgents + llama (no timing)
    --S0.gemma          SmolAgents + gemma (no timing)
    --S2.llama          SmolAgents + llama + summer24 timing
    --S2.gemma          SmolAgents + gemma + summer24 timing
    --S3.llama          SmolAgents + llama + fall24 timing
    --S3.gemma          SmolAgents + gemma + fall24 timing
    --S4.llama          SmolAgents + llama + spring25 timing
    --S4.gemma          SmolAgents + gemma + spring25 timing

  CPU Baselines (no GPU — Ollama on CPU):
    --B0C.llama         BrowserUse + llama (CPU only)
    --B0C.gemma         BrowserUse + gemma (CPU only)
    --S0C.llama         SmolAgents + llama (CPU only)
    --S0C.gemma         SmolAgents + gemma (CPU only)

  CPU Calibrated (no GPU — summer24 timing):
    --B2C.llama         BrowserUse + llama + summer24 (CPU only)
    --B2C.gemma         BrowserUse + gemma + summer24 (CPU only)
    --S2C.llama         SmolAgents + llama + summer24 (CPU only)
    --S2C.gemma         SmolAgents + gemma + summer24 (CPU only)

  RTX Baselines (RTX 2080 Ti):
    --B0R.llama         BrowserUse + llama (RTX)
    --B0R.gemma         BrowserUse + gemma (RTX)
    --S0R.llama         SmolAgents + llama (RTX)
    --S0R.gemma         SmolAgents + gemma (RTX)

  RTX Iteration 2 (RTX 2080 Ti):
    --B2R.llama         BrowserUse + llama iteration 2 (RTX)
    --B2R.gemma         BrowserUse + gemma iteration 2 (RTX)

=== Deprecated (exp-2 compat) ===

  Old MCHP+LLM keys (M1a.llama, M2a.llama, etc.) map to M1/M2.
  Old variant keys (B1a.llama, S2c.deepseek, etc.) map to new scheme.

=== Long-Form Options ===

  --brain <TYPE>        Brain type: mchp, smolagents, browseruse
  --model <MODEL>       Model: llama, gemma
  --calibration <PROF>  Calibration profile: summer24, fall24, spring25

=== Execution Options ===

  --runner              Run directly without systemd installation
  --task "TASK"         Task for LLM agents (with --runner)
  --stage <1|2>         Staged install for Ansible (1=pre-reboot, 2=post-reboot)
  --list                List all available configurations
  --help                Show this help

=== Examples ===

  ./INSTALL_SUP.sh --M1                           # Install pure MCHP
  ./INSTALL_SUP.sh --M3 --runner                   # Run MCHP + fall24 timing
  ./INSTALL_SUP.sh --B2.gemma                      # Install BrowserUse + gemma + summer24
  ./INSTALL_SUP.sh --brain browseruse --model llama --calibration fall24
  ./INSTALL_SUP.sh --S4.llama --runner             # SmolAgents + spring25 timing

EOF
}

# ============================================================================
# Configuration Parsing
# ============================================================================

# Default values
CONFIG_KEY=""
BRAIN=""
CONTENT=""
MODEL=""
CALIBRATION="none"
RUNNER=false
TASK=""
STAGE=0  # 0=full install, 1=pre-reboot only, 2=post-reboot only

# Pre-defined configurations: brain:content:model:calibration
declare -A CONFIGS
CONFIGS=(
    # Canonical phase-workflow-plan-v1 controls
    ["scripted-cpu"]="scripted:none:none:none"
    ["mchp-cpu"]="mchp:none:none:none"
    ["browseruse-gpu"]="browseruse:none:gemma:none"
    ["smolagents-gpu"]="smolagents:none:gemma:none"

    # Control
    ["C0"]="mchp:none:none:none"

    # M Series - MCHP brain (no LLM)
    ["M0"]="upstream:none:none:none"
    ["M1"]="mchp:none:none:none"
    ["M2"]="mchp:none:none:summer24"
    ["M3"]="mchp:none:none:fall24"
    ["M4"]="mchp:none:none:spring25"

    # B Series - BrowserUse brain (GPU)
    ["B0.llama"]="browseruse:none:llama:none"
    ["B0.gemma"]="browseruse:none:gemma:none"
    ["B2.llama"]="browseruse:none:llama:summer24"
    ["B2.gemma"]="browseruse:none:gemma:summer24"
    ["B3.llama"]="browseruse:none:llama:fall24"
    ["B3.gemma"]="browseruse:none:gemma:fall24"
    ["B4.llama"]="browseruse:none:llama:spring25"
    ["B4.gemma"]="browseruse:none:gemma:spring25"

    # S Series - SmolAgents brain (GPU)
    ["S0.llama"]="smolagents:none:llama:none"
    ["S0.gemma"]="smolagents:none:gemma:none"
    ["S2.llama"]="smolagents:none:llama:summer24"
    ["S2.gemma"]="smolagents:none:gemma:summer24"
    ["S3.llama"]="smolagents:none:llama:fall24"
    ["S3.gemma"]="smolagents:none:gemma:fall24"
    ["S4.llama"]="smolagents:none:llama:spring25"
    ["S4.gemma"]="smolagents:none:gemma:spring25"

    # CPU baselines (no GPU) — gemma uses gemmac (gemma4:e2b, edge-optimized)
    ["B0C.llama"]="browseruse:none:llama:none"
    ["B0C.gemma"]="browseruse:none:gemmac:none"
    ["S0C.llama"]="smolagents:none:llama:none"
    ["S0C.gemma"]="smolagents:none:gemmac:none"

    # CPU calibrated (no GPU, summer24 timing)
    ["B2C.llama"]="browseruse:none:llama:summer24"
    ["B2C.gemma"]="browseruse:none:gemmac:summer24"
    ["S2C.llama"]="smolagents:none:llama:summer24"
    ["S2C.gemma"]="smolagents:none:gemmac:summer24"

    # RTX baselines (RTX 2080 Ti, 11 GB VRAM — gemma4 edge 4B variant)
    # B0R/S0R.gemma re-keyed 2026-05-20: "gemma" alias→gemma4:26b doesn't
    # fit 11GB; "gemmar" alias→gemma4:e4b is the correct R-tier mapping.
    ["B0R.llama"]="browseruse:none:llama:none"
    ["B0R.gemma"]="browseruse:none:gemmar:none"
    ["S0R.llama"]="smolagents:none:llama:none"
    ["S0R.gemma"]="smolagents:none:gemmar:none"

    # RTX iteration 2 (RTX 2080 Ti) — feedback deploys use B2R.gemma/S2R.gemma
    ["B2R.llama"]="browseruse:none:llama:none"
    ["B2R.gemma"]="browseruse:none:gemmar:none"
    ["S2R.llama"]="smolagents:none:llama:none"
    ["S2R.gemma"]="smolagents:none:gemmar:none"

    # === Deprecated aliases ===
    # Old B1/S1 baseline keys -> B0/S0
    ["B1.llama"]="browseruse:none:llama:none"
    ["B1.gemma"]="browseruse:none:gemma:none"
    ["S1.llama"]="smolagents:none:llama:none"
    ["S1.gemma"]="smolagents:none:gemma:none"

    # MCHP + LLM keys -> plain MCHP (no LLM)
    ["M1a.llama"]="mchp:llm:llama:none"
    ["M1b.gemma"]="mchp:llm:gemma:none"
    ["M1c.deepseek"]="mchp:llm:deepseek:none"
    ["M2a.llama"]="mchp:llm:llama:summer24"
    ["M2b.gemma"]="mchp:llm:gemma:summer24"
    ["M2c.deepseek"]="mchp:llm:deepseek:summer24"

    # BrowserUse exp-2 keys (with variant letter)
    ["B1a.llama"]="browseruse:none:llama:none"
    ["B1b.gemma"]="browseruse:none:gemma:none"
    ["B1c.deepseek"]="browseruse:none:deepseek:none"
    ["B2a.llama"]="browseruse:none:llama:summer24"
    ["B2b.gemma"]="browseruse:none:gemma:summer24"
    ["B2c.deepseek"]="browseruse:none:deepseek:summer24"

    # SmolAgents exp-2 keys (with variant letter)
    ["S1a.llama"]="smolagents:none:llama:none"
    ["S1b.gemma"]="smolagents:none:gemma:none"
    ["S1c.deepseek"]="smolagents:none:deepseek:none"
    ["S2a.llama"]="smolagents:none:llama:summer24"
    ["S2b.gemma"]="smolagents:none:gemma:summer24"
    ["S2c.deepseek"]="smolagents:none:deepseek:summer24"

    # CPU variants (exp-2)
    ["MC1a.llama"]="mchp:llm:llama:none"
    ["MC1b.gemma"]="mchp:llm:gemma:none"
    ["MC1c.deepseek"]="mchp:llm:deepseek:none"
    ["MC1d.lfm"]="mchp:llm:lfm:none"
    ["MC1e.ministral"]="mchp:llm:ministral:none"
    ["MC1f.qwen"]="mchp:llm:qwen:none"
    ["MC2a.llama"]="mchp:llm:llama:summer24"
    ["MC2b.gemma"]="mchp:llm:gemma:summer24"
    ["MC2c.deepseek"]="mchp:llm:deepseek:summer24"
    ["MC2d.lfm"]="mchp:llm:lfm:summer24"
    ["MC2e.ministral"]="mchp:llm:ministral:summer24"
    ["MC2f.qwen"]="mchp:llm:qwen:summer24"
    ["BC1a.llama"]="browseruse:none:llama:none"
    ["BC1b.gemma"]="browseruse:none:gemma:none"
    ["BC1c.deepseek"]="browseruse:none:deepseek:none"
    ["BC1d.lfm"]="browseruse:none:lfm:none"
    ["BC1e.ministral"]="browseruse:none:ministral:none"
    ["BC1f.qwen"]="browseruse:none:qwen:none"
    ["SC1a.llama"]="smolagents:none:llama:none"
    ["SC1b.gemma"]="smolagents:none:gemma:none"
    ["SC1c.deepseek"]="smolagents:none:deepseek:none"
    ["SC1d.lfm"]="smolagents:none:lfm:none"
    ["SC1e.ministral"]="smolagents:none:ministral:none"
    ["SC1f.qwen"]="smolagents:none:qwen:none"
)

# Model name mappings
declare -A MODEL_NAMES
MODEL_NAMES=(
    ["none"]=""
    # GPU-optimized models
    ["llama"]="llama3.1:8b"
    ["gemma"]="gemma4:26b"        # V100 32GB sweet spot (MoE: 25.2B params, 3.8B active)
    ["gemmar"]="gemma4:e4b"       # RTX 2080 Ti 11GB — gemma4 edge 4B variant (~3GB int4)
    ["gemmac"]="gemma4:e2b"       # CPU edge-optimized (2.3B effective params)
    # Legacy (exp-2 compat)
    ["deepseek"]="deepseek-r1:8b"
    ["lfm"]="lfm2.5-thinking:latest"
    ["ministral"]="ministral-3:3b"
    ["qwen"]="qwen2.5:3b"
)

list_configs() {
    echo "Available configurations:"
    echo ""
    echo "Canonical Workflow Controls:"
    for key in scripted-cpu mchp-cpu browseruse-gpu smolagents-gpu; do
        IFS=':' read -r brain content model calibration <<< "${CONFIGS[$key]}"
        printf "  %-20s brain=%-12s model=%s\n" \
            "--$key" "$brain" "$model"
    done
    echo ""
    echo "Control:"
    printf "  %-16s Bare Ubuntu VM (no software)\n" "--C0"
    echo ""
    echo "M Series - MCHP Brain (no LLM, no GPU):"
    for key in M0 M1 M2 M3 M4; do
        IFS=':' read -r brain content model calibration <<< "${CONFIGS[$key]}"
        printf "  %-16s brain=%-10s calibration=%s\n" \
            "--$key" "$brain" "$calibration"
    done
    echo ""
    echo "B Series - BrowserUse Brain (GPU):"
    for key in B0.llama B0.gemma B2.llama B2.gemma B3.llama B3.gemma B4.llama B4.gemma; do
        IFS=':' read -r brain content model calibration <<< "${CONFIGS[$key]}"
        printf "  %-16s brain=%-12s model=%-8s calibration=%s\n" \
            "--$key" "$brain" "$model" "$calibration"
    done
    echo ""
    echo "S Series - SmolAgents Brain (GPU):"
    for key in S0.llama S0.gemma S2.llama S2.gemma S3.llama S3.gemma S4.llama S4.gemma; do
        IFS=':' read -r brain content model calibration <<< "${CONFIGS[$key]}"
        printf "  %-16s brain=%-12s model=%-8s calibration=%s\n" \
            "--$key" "$brain" "$model" "$calibration"
    done
    echo ""
    echo "CPU Baselines (no GPU):"
    for key in B0C.llama B0C.gemma S0C.llama S0C.gemma; do
        IFS=':' read -r brain content model calibration <<< "${CONFIGS[$key]}"
        printf "  %-16s brain=%-12s model=%-8s (CPU only)\n" \
            "--$key" "$brain" "$model"
    done
    echo ""
    echo "CPU Calibrated (no GPU, summer24):"
    for key in B2C.llama B2C.gemma S2C.llama S2C.gemma; do
        IFS=':' read -r brain content model calibration <<< "${CONFIGS[$key]}"
        printf "  %-16s brain=%-12s model=%-8s calibration=%s (CPU only)\n" \
            "--$key" "$brain" "$model" "$calibration"
    done
    echo ""
    echo "RTX Baselines (RTX 2080 Ti):"
    for key in B0R.llama B0R.gemma S0R.llama S0R.gemma; do
        IFS=':' read -r brain content model calibration <<< "${CONFIGS[$key]}"
        printf "  %-16s brain=%-12s model=%-8s (RTX)\n" \
            "--$key" "$brain" "$model"
    done
    echo ""
    echo "RTX Iteration 2 (RTX 2080 Ti):"
    for key in B2R.llama B2R.gemma S2R.llama S2R.gemma; do
        IFS=':' read -r brain content model calibration <<< "${CONFIGS[$key]}"
        printf "  %-16s brain=%-12s model=%-8s (RTX)\n" \
            "--$key" "$brain" "$model"
    done
    echo ""
    echo "Deprecated exp-2 keys (including old B1/S1) are still accepted."
}

parse_config_key() {
    local key="$1"
    if [[ -v "CONFIGS[$key]" ]]; then
        IFS=':' read -r BRAIN CONTENT MODEL CALIBRATION <<< "${CONFIGS[$key]}"
        CONFIG_KEY="$key"
        return 0
    fi
    return 1
}

is_phase_workflow_config() {
    case "$1" in
        scripted-cpu|mchp-cpu|browseruse-gpu|smolagents-gpu) return 0 ;;
        *) return 1 ;;
    esac
}

apply_phase_workflow_gpu_tier() {
    is_phase_workflow_config "$CONFIG_KEY" || return 0

    local gpu_tier="${RUSE_WORKFLOW_GPU_TIER:-v100}"
    case "$gpu_tier" in
        v100)
            ;;
        rtx)
            if [[ "$CONFIG_KEY" == "browseruse-gpu" || \
                  "$CONFIG_KEY" == "smolagents-gpu" ]]; then
                MODEL="gemmar"
            fi
            ;;
        *)
            log_error "Invalid canonical workflow GPU tier: $gpu_tier (expected v100 or rtx)"
            return 1
            ;;
    esac
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            # Canonical phase-workflow-plan-v1 controls
            --scripted-cpu|--mchp-cpu|--browseruse-gpu|--smolagents-gpu)
                parse_config_key "${1#--}"
                ;;

            # C0 - Control VM (bare Ubuntu, no installation)
            --C0)
                log "C0 Control VM - no software installation required"
                log "This is a bare Ubuntu control system"
                exit 0
                ;;

            # Config key shortcuts - M Series (exp-3)
            --M0|--M1|--M2|--M3|--M4)
                parse_config_key "${1#--}"
                ;;

            # Config key shortcuts - B Series
            --B0.llama|--B0.gemma|--B2.llama|--B2.gemma|\
            --B3.llama|--B3.gemma|--B4.llama|--B4.gemma)
                parse_config_key "${1#--}"
                ;;

            # Config key shortcuts - S Series
            --S0.llama|--S0.gemma|--S2.llama|--S2.gemma|\
            --S3.llama|--S3.gemma|--S4.llama|--S4.gemma)
                parse_config_key "${1#--}"
                ;;

            # Config key shortcuts - CPU baselines
            --B0C.llama|--B0C.gemma|--S0C.llama|--S0C.gemma)
                parse_config_key "${1#--}"
                ;;

            # Config key shortcuts - CPU calibrated (summer24)
            --B2C.llama|--B2C.gemma|--S2C.llama|--S2C.gemma)
                parse_config_key "${1#--}"
                ;;

            # Config key shortcuts - RTX baselines
            --B0R.llama|--B0R.gemma|--S0R.llama|--S0R.gemma)
                parse_config_key "${1#--}"
                ;;

            # Config key shortcuts - RTX iteration 2
            --B2R.llama|--B2R.gemma|--S2R.llama|--S2R.gemma)
                parse_config_key "${1#--}"
                ;;

            # Deprecated config keys (still accepted)
            --B1.llama|--B1.gemma|--S1.llama|--S1.gemma|\
            --M1a.llama|--M1b.gemma|--M1c.deepseek|\
            --M2a.llama|--M2b.gemma|--M2c.deepseek|\
            --B1a.llama|--B1b.gemma|--B1c.deepseek|\
            --B2a.llama|--B2b.gemma|--B2c.deepseek|\
            --S1a.llama|--S1b.gemma|--S1c.deepseek|\
            --S2a.llama|--S2b.gemma|--S2c.deepseek|\
            --MC1a.llama|--MC1b.gemma|--MC1c.deepseek|--MC1d.lfm|--MC1e.ministral|--MC1f.qwen|\
            --MC2a.llama|--MC2b.gemma|--MC2c.deepseek|--MC2d.lfm|--MC2e.ministral|--MC2f.qwen|\
            --BC1a.llama|--BC1b.gemma|--BC1c.deepseek|--BC1d.lfm|--BC1e.ministral|--BC1f.qwen|\
            --SC1a.llama|--SC1b.gemma|--SC1c.deepseek|--SC1d.lfm|--SC1e.ministral|--SC1f.qwen)
                log_info "Deprecated config key '${1#--}' - using exp-2 compat"
                parse_config_key "${1#--}"
                ;;

            # Long-form options (support both --arg value and --arg=value)
            --brain) shift; BRAIN="$1" ;;
            --brain=*) BRAIN="${1#*=}" ;;
            --content) shift; CONTENT="$1" ;;
            --content=*) CONTENT="${1#*=}" ;;
            --model) shift; MODEL="$1" ;;
            --model=*) MODEL="${1#*=}" ;;
            --calibration) shift; CALIBRATION="$1" ;;
            --calibration=*) CALIBRATION="${1#*=}" ;;
            --phase) CALIBRATION="summer24" ;;  # Legacy compat

            # Execution options
            --runner) RUNNER=true ;;
            --task) shift; TASK="$1" ;;
            --task=*) TASK="${1#*=}" ;;
            --stage) shift; STAGE="$1" ;&
            --stage=*)
                [[ "$1" == --stage=* ]] && STAGE="${1#*=}"
                if [[ "$STAGE" != "1" && "$STAGE" != "2" ]]; then
                    log_error "Invalid stage: $STAGE (must be 1 or 2)"
                    exit 1
                fi
                ;;
            --list)
                list_configs
                exit 0
                ;;
            --help|-h)
                usage
                exit 0
                ;;
            *)
                log_error "Unknown option: $1"
                usage
                exit 1
                ;;
        esac
        shift
    done

    # Validate configuration
    if [[ -z "$BRAIN" ]]; then
        log_error "No configuration specified. Use a config key (e.g., --M1) or --brain option."
        echo ""
        usage
        exit 1
    fi

    # Default content to none if not specified
    [[ -z "$CONTENT" ]] && CONTENT="none"

    # Default model based on brain type
    if [[ -z "$MODEL" ]]; then
        if [[ "$BRAIN" == "mchp" || "$BRAIN" == "upstream" ]]; then
            MODEL="none"
        else
            MODEL="llama"
        fi
    fi

    # Generate config key if not set
    if [[ -z "$CONFIG_KEY" ]]; then
        generate_config_key
    fi
}

generate_config_key() {
    local version
    case "$CALIBRATION" in
        summer24) version="2" ;;
        fall24) version="3" ;;
        spring25) version="4" ;;
        *)
            # Baseline version: MCHP=1, BrowserUse/SmolAgents=0
            if [[ "$BRAIN" == "mchp" ]]; then
                version="1"
            else
                version="0"
            fi
            ;;
    esac

    if [[ "$BRAIN" == "mchp" ]]; then
        CONFIG_KEY="M${version}"
    elif [[ "$BRAIN" == "browseruse" ]]; then
        CONFIG_KEY="B${version}.${MODEL}"
    elif [[ "$BRAIN" == "smolagents" ]]; then
        CONFIG_KEY="S${version}.${MODEL}"
    else
        CONFIG_KEY="custom"
    fi
}

# ============================================================================
# Installation Functions
# ============================================================================

AUTOMATIC_APT_UNITS=(
    apt-daily.service
    apt-daily.timer
    apt-daily-upgrade.service
    apt-daily-upgrade.timer
    unattended-upgrades.service
)

disable_automatic_apt() {
    log "Stopping and masking automatic APT activity..."
    sudo systemctl mask --now "${AUTOMATIC_APT_UNITS[@]}"
}

automatic_apt_unit_is_safe() {
    local enabled_state="$1"
    local active_state="$2"
    [[ "$enabled_state" == "masked" && \
        ( "$active_state" == "inactive" || "$active_state" == "failed" ) ]]
}

verify_automatic_apt_disabled() {
    local unit enabled_state active_state

    for unit in "${AUTOMATIC_APT_UNITS[@]}"; do
        enabled_state=$(sudo systemctl is-enabled "$unit" 2>/dev/null || true)
        active_state=$(sudo systemctl is-active "$unit" 2>/dev/null || true)
        if ! automatic_apt_unit_is_safe "$enabled_state" "$active_state"; then
            log_error "Automatic APT unit $unit is not safely disabled (enabled=$enabled_state, active=$active_state)"
            return 1
        fi
    done

    log "Automatic APT timers and services are masked and unable to run"
}

install_ollama() {
    local model_name="${MODEL_NAMES[$MODEL]}"
    [[ -z "$model_name" ]] && return 0  # No model needed

    log "Setting up Ollama with model: $model_name"
    if [ -f "$SCRIPT_DIR/decoys/install_scripts/install_ollama.sh" ]; then
        chmod +x "$SCRIPT_DIR/decoys/install_scripts/install_ollama.sh"
        export OLLAMA_MODELS="$model_name"
        "$SCRIPT_DIR/decoys/install_scripts/install_ollama.sh"
        log "Ollama setup complete"
    else
        log_error "install_ollama.sh not found"
        exit 1
    fi
}

NVIDIA_DRIVER_INSTALLED=false

install_nvidia_driver() {
    # Ollama ships the GPU user-space runtime it needs. Canonical workflows do
    # not compile CUDA code, so install the NVIDIA driver without the toolkit.

    # Check if GPU is present
    if ! lspci | grep -qi nvidia; then
        log "No NVIDIA GPU detected, skipping NVIDIA driver installation"
        return 0
    fi

    log "NVIDIA GPU detected: $(lspci | grep -i nvidia | head -1)"

    if command -v nvidia-smi &>/dev/null && nvidia-smi -L &>/dev/null; then
        log "NVIDIA driver is already active"
        return 0
    fi

    # Step 1: Install kernel headers and build tools (REQUIRED for DKMS)
    log "Installing kernel headers and build tools for $(uname -r)..."
    sudo apt-get install -y build-essential linux-headers-$(uname -r)

    # Step 2: Setup NVIDIA repository containing the pinned driver package
    log "Setting up NVIDIA driver repository..."
    wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb -O /tmp/cuda-keyring.deb
    sudo dpkg -i /tmp/cuda-keyring.deb
    rm -f /tmp/cuda-keyring.deb
    sudo apt-get update -y

    # Step 3: Install NVIDIA driver (specific version for compatibility)
    log "Installing NVIDIA driver 580..."
    sudo apt-get install -y nvidia-driver-580

    # Step 4: Enable nvidia-persistenced for better GPU management
    log "Enabling nvidia-persistenced..."
    sudo systemctl enable nvidia-persistenced || true

    NVIDIA_DRIVER_INSTALLED=true
    log "NVIDIA driver 580 installed. One reboot is required for the driver to load."
}

install_firefox_deb() {
    # Install Firefox from Mozilla's official deb repository (not snap)
    # Snap Firefox causes Selenium/geckodriver timeouts due to sandbox confinement
    # See: https://support.mozilla.org/en-US/kb/install-firefox-linux

    log "Setting up Mozilla APT repository for Firefox..."

    # Remove snap Firefox if present
    if snap list firefox &>/dev/null; then
        log "Removing snap Firefox (causes Selenium compatibility issues)..."
        sudo snap remove firefox 2>/dev/null || true
    fi

    # Create keyring directory
    sudo install -d -m 0755 /etc/apt/keyrings

    # Import Mozilla APT signing key
    wget -q https://packages.mozilla.org/apt/repo-signing-key.gpg -O- | \
        sudo tee /etc/apt/keyrings/packages.mozilla.org.asc > /dev/null

    # Add Mozilla APT repository
    echo "deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main" | \
        sudo tee /etc/apt/sources.list.d/mozilla.list > /dev/null

    # Configure APT to prioritize Mozilla packages
    echo 'Package: *
Pin: origin packages.mozilla.org
Pin-Priority: 1000
' | sudo tee /etc/apt/preferences.d/mozilla > /dev/null

    # Install Firefox from Mozilla repo
    sudo apt-get update -y
    sudo apt-get install -y firefox

    log "Firefox deb installed: $(firefox --version)"
}

install_chrome() {
    # Install Google Chrome from official repository
    # Required for M0 upstream MITRE pyhuman (uses Chrome by default)
    # Ref: https://github.com/mitre/human/wiki#installation--setup

    log "Setting up Google Chrome APT repository..."

    # Add Google's signing key
    wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | sudo gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg

    # Add Chrome repository
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" | \
        sudo tee /etc/apt/sources.list.d/google-chrome.list > /dev/null

    # Install Chrome
    sudo apt-get update -y
    sudo apt-get install -y google-chrome-stable

    log "Google Chrome installed: $(google-chrome --version)"
}

install_chromedriver() {
    # Install ChromeDriver matching Chrome version
    # Required for Selenium-based automation with Chrome

    log "Installing ChromeDriver..."

    # Get Chrome version
    CHROME_VERSION=$(google-chrome --version | grep -oP '\d+\.\d+\.\d+' | head -1)
    CHROME_MAJOR=$(echo "$CHROME_VERSION" | cut -d. -f1)

    log "Chrome version: $CHROME_VERSION (major: $CHROME_MAJOR)"

    # For Chrome 115+, use Chrome for Testing repository
    if [[ "$CHROME_MAJOR" -ge 115 ]]; then
        # Get the latest chromedriver version for this Chrome major version
        CHROMEDRIVER_URL="https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}/linux64/chromedriver-linux64.zip"

        # Try to download, fall back to latest stable if specific version fails
        if ! wget -q "$CHROMEDRIVER_URL" -O /tmp/chromedriver.zip 2>/dev/null; then
            log "Specific version not found, fetching latest stable ChromeDriver..."
            LATEST_VERSION=$(curl -s "https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_${CHROME_MAJOR}")
            CHROMEDRIVER_URL="https://storage.googleapis.com/chrome-for-testing-public/${LATEST_VERSION}/linux64/chromedriver-linux64.zip"
            wget -q "$CHROMEDRIVER_URL" -O /tmp/chromedriver.zip
        fi

        sudo unzip -o /tmp/chromedriver.zip -d /tmp/
        sudo mv /tmp/chromedriver-linux64/chromedriver /usr/local/bin/
        sudo chmod +x /usr/local/bin/chromedriver
        rm -rf /tmp/chromedriver.zip /tmp/chromedriver-linux64
    else
        # Legacy chromedriver download for older Chrome versions
        CHROMEDRIVER_VERSION=$(curl -s "https://chromedriver.storage.googleapis.com/LATEST_RELEASE_${CHROME_MAJOR}")
        wget -q "https://chromedriver.storage.googleapis.com/${CHROMEDRIVER_VERSION}/chromedriver_linux64.zip" -O /tmp/chromedriver.zip
        sudo unzip -o /tmp/chromedriver.zip -d /usr/local/bin/
        sudo chmod +x /usr/local/bin/chromedriver
        rm -f /tmp/chromedriver.zip
    fi

    log "ChromeDriver installed: $(chromedriver --version)"
}

install_system_deps() {
    log "Installing system dependencies for $BRAIN..."

    sudo apt-get update -y
    sudo apt-get install -y python3-pip python3-venv python3-dev build-essential

    case "$BRAIN" in
        scripted)
            sudo apt-get install -y xvfb xdg-utils unzip
            install_chrome
            install_chromedriver
            ;;
        upstream)
            # M0: Upstream MITRE pyhuman requires Chrome (not Firefox)
            # Ref: https://github.com/mitre/human/wiki#installation--setup
            sudo apt-get install -y xvfb xdg-utils libxml2-dev libxslt-dev python3-tk scrot unzip
            install_chrome
            install_chromedriver
            ;;
        mchp)
            sudo apt-get install -y xvfb openbox xdg-utils libxml2-dev libxslt-dev python3-tk scrot libreoffice
            # Install Firefox from Mozilla deb repo (not snap)
            install_firefox_deb
            # Install Geckodriver
            log "Installing Geckodriver..."
            if [[ "$(uname -m)" == "x86_64" ]]; then
                wget -q "https://github.com/mozilla/geckodriver/releases/download/v0.37.0/geckodriver-v0.37.0-linux64.tar.gz" -O /tmp/geckodriver.tar.gz
                sudo tar -xzf /tmp/geckodriver.tar.gz -C /usr/local/bin/
                sudo chmod +x /usr/local/bin/geckodriver
                rm -f /tmp/geckodriver.tar.gz
            fi
            ;;
        browseruse)
            sudo apt-get install -y xvfb
            ;;
        smolagents)
            sudo apt-get install -y xvfb ffmpeg
            ;;
    esac

    # Model-backed GPU configurations require the NVIDIA driver. CPU hosts have
    # no NVIDIA PCI device and canonical CPU controls have MODEL=none.
    if [[ "$MODEL" != "none" ]]; then
        install_nvidia_driver
    fi
}

install_python_deps() {
    local venv_path="$1"

    log "Installing Python dependencies..."
    source "$venv_path/bin/activate"
    pip install --upgrade pip
    # Strict DECOY behavior V2 contract validation (Draft 2020-12).
    pip install 'jsonschema>=4.10,<5'

    # Base deps for brain type
    case "$BRAIN" in
        scripted)
            pip install selenium requests
            ;;
        mchp)
            pip install selenium beautifulsoup4 lxml pyautogui lorem requests
            # Add LiteLLM if content augmentation is enabled
            if [[ "$CONTENT" == "llm" ]]; then
                pip install litellm torch transformers
            fi
            ;;
        smolagents)
            # smolagents pinned: the step-action log parser (_SMOL_ACTION_PATTERNS
            # in common/logging/llm_callbacks.py) keys on this version's tool-call
            # vocabulary. Bumping unpinned silently breaks step logging.
            pip install 'smolagents==1.25.0' litellm requests markdownify ddgs yt-dlp
            python -c 'import markdownify, requests; from smolagents import VisitWebpageTool; VisitWebpageTool()'
            ;;
        browseruse)
            # browser-use pinned: _BU_ACTION_MAP in brains/browseruse/agent.py keys
            # on this version's action names (renamed across the 0.12.x line).
            # Bumping unpinned silently breaks step logging — confirmed 2026-05-25.
            pip install 'browser-use==0.12.7' langchain-ollama playwright requests
            playwright install --with-deps chromium
            ;;
    esac

    deactivate
}

copy_source_code() {
    local dest_dir="$1"
    log "Copying source code to $dest_dir..."

    mkdir -p "$dest_dir/decoys"
    cp -r "$SCRIPT_DIR/decoys/brains" "$dest_dir/decoys/"
    cp -r "$SCRIPT_DIR/decoys/runners" "$dest_dir/decoys/"
    cp -r "$SCRIPT_DIR/decoys/augmentations" "$dest_dir/decoys/"
    cp -r "$SCRIPT_DIR/decoys/common" "$dest_dir/decoys/"
    cp -r "$SCRIPT_DIR/decoys/sup" "$dest_dir/decoys/"
    mkdir -p "$dest_dir/contracts"
    cp -r "$SCRIPT_DIR/contracts/decoy" "$dest_dir/contracts/"
    if is_phase_workflow_config "$CONFIG_KEY"; then
        cp -r "$SCRIPT_DIR/decoys/phase_workflow" "$dest_dir/decoys/"
        cp -r "$SCRIPT_DIR/contracts/phase-workflow-plan-v1" "$dest_dir/contracts/"
        mkdir -p "$dest_dir/behavioral_configurations"
        local workflow_behavior_path="${RUSE_WORKFLOW_BEHAVIOR_PATH:-}"
        if [[ ! -f "$workflow_behavior_path" ]]; then
            log_error "RUSE_WORKFLOW_BEHAVIOR_PATH must name the assigned canonical workflow plan"
            exit 1
        fi
        cp "$workflow_behavior_path" \
            "$dest_dir/behavioral_configurations/behavior.json"
    fi
    touch "$dest_dir/decoys/__init__.py"
}

create_run_script() {
    local deploy_dir="$1"
    local run_script="$deploy_dir/run_agent.sh"

    log "Creating run script: $run_script"

    # Map content to runner args
    local content_arg="none"
    [[ "$CONTENT" == "llm" ]] && content_arg="llm"

    local calibration_arg=""
    [[ "$CALIBRATION" != "none" ]] && calibration_arg="--calibration=$CALIBRATION"

    local model_name="${MODEL_NAMES[$MODEL]:-llama3.1:8b}"
    local workflow_gpu_tier="${RUSE_WORKFLOW_GPU_TIER:-v100}"
    local browseruse_start_timeout=""

    if is_phase_workflow_config "$CONFIG_KEY" && [[ "$BRAIN" == "browseruse" ]]; then
        # Ten simultaneous BrowserUse sessions can legitimately need longer
        # than the framework's 30-second BrowserStartEvent default while
        # Chromium processes initialize. Keep that event bounded without
        # changing workflow or LLM action deadlines.
        browseruse_start_timeout='export TIMEOUT_BrowserStartEvent="75"'
    fi

    # Build model arg (skip if none)
    local model_arg=""
    [[ "$MODEL" != "none" ]] && model_arg="--model=$MODEL"

    # Build runner command based on brain
    local runner_cmd=""
    local xvfb_prefix=""

    if is_phase_workflow_config "$CONFIG_KEY"; then
        runner_cmd="python3 -m sup $CONFIG_KEY --behavior-config-dir=$deploy_dir/behavioral_configurations"
        if [[ "$BRAIN" == "mchp" ]]; then
            # LibreOffice needs a window manager for mapped, focusable GUI
            # windows. Keep it inside the same Xvfb/service ownership group.
            xvfb_prefix="xvfb-run -a sh -c 'openbox >/dev/null 2>&1 & exec \"\$@\"' sh "
        fi
    else
        case "$BRAIN" in
            upstream)
                # M0: Run upstream MITRE pyhuman via RUSE wrapper
                runner_cmd="python3 -m runners.run_m0"
                xvfb_prefix=""  # xvfb is handled inside run_m0.py
                ;;
            mchp)
                runner_cmd="python3 -m runners.run_mchp --content=$content_arg $model_arg $calibration_arg"
                xvfb_prefix="xvfb-run -a "
                ;;
            smolagents)
                # Always use loop mode for continuous execution and JSONL logging
                runner_cmd="python3 -m runners.run_smolagents --loop $model_arg $calibration_arg"
                ;;
            browseruse)
                # Always use loop mode for continuous execution and JSONL logging
                runner_cmd="python3 -m runners.run_browseruse --loop $model_arg $calibration_arg"
                xvfb_prefix="xvfb-run -a "
                ;;
        esac
    fi

    cat > "$run_script" << EOF
#!/bin/bash
# $CONFIG_KEY Agent Runner
# Generated by INSTALL_SUP.sh

set -e

cd "$deploy_dir"
source venv/bin/activate

# Configuration
export PATH="\$HOME/.local/bin:\$PATH"
export OLLAMA_MODEL="$model_name"
export LITELLM_MODEL="ollama/$model_name"
export RUSE_WORKFLOW_GPU_TIER="$workflow_gpu_tier"
export PYTHONPATH="$deploy_dir/decoys:\${PYTHONPATH:-}"
export LOG_DIR="$deploy_dir/logs"
export SUP_CONFIG_KEY="$CONFIG_KEY"
export CALIBRATION_PROFILE="${CALIBRATION}"
$browseruse_start_timeout

# Task (for LLM agents)
TASK="\${1:-Research the latest technology news}"

cd decoys
${xvfb_prefix}${runner_cmd}

deactivate
EOF
    chmod +x "$run_script"
}

create_systemd_service() {
    local service_name="$1"
    local deploy_dir="$2"
    local runtime_directory_directives=""

    if is_phase_workflow_config "$CONFIG_KEY"; then
        runtime_directory_directives=$'RuntimeDirectory=ruse\nRuntimeDirectoryMode=0700'
    fi

    log "Creating systemd service: $service_name"

    sudo tee "/etc/systemd/system/${service_name}.service" > /dev/null << EOF
[Unit]
Description=$CONFIG_KEY SUP Agent
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$deploy_dir
ExecStart=/bin/bash $deploy_dir/run_agent.sh
Restart=always
RestartSec=5s
$runtime_directory_directives
StandardOutput=append:$deploy_dir/logs/systemd.log
StandardError=append:$deploy_dir/logs/systemd_error.log

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable "${service_name}.service"
}

# ============================================================================
# Runner Mode (Direct Execution)
# ============================================================================

run_directly() {
    log "Running $CONFIG_KEY directly (development mode)..."

    # Install Ollama if needed
    if [[ "$MODEL" != "none" ]]; then
        install_ollama
    fi

    # Set environment
    export PYTHONPATH="$SCRIPT_DIR/decoys:${PYTHONPATH:-}"
    export OLLAMA_MODEL="${MODEL_NAMES[$MODEL]:-llama3.1:8b}"
    export LITELLM_MODEL="ollama/$OLLAMA_MODEL"
    export CALIBRATION_PROFILE="${CALIBRATION}"

    cd "$SCRIPT_DIR/decoys"

    # Build model arg (skip if none)
    local model_arg=""
    [[ "$MODEL" != "none" ]] && model_arg="--model=$MODEL"

    # Build calibration arg
    local calibration_arg=""
    [[ "$CALIBRATION" != "none" ]] && calibration_arg="--calibration=$CALIBRATION"

    # Map content to runner args
    local content_arg="none"
    [[ "$CONTENT" == "llm" ]] && content_arg="llm"

    if is_phase_workflow_config "$CONFIG_KEY"; then
        local workflow_behavior_path="${RUSE_WORKFLOW_BEHAVIOR_PATH:-}"
        if [[ ! -f "$workflow_behavior_path" || "$(basename "$workflow_behavior_path")" != "behavior.json" ]]; then
            log_error "Canonical runner requires RUSE_WORKFLOW_BEHAVIOR_PATH to an installed behavior.json"
            exit 1
        fi
        local behavior_dir
        behavior_dir="$(dirname "$workflow_behavior_path")"
        if [[ "$BRAIN" == "mchp" ]]; then
            exec xvfb-run -a sh -c \
                'openbox >/dev/null 2>&1 & exec "$@"' sh \
                python3 -m sup "$CONFIG_KEY" \
                --behavior-config-dir="$behavior_dir"
        fi
        exec python3 -m sup "$CONFIG_KEY" --behavior-config-dir="$behavior_dir"
    fi

    case "$BRAIN" in
        mchp)
            log "Running MCHP agent..."
            exec xvfb-run -a python3 -m runners.run_mchp --content="$content_arg" $model_arg $calibration_arg
            ;;
        smolagents)
            log "Running SmolAgents loop mode..."
            exec python3 -m runners.run_smolagents --loop $model_arg $calibration_arg
            ;;
        browseruse)
            log "Running BrowserUse loop mode..."
            exec xvfb-run -a python3 -m runners.run_browseruse --loop $model_arg $calibration_arg
            ;;
    esac
}

# ============================================================================
# M0 Upstream MITRE pyhuman Installation
# ============================================================================

install_m0_upstream() {
    log "Installing M0 upstream MITRE pyhuman..."

    # Clone upstream MITRE repo if not already present
    if [[ ! -d "/opt/human" ]]; then
        log "Cloning upstream MITRE pyhuman..."
        sudo git clone https://github.com/mitre/human.git /opt/human
        sudo chown -R "$USER:$USER" /opt/human
    else
        log "Upstream MITRE pyhuman already present at /opt/human"
    fi

    # Create venv and install deps for upstream pyhuman
    if [[ ! -d "/opt/human/pyhuman/venv" ]]; then
        log "Creating upstream pyhuman virtual environment..."
        python3 -m venv /opt/human/pyhuman/venv
        source /opt/human/pyhuman/venv/bin/activate
        pip install --upgrade pip
        pip install -r /opt/human/pyhuman/requirements.txt
        deactivate
    else
        log "Upstream pyhuman venv already exists"
    fi

    log "M0 upstream installation complete"
}

# ============================================================================
# Full Installation
# ============================================================================

install_agent() {
    # Generate deploy name and service name
    local deploy_name="$CONFIG_KEY"
    local deploy_dir="$SCRIPT_DIR/deployed_sups/$deploy_name"
    local service_name=$(echo "$deploy_name" | tr '[:upper:].' '[:lower:]_' | tr '+' 'p')

    log "Installing $CONFIG_KEY..."
    log "  Brain: $BRAIN"
    log "  Content: $CONTENT"
    log "  Model: $MODEL"
    log "  Calibration: $CALIBRATION"
    log "  Deploy directory: $deploy_dir"
    log "  Service name: $service_name"
    [[ "$STAGE" != "0" ]] && log "  Stage: $STAGE"

    if is_phase_workflow_config "$CONFIG_KEY"; then
        # Stop any in-flight daily upgrade before the first direct installer
        # APT command. Masking systemd units does not disable manual apt-get.
        disable_automatic_apt
    fi

    # ========== STAGE 1: System deps and drivers (pre-reboot) ==========
    if [[ "$STAGE" == "0" || "$STAGE" == "1" ]]; then
        log "=== Stage 1: Installing system dependencies ==="

        # Install system dependencies (includes the driver on GPU hosts)
        install_system_deps

        # Create deployment directory
        mkdir -p "$deploy_dir/logs"

        # Check if reboot needed for NVIDIA driver
        if $NVIDIA_DRIVER_INSTALLED; then
            if [[ "$STAGE" == "1" ]]; then
                log "Stage 1 complete. NVIDIA drivers installed - reboot required."
                log "After reboot, run: ./INSTALL_SUP.sh --$CONFIG_KEY --stage=2"
                exit 100  # Special exit code: reboot needed
            else
                log "NVIDIA driver installed. Rebooting in 5 seconds..."
                sleep 5
                sudo reboot
            fi
        fi

        if [[ "$STAGE" == "1" ]]; then
            log "Stage 1 complete. No reboot required."
            exit 0
        fi
    fi

    # ========== STAGE 2: Ollama, Python, services (post-reboot) ==========
    if [[ "$STAGE" == "0" || "$STAGE" == "2" ]]; then
        log "=== Stage 2: Installing application components ==="

        # Ensure deploy dir exists (may be running stage 2 after reboot)
        mkdir -p "$deploy_dir/logs"

        # M0 special handling: Install upstream MITRE pyhuman
        if [[ "$CONFIG_KEY" == "M0" ]]; then
            install_m0_upstream
        fi

        # Install Ollama if needed (skip for M0 - no LLM)
        if [[ "$MODEL" != "none" ]]; then
            install_ollama
        fi

        # Create virtual environment
        log "Creating Python virtual environment..."
        python3 -m venv "$deploy_dir/venv"

        # Install Python dependencies
        install_python_deps "$deploy_dir/venv"

        # Copy source code
        copy_source_code "$deploy_dir"

        # Create run script
        create_run_script "$deploy_dir"

        # Create systemd service
        create_systemd_service "$service_name" "$deploy_dir"

        if is_phase_workflow_config "$CONFIG_KEY"; then
            verify_automatic_apt_disabled
        fi

        # Start service (unless deploy flow asked us to wait — the deploy
        # ordering wants behavior.json to land via distribute-behavior-configs
        # before the service starts, otherwise the brain's mandatory-config
        # check crash-loops the service 60-100x while distribute runs).
        if [[ "${RUSE_NO_SERVICE_START:-0}" == "1" ]]; then
            if is_phase_workflow_config "$CONFIG_KEY"; then
                log "RUSE_NO_SERVICE_START=1 — deployment will start the canonical service after Stage 2."
            else
                log "RUSE_NO_SERVICE_START=1 — skipping start, distribute step will start the service."
            fi
        else
            log "Starting $service_name service..."
            sudo systemctl start "${service_name}.service"
        fi
    fi

    echo ""
    echo -e "${GREEN}================================${NC}"
    echo -e "${GREEN}  INSTALLATION COMPLETE!${NC}"
    echo -e "${GREEN}================================${NC}"
    echo ""
    echo "Configuration: $CONFIG_KEY"
    echo "  Brain:        $BRAIN"
    echo "  Content:      $CONTENT"
    echo "  Model:        $MODEL"
    echo "  Calibration:  $CALIBRATION"
    echo ""
    echo "Deploy directory: $deploy_dir"
    echo "Service: $service_name"
    echo ""
    echo "Commands:"
    echo "  sudo systemctl status $service_name"
    echo "  sudo systemctl stop $service_name"
    echo "  sudo journalctl -u $service_name -f"
    echo ""
}

# ============================================================================
# Main
# ============================================================================

main() {
    if [[ $# -eq 0 ]]; then
        usage
        exit 1
    fi

    parse_args "$@"
    apply_phase_workflow_gpu_tier

    echo ""
    echo -e "${BLUE}================================${NC}"
    echo -e "${BLUE}  RUSE Installer${NC}"
    echo -e "${BLUE}================================${NC}"
    echo ""
    log_info "Config: $CONFIG_KEY"
    log_info "Brain: $BRAIN | Content: $CONTENT | Model: $MODEL"
    log_info "Calibration: $CALIBRATION | Mode: $(if $RUNNER; then echo 'Runner'; else echo 'Install'; fi)"
    echo ""

    if $RUNNER; then
        run_directly
    else
        install_agent
    fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
