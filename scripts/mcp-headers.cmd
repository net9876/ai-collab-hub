@echo off
rem Fast MCP header helper for Claude Code (headersHelper) and Codex
rem (http_headers_helper). Both kill helpers after 10 seconds, and starting
rem PowerShell + az can take longer, so this only prints a cached header file.
rem The cache is filled by `mcp-headers.ps1 -Refresh`, run by the scheduled task
rem from scripts/token-refresh-task.ps1 (or by hand). No child process is
rem started here: a child would inherit the caller's output pipe and make it wait.
rem Usage: mcp-headers.cmd <agent>   e.g. mcp-headers.cmd codex
setlocal
set "AGENT=%~1"
if "%AGENT%"=="" set "AGENT=other"
set "CACHE=%LOCALAPPDATA%\ai-collab-hub\headers-%AGENT%.json"
if exist "%CACHE%" (
    type "%CACHE%"
    exit /b 0
)
>&2 echo collab: no cached token. Run: pwsh -File "%~dp0mcp-headers.ps1" -Refresh  (needs az login)
exit /b 1
