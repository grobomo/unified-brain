# Spec 003: ccc-manager Integration

## Problem
unified-brain can analyze events but has no way to dispatch work to ccc-manager workers or receive results back. The dispatcher.py has placeholder bridge integration but needs a concrete protocol, project registry, and end-to-end validation.

## Solution
1. Verify/harden the bridge dispatch protocol (task JSON format matches BridgeInput)
2. Build result relay — when results arrive, post summaries to originating channels
3. Create shared project registry (YAML) mapping repos/chats to manager configs
4. Add E2E integration test exercising the full pipeline

## Scope
- src/unified_brain/registry.py — project registry
- Harden dispatcher.py bridge output format
- Result relay logic in service.py
- tests/test_integration.py — E2E test with mock adapters
