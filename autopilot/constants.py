"""Constants for the Hermes Project Autopilot plugin."""

from __future__ import annotations

SCHEMA_VERSION = 1
PLUGIN_VERSION = "0.1.0"
PLUGIN_NAME = "project-autopilot"
STATE_DIR_NAME = "autopilot"

# --- State labels ---
STATE_IDLE = "IDLE"
STATE_CONFIGURED = "CONFIGURED"
STATE_LEASE_READY = "LEASE_READY"
STATE_SIMULATING = "SIMULATING"
STATE_PREPARING_BRIEF = "PREPARING_BRIEF"
STATE_EXECUTING = "EXECUTING"
STATE_VERIFYING = "VERIFYING"
STATE_REVIEWING = "REVIEWING"
STATE_REMEDIATING = "REMEDIATING"
STATE_PHASE_ACCEPTED = "PHASE_ACCEPTED"
STATE_NEEDS_HUMAN = "NEEDS_HUMAN"
STATE_PAUSED = "PAUSED"
STATE_FAILED = "FAILED"
STATE_LEASE_EXPIRED = "LEASE_EXPIRED"
STATE_STOPPED = "STOPPED"

ALL_STATES = {
    STATE_IDLE, STATE_CONFIGURED, STATE_LEASE_READY, STATE_SIMULATING,
    STATE_PREPARING_BRIEF, STATE_EXECUTING, STATE_VERIFYING, STATE_REVIEWING,
    STATE_REMEDIATING, STATE_PHASE_ACCEPTED, STATE_NEEDS_HUMAN, STATE_PAUSED,
    STATE_FAILED, STATE_LEASE_EXPIRED, STATE_STOPPED,
}

TERMINAL_STATES = {STATE_IDLE, STATE_STOPPED, STATE_FAILED, STATE_LEASE_EXPIRED, STATE_PHASE_ACCEPTED}
ERROR_STATES = {STATE_FAILED, STATE_LEASE_EXPIRED}

# --- Risk levels ---
RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

# --- Capability keys ---
CAP_WORKSPACE_READ = "workspace.read"
CAP_WORKSPACE_WRITE = "workspace.write"
CAP_GIT_READ = "git.read"
CAP_GIT_COMMIT = "git.commit"
CAP_GIT_PUSH = "git.push"
CAP_GIT_MERGE = "git.merge"
CAP_GIT_RELEASE = "git.release"
CAP_DEPENDENCY_INSTALL = "dependency.install"
CAP_LOCAL_SERVICE_START = "local-service.start"
CAP_DATABASE_READ = "database.read"
CAP_DATABASE_WRITE = "database.write"
CAP_DATABASE_MIGRATE = "database.migrate"
CAP_PRIVILEGED_ACCOUNT = "privileged.account"
CAP_EXTERNAL_WRITE = "external.write"
CAP_SECRET_ACCESS = "secret.access"
CAP_DEPLOYMENT = "deployment"
CAP_NEXT_PHASE = "next-phase"
CAP_PERSONAL_ACCOUNT = "personal.account"
CAP_USER_INTERACTION = "user.interaction"
CAP_APPROVALS_OFF = "approvals.off"
CAP_YOLO = "yolo"

# Default-deny capabilities: these are NEVER granted unless explicitly in lease
DEFAULT_DENIED_CAPABILITIES = {
    CAP_WORKSPACE_WRITE,
    CAP_GIT_COMMIT,
    CAP_GIT_PUSH,
    CAP_GIT_MERGE,
    CAP_GIT_RELEASE,
    CAP_DEPENDENCY_INSTALL,
    CAP_LOCAL_SERVICE_START,
    CAP_DATABASE_WRITE,
    CAP_DATABASE_MIGRATE,
    CAP_PRIVILEGED_ACCOUNT,
    CAP_EXTERNAL_WRITE,
    CAP_SECRET_ACCESS,
    CAP_DEPLOYMENT,
    CAP_NEXT_PHASE,
    CAP_PERSONAL_ACCOUNT,
    CAP_APPROVALS_OFF,
    CAP_YOLO,
}

# Allowed capabilities for explicit grants in lease
VALID_CAPABILITIES = {
    CAP_WORKSPACE_READ, CAP_WORKSPACE_WRITE,
    CAP_GIT_READ, CAP_GIT_COMMIT, CAP_GIT_PUSH, CAP_GIT_MERGE, CAP_GIT_RELEASE,
    CAP_DEPENDENCY_INSTALL, CAP_LOCAL_SERVICE_START,
    CAP_DATABASE_READ, CAP_DATABASE_WRITE, CAP_DATABASE_MIGRATE,
    CAP_PRIVILEGED_ACCOUNT, CAP_EXTERNAL_WRITE, CAP_SECRET_ACCESS,
    CAP_DEPLOYMENT, CAP_NEXT_PHASE, CAP_PERSONAL_ACCOUNT,
    CAP_USER_INTERACTION, CAP_APPROVALS_OFF, CAP_YOLO,
}

# --- Default lease policy values ---
DEFAULT_MAX_RUNTIME_SECONDS = 3600
DEFAULT_MAX_LOOP_ITERATIONS = 1
DEFAULT_MAX_BUDGET_CENTS = 100

# --- Safe lease presets ---
LEASE_PRESET_PHASE2_READONLY = "phase2-readonly"
LEASE_PRESET_PHASE3_DEVELOPMENT = "phase3-development"
LEASE_PRESET_AUTONOMOUS_DEVELOPMENT = "autonomous-development"
LEASE_PRESET_DURATION_HOURS = 2
LEASE_PRESET_ISSUER = "user"
PHASE2_READONLY_CAPABILITIES = (
    CAP_WORKSPACE_READ,
    CAP_GIT_READ,
)
PHASE3_DEVELOPMENT_CAPABILITIES = (
    CAP_WORKSPACE_READ,
    CAP_GIT_READ,
    CAP_WORKSPACE_WRITE,
    CAP_GIT_COMMIT,
    CAP_NEXT_PHASE,
)
AUTONOMOUS_DEVELOPMENT_CAPABILITIES = (
    CAP_WORKSPACE_READ,
    CAP_GIT_READ,
    CAP_WORKSPACE_WRITE,
    CAP_GIT_COMMIT,
    CAP_GIT_PUSH,
    CAP_NEXT_PHASE,
    CAP_USER_INTERACTION,
)

# --- Storage ---
KILL_SWITCH_FILENAME = "kill_switch.json"
KILL_SWITCH_FILE = KILL_SWITCH_FILENAME  # alias
STATE_FILENAME = "autopilot_state.json"
STATE_FILE_NAME = STATE_FILENAME  # alias for storage.py
LOCK_FILE_SUFFIX = ".lock"
AUDIT_FILE_NAME = "audit.jsonl"
MAX_AUDIT_ROTATION_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_TRANSITION_HISTORY = 100

# --- Risk keyword patterns ---
LOW_RISK_PATTERNS = [
    "rename", "format", "typo", "comment", "docstring", "whitespace",
    "reorder", "clean", "lint", "fix typo", "update doc", "readme",
    "rephrase", "reformat", "sort import", "restructure import",
]

MEDIUM_RISK_PATTERNS = [
    "commit", "push", "merge", "install", "dependency", "npm", "pip",
    "cargo", "yarn", "require", "import", "requirement", "package",
    "branch", "checkout", "rebase",
]

HIGH_RISK_PATTERNS = [
    "security", "secret", "credential", "password", "token", "key",
    "deploy", "production", "prod", "migration", "database",
    "data loss", "delete", "drop", "truncate", "remove account",
    "privileged", "admin", "root", "sudo",
    "next-phase", "phase", "phase 2",
    "yolo", "approvals.mode=off", "disable", "override",
    "personal account", "oauth", "consent", "authorize",
    "business", "pricing", "payment", "billing",
]
