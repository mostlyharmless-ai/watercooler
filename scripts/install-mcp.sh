#!/usr/bin/env bash
# Watercooler MCP Server - Installation Helper Script
#
# Registers the Watercooler MCP server with clients and sets up shared
# configuration so Claude and Codex can collaborate on the same threads.

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Helper functions
error() { echo -e "${RED}Error: $1${NC}" >&2; exit 1; }
success() { echo -e "${GREEN}✓ $1${NC}"; }
info() { echo -e "${YELLOW}→ $1${NC}"; }
warn() { echo -e "${YELLOW}Warning: $1${NC}"; }

# Function to update Codex config.toml with error handling
update_codex_config() {
  local agent_name="$1"
  local watercooler_dir="$2"
  local python_cmd="$3"
  
  local codex_config="${HOME}/.codex/config.toml"
  local codex_dir="${HOME}/.codex"
  
  # Check if ~/.codex directory exists and is writable
  if [[ ! -d "${codex_dir}" ]]; then
    info "Creating ${codex_dir} directory..."
    if ! mkdir -p "${codex_dir}" 2>/dev/null; then
      error "Failed to create ${codex_dir}. Check permissions."
    fi
  fi
  
  if [[ ! -w "${codex_dir}" ]]; then
    error "Directory ${codex_dir} is not writable. Check permissions."
  fi
  
  # Check if config file exists
  if [[ -f "${codex_config}" ]]; then
    # Check if file is writable
    if [[ ! -w "${codex_config}" ]]; then
      error "File ${codex_config} is not writable. Check permissions."
    fi
    
    # Check if watercooler section already exists
    if grep -q "\[mcp_servers.watercooler\]" "${codex_config}"; then
      echo ""
      warn "Watercooler configuration already exists in ${codex_config}"
      read -p "Overwrite existing configuration? (y/n) " -n 1 -r; echo
      if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        info "Skipping Codex configuration update"
        return 0
      fi
      
      info "Removing existing watercooler configuration..."
      # Create backup before modifying
      if ! cp "${codex_config}" "${codex_config}.backup-$(date +%Y%m%d-%H%M%S)" 2>/dev/null; then
        error "Failed to create backup of ${codex_config}"
      fi
      
      # Remove existing watercooler section
      # This removes from [mcp_servers.watercooler] to the next section or EOF
      if ! sed -i.tmp '/^\[mcp_servers\.watercooler\]/,/^\[/{ /^\[mcp_servers\.watercooler\]/d; /^\[/!d; }' "${codex_config}" 2>/dev/null; then
        error "Failed to remove existing watercooler configuration"
      fi
      rm -f "${codex_config}.tmp"
    else
      info "Adding watercooler configuration to ${codex_config}..."
    fi
  else
    info "Creating new config file: ${codex_config}..."
  fi
  
  # Append new configuration
  if ! cat >> "${codex_config}" <<TOML

# Watercooler MCP server
[mcp_servers.watercooler]
command = "${python_cmd}"
args = ["-m", "watercooler_mcp"]

[mcp_servers.watercooler.env]
WATERCOOLER_AGENT = "${agent_name}"
TOML
  then
    error "Failed to write watercooler configuration to ${codex_config}"
  fi
  
  # Add WATERCOOLER_DIR if specified
  if [[ -n "${watercooler_dir}" ]]; then
    if ! echo "WATERCOOLER_DIR = \"${watercooler_dir}\"" >> "${codex_config}"; then
      error "Failed to add WATERCOOLER_DIR to ${codex_config}"
    fi
  fi
  
  success "Updated ${codex_config}"
  return 0
}

# Ensure we are in repo root
if [[ ! -f "pyproject.toml" ]] || ! grep -q '^name = "watercooler"' pyproject.toml 2>/dev/null; then
  error "This script must be run from the watercooler directory"
fi

PROJECT_ROOT="$(pwd)"
SERVER_PATH="${PROJECT_ROOT}/src/watercooler_mcp/server.py"
[[ -f "${SERVER_PATH}" ]] || error "Server file not found at ${SERVER_PATH}"

echo "======================================"
echo "Watercooler MCP Server Installation"
echo "======================================"
echo ""

# Check if in a virtual environment (venv or conda)
if [[ -n "${VIRTUAL_ENV}" ]]; then
  success "Virtual environment detected: ${VIRTUAL_ENV}"
elif [[ -n "${CONDA_PREFIX}" ]]; then
  success "Conda environment detected: ${CONDA_DEFAULT_ENV} (${CONDA_PREFIX})"
else
  warn "No virtual environment detected!"
  echo ""
  echo "It's recommended to activate a virtual environment first:"
  echo "  python3 -m venv .venv"
  echo "  source .venv/bin/activate  # On macOS/Linux"
  echo "  # OR: conda activate your-env"
  echo ""
  read -p "Continue without virtual environment? (y/n) " -n 1 -r; echo
  if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    info "Installation cancelled. Activate your virtual environment and run again."
    exit 0
  fi
fi
echo ""

# Step 0: Choose target client(s)
info "Choose target client:"
echo "  1) Claude (Claude Code/Desktop)"
echo "  2) Codex (CLI / VS Code via Cline)"
echo "  3) Both (Claude + Codex)"
read -p "Client (1-3, default: 1): " CLIENT_CHOICE
CLIENT_CHOICE=${CLIENT_CHOICE:-1}
case $CLIENT_CHOICE in
  1) CLIENT="claude" ; DEFAULT_AGENT="Claude" ;;
  2) CLIENT="codex"  ; DEFAULT_AGENT="Codex"  ;;
  3) CLIENT="both"   ; DEFAULT_AGENT="Claude" ;;
  *) error "Invalid client choice" ;;
esac

# Step 1: Ensure package (with MCP extras) is available
info "Checking installation..."

# Select a Python interpreter (require 3.10+); prefer python3, else specific versions
pick_python() {
  local candidates=(python3 python3.12 python3.11 python3.10)
  for c in "${candidates[@]}"; do
    if command -v "$c" >/dev/null 2>&1; then
      local ver
      ver="$($c -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)" || true
      local major minor
      major="${ver%%.*}"
      minor="$(echo "$ver" | cut -d. -f2)"
      if [[ -n "$major" && -n "$minor" ]] && (( major > 3 || (major == 3 && minor >= 10) )); then
        command -v "$c"
        return 0
      fi
    fi
  done
  return 1
}

PY="$(pick_python || true)"
[[ -n "${PY}" ]] || error "Python 3.10+ not found. Install Python 3.10+ and ensure 'python3' resolves to it, or install a specific binary like python3.10. See docs/CLAUDE_CODE_SETUP.md → Using Specific Python Version."

# Get the absolute path to the Python executable
PY_PATH="$(which "${PY}" 2>/dev/null || command -v "${PY}")"
[[ -n "${PY_PATH}" ]] || error "Failed to resolve absolute path for ${PY}"

info "Using Python: $(${PY} --version) at ${PY_PATH}"

if ! "${PY}" -c "import watercooler_mcp" 2>/dev/null; then
  echo "watercooler[mcp] is not installed."
  read -p "Install now? (y/n) " -n 1 -r; echo
  if [[ $REPLY =~ ^[Yy]$ ]]; then
    info "Installing watercooler[mcp]..."
    "${PY}" -m pip install -e ".[mcp]"
    success "Installation complete"
  else
    error "Installation required. Run: pip install -e .[mcp]"
  fi
else
  success "watercooler[mcp] is installed"
fi

# Step 2: Agent identity overrides (optional)
echo ""
info "Agent identity (optional):"
echo "  Defaults: Claude → 'Claude', Codex → 'Codex' (or client-provided)."
if [[ "${CLIENT}" == "both" ]]; then
  read -p "Claude agent override (default: Claude, blank=auto): " AGENT_NAME_CLAUDE
  read -p "Codex agent override  (default: Codex,  blank=auto): " AGENT_NAME_CODEX
else
  read -p "Agent name override (default: ${DEFAULT_AGENT}, blank=auto): " AGENT_NAME
  if [[ -z "${AGENT_NAME}" && "${CLIENT}" == "codex" ]]; then
    AGENT_NAME="${DEFAULT_AGENT}"
  fi
fi

# Step 3: Threads directory
echo ""
info "Where is your .watercooler directory?"
echo "  Leave blank to use dynamic discovery (./.watercooler)"
echo "  Or provide an absolute path for a specific project."
read -p "Watercooler directory (default: dynamic): " WATERCOOLER_DIR

# Step 4: Set scope for Claude registration (user scope for all projects)
if [[ "${CLIENT}" == "claude" || "${CLIENT}" == "both" ]]; then
  SCOPE="user"
fi

# Step 5: Prepare installation
echo ""
info "Preparing watercooler MCP server installation..."

# Step 6: Build command(s)
if [[ "${CLIENT}" == "claude" ]]; then
  # Check for claude CLI
  if ! command -v claude >/dev/null 2>&1; then
    error "'claude' CLI not found in PATH. Install Claude Desktop and ensure the 'claude' CLI is available. See docs/CLAUDE_DESKTOP_SETUP.md."
  fi
  
  # Build command
  CMD="claude mcp add watercooler --scope ${SCOPE}"
  [[ -n "${AGENT_NAME}" ]] && CMD+=" -e WATERCOOLER_AGENT=${AGENT_NAME}"
  [[ -n "${WATERCOOLER_DIR}" ]] && CMD+=" -e WATERCOOLER_DIR=${WATERCOOLER_DIR}"
  CMD+=" -- ${PY_PATH} -m watercooler_mcp"
  
elif [[ "${CLIENT}" == "codex" ]]; then
  # Codex uses TOML config
  CODEX_TOML=1
  
else
  # Both Claude and Codex
  # Check for claude CLI
  if ! command -v claude >/dev/null 2>&1; then
    error "'claude' CLI not found in PATH. Install Claude Desktop and ensure the 'claude' CLI is available. See docs/CLAUDE_DESKTOP_SETUP.md."
  fi
  
  # Build Claude command
  CMD_CLAUDE="claude mcp add watercooler --scope ${SCOPE}"
  [[ -n "${AGENT_NAME_CLAUDE}" ]] && CMD_CLAUDE+=" -e WATERCOOLER_AGENT=${AGENT_NAME_CLAUDE}" || CMD_CLAUDE+=" -e WATERCOOLER_AGENT=Claude"
  [[ -n "${WATERCOOLER_DIR}" ]] && CMD_CLAUDE+=" -e WATERCOOLER_DIR=${WATERCOOLER_DIR}"
  CMD_CLAUDE+=" -- ${PY_PATH} -m watercooler_mcp"
  
  # Codex uses TOML config
  CODEX_TOML=1
fi

# Step 7: Show installation plan
echo ""
echo "========================================"
echo "Ready to Install"
echo "========================================"
echo ""
if [[ "${CLIENT}" == "both" ]]; then
  echo "[Claude] Will execute:"
  echo "  ${CMD_CLAUDE}"
  echo ""
  echo "[Codex] Will update ~/.codex/config.toml with:"
  cat <<TOML
  [mcp_servers.watercooler]
  command = "${PY_PATH}"
  args = ["-m", "watercooler_mcp"]

  [mcp_servers.watercooler.env]
  WATERCOOLER_AGENT = "${AGENT_NAME_CODEX:-Codex}"
TOML
  if [[ -n "${WATERCOOLER_DIR}" ]]; then
    echo "  WATERCOOLER_DIR = \"${WATERCOOLER_DIR}\""
  fi
elif [[ "${CLIENT}" == "codex" ]]; then
  echo "Will update ~/.codex/config.toml with:"
  cat <<TOML
  [mcp_servers.watercooler]
  command = "${PY_PATH}"
  args = ["-m", "watercooler_mcp"]

  [mcp_servers.watercooler.env]
  WATERCOOLER_AGENT = "${AGENT_NAME:-Codex}"
TOML
  if [[ -n "${WATERCOOLER_DIR}" ]]; then
    echo "  WATERCOOLER_DIR = \"${WATERCOOLER_DIR}\""
  fi
else
  echo "Will execute:"
  echo "  ${CMD}"
fi

echo ""; read -p "Proceed with installation? (y/n) " -n 1 -r; echo

# Step 8: Execute registration (Claude) and finish
if [[ $REPLY =~ ^[Yy]$ ]]; then
  if [[ "${CLIENT}" == "claude" ]]; then
    set +e
    eval "${CMD}"
    RC=$?
    set -e
    if [[ ${RC} -ne 0 ]]; then
      error "Claude registration command failed (exit ${RC}). Review the command above and see docs/CLAUDE_DESKTOP_SETUP.md."
    fi
    echo ""; success "Watercooler MCP server registered!"
    echo ""; echo "Next steps:"; echo "  1. Verify installation:"; echo "     claude mcp list"; echo ""
    echo "  2. In Claude Code, test with:"; echo "     'Can you use the watercooler_health tool?'"
  elif [[ "${CLIENT}" == "codex" ]]; then
    # Update Codex configuration
    update_codex_config "${AGENT_NAME:-Codex}" "${WATERCOOLER_DIR}" "${PY_PATH}"
    
    echo ""; echo "Next steps:"
    echo "  1. Restart Codex to load the new configuration"
    echo "  2. Test with: watercooler_health"
  else
    # Both Claude and Codex
    eval "${CMD_CLAUDE}"; echo ""; success "Claude MCP registration completed."

    # Update Codex configuration
    update_codex_config "${AGENT_NAME_CODEX:-Codex}" "${WATERCOOLER_DIR}" "${PY_PATH}"
  fi

  echo ""; echo "Configuration:"
  if [[ "${CLIENT}" == "both" ]]; then
    echo "  Claude Agent: ${AGENT_NAME_CLAUDE:-auto (Claude)}"
    echo "  Codex Agent:  ${AGENT_NAME_CODEX:-auto (Codex)}"
  else
    if [[ -n "${AGENT_NAME}" ]]; then
      echo "  Agent: ${AGENT_NAME}"
    else
      echo "  Agent: Auto-detected from MCP client"
    fi
  fi
  if [[ -n "${WATERCOOLER_DIR}" ]]; then
    echo "  Directory: ${WATERCOOLER_DIR}"
  else
    echo "  Directory: ./.watercooler (dynamic)"
  fi
  if [[ "${CLIENT}" == "claude" || "${CLIENT}" == "both" ]]; then
    echo "  Scope: ${SCOPE}"
  fi
else
  info "Installation cancelled"
  echo "You can run the commands shown above manually."
fi
