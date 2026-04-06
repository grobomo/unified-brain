# Spec 001: CCC Ecosystem Analysis

## Problem
Four projects (unified-brain, ccc-central, ccc-manager, ccc CLI) have overlapping scope in event ingestion, task dispatch, and worker management. No documented analysis of how they relate, overlap, or should integrate. Need clear role definitions and integration spec before building unified-brain.

## Solution
Analyze all four projects. Document current state, overlap, and proposed architecture. Define unified-brain as the THINKER (context + analysis + memory) and ccc-manager as the DOER (dispatch + workers + verification). Absorb ccc-central. Write findings to notes/ for reference.

## Scope
- Research all four codebases
- Document overlap matrix
- Define integration protocol (task JSON, result JSON, bridge/SQS)
- Write analysis notes with date
- Update TODO.md with revised architecture

## Out of Scope
- Implementing any integration code (Phase 2+)
- Modifying ccc-manager or ccc CLI codebases
