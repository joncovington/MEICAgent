"""Pytest fixtures for the MEICAgent test suite."""
import pytest
from mock_mcp import MockMCP


@pytest.fixture
def mock_midday():
    return MockMCP("midday_normal")


@pytest.fixture
def mock_stop_filled():
    return MockMCP("stop_filled")


@pytest.fixture
def mock_bp_rejected():
    return MockMCP("bp_rejected")
