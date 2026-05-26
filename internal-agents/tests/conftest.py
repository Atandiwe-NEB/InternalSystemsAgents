"""Shared pytest configuration for the internal-agents test suite."""

import os

import pytest

# Ensure MOCK_MODE is always true in tests and a dummy API key is set
# so Settings validation passes without real credentials.
os.environ.setdefault("MOCK_MODE", "true")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-key")
