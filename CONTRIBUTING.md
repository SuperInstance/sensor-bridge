# Contributing to sensor-bridge

## Development Setup

```bash
git clone <repo-url>
cd sensor-bridge
pip install -e .
```

## Running Tests

```bash
python3 -m pytest -v
```

## Code Style

- Follow existing patterns in the codebase
- Every new feature needs test coverage
- Use conventional commits: feat:, fix:, test:, docs:, chore:, refactor:

## Pull Request Checklist

- [ ] Tests pass locally
- [ ] New code has test coverage
- [ ] No secrets or credentials committed
- [ ] Documentation updated if behavior changed

## Architecture

See README.md for the component's role in the fleet.
