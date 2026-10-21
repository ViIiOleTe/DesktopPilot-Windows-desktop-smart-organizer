@echo off
setlocal
rem Run from an x64 Native Tools Command Prompt for Visual Studio 2022.
where cl >nul 2>nul
if errorlevel 1 (
    echo MSVC cl.exe was not found. Open the x64 Native Tools Command Prompt for VS 2022.
    echo Install Desktop development with C++ and a Windows 10/11 SDK first.
    exit /b 1
)
pushd "%~dp0.."
if errorlevel 1 exit /b 1
if not exist "build\native" mkdir "build\native"
if not exist "desktop_pilot\native" mkdir "desktop_pilot\native"
cl /nologo /utf-8 /std:c++17 /EHsc /W4 /LD /MT /O2 /DUNICODE /D_UNICODE /Fo"build\native\desktop_shell.obj" native\desktop_shell.cpp /link /OUT:"desktop_pilot\native\desktop_shell.dll" /IMPLIB:"build\native\desktop_shell.lib" /PDB:"build\native\desktop_shell.pdb" ole32.lib shell32.lib shlwapi.lib user32.lib uuid.lib oleaut32.lib shcore.lib
set "desktop_pilot_build_status=%errorlevel%"
popd
exit /b %desktop_pilot_build_status%
