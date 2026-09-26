"""Shared test doubles. Deliberately dependency-free: no network, no clock."""

from tests.support.http import FakeTransport, ScriptedResponse, json_response, load_fixture

__all__ = ["FakeTransport", "ScriptedResponse", "json_response", "load_fixture"]
