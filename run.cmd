@echo off
rem Engram hidden launcher - runs the indexer with pythonw (no console window).
rem Usage: run.cmd [index|status|query ...]   (defaults to: index)
setlocal
set "HERE=%~dp0"
set "PYW="
for /f "delims=" %%i in ('where pythonw.exe 2^>nul') do if not defined PYW set "PYW=%%i"
if not defined PYW set "PYW=pythonw.exe"
set "ARGS=%*"
if "%ARGS%"=="" set "ARGS=index"
"%PYW%" "%HERE%engram.py" %ARGS%
endlocal
