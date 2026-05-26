# Copilot Bridge - Push/Pull Mechanism

## Overview
A sync mechanism between two systems:
- **COPO** (has Copilot, sites blocked) → Writes/rewrites code
- **NOCOPO** (no Copilot, sites unblocked) → Runs code and returns output

## How It Works

```
┌─────────────────────┐                    ┌─────────────────────┐
│   COPO SYSTEM       │                    │   NOCOPO SYSTEM     │
│   (Has Copilot)     │                    │   (Can run code)    │
│                     │                    │                     │
│ 1. Write code       │──── push code ────▶│ 2. Pull code        │
│                     │     to GitHub      │ 3. Run code         │
│ 5. Read output      │◀── push output ───│ 4. Push output      │
│ 6. Satisfied? ──────│                    │                     │
│    No → rewrite     │                    │    Polls every 10s  │
│    Yes → done       │                    │                     │
└─────────────────────┘                    └─────────────────────┘
```

## Setup

### Both Systems
```bash
pip install requests
```

### COPO System (Copilot system)
1. Copy `copo_system.py` to the COPO machine
2. Run: `python copo_system.py`
3. Create `code_to_push.py` in the same directory with your Python code
4. The script will push it and wait for output

### NOCOPO System (Testing system)
1. Copy `nocopo_system.py` to the NOCOPO machine
2. Run: `python nocopo_system.py`
3. It will automatically poll, run code, and push output

## Workflow
1. **COPO**: Write code in `code_to_push.py` → script pushes to GitHub
2. **NOCOPO**: Detects new code → pulls → runs → pushes output
3. **COPO**: Detects output → displays it → asks if satisfied
4. If not satisfied → update `code_to_push.py` → press Enter → repeat
5. If satisfied → marks complete → NOCOPO stops

## Files on GitHub Repo (`copilot-bridge`)
- `code.py` - The Python code to test
- `output.txt` - Execution output (stdout + stderr + exit code)
- `status.json` - State machine tracking who should act next

## Status States
| State | Meaning |
|-------|---------|
| `code_ready` | COPO pushed new code, waiting for NOCOPO |
| `output_ready` | NOCOPO pushed output, waiting for COPO |
| `satisfied` | COPO is satisfied, process complete |
