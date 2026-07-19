"""Tool schemas exposed by the Project Autopilot plugin."""

AUTOPILOT_STATUS = {
    "name": "autopilot_status",
    "description": (
        "Return the current status of the Project Autopilot, including state machine "
        "state, registration status, and lease validity. Read-only — never modifies state."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
}
