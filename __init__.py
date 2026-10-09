"""
MyFitnessPal MCP Server

A Model Context Protocol (MCP) server for interacting with MyFitnessPal data.
"""

__version__ = "1.0.0"
__author__ = "Adam"

from .server import mcp

__all__ = ["mcp", "__version__"]
