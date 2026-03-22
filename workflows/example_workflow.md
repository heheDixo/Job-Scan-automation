# Example Workflow Template

## Objective
Describe what this workflow accomplishes in 1-2 sentences.

## Required Inputs
- Input 1: description
- Input 2: description

## Tools
1. `tools/script_one.py` — what it does
2. `tools/script_two.py` — what it does

## Steps
1. Gather required inputs
2. Run `tools/script_one.py` with arguments X and Y
3. Validate output
4. Run `tools/script_two.py` with results from step 2
5. Deliver final output to [destination]

## Expected Outputs
- Description of what success looks like
- Where the output is delivered (e.g., Google Sheet URL, local file path)

## Edge Cases
- **Rate limits**: If you hit a rate limit on API X, wait N seconds and retry
- **Missing data**: If input Y is missing, fall back to Z
- **Auth errors**: Re-run the auth setup in `tools/auth.py`
