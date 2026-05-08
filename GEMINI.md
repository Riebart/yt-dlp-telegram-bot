# GEMINI.md — Project Guidelines

## Development Workflow
All work in this project must follow **Test-Driven Development (TDD)**:
1. **Write a failing test** that describes the desired behavior.
2. **Implement the minimum code** necessary to make the test pass.
3. **Refactor** the code while ensuring the tests remain green.

Tests are always written first; functionality comes after.

## Testing Guide

This project is developed natively on Windows.

## Running Tests on Windows
To run the tests natively on Windows, ensure your virtual environment is active and run:
```powershell
python -m pytest tests/
```

## Running Tests via WSL
Tests can also be executed within WSL using the following command, or one like it adjusted to run integration, unit, or other new test suites:

```powershell
wsl bash -lc 'source "${HOME}/.venv/bin/activate"; python3 -m pytest tests/integration --cov=. --cov-report=html --cov-branch --verbose'
```

**Important:** The `source "${HOME}/.venv/bin/activate"` command is critical as it activates the user's virtual environment within the WSL shell before executing pytest.
