@echo off
REM ============================================================================
REM disconnect_rdp_keep_gui.bat — Disconnect RDP without locking GUI Session
REM ============================================================================
REM Run as Administrator when exiting RDP on the Windows VM.
REM Standard "X" disconnect locks the Windows desktop and detaches the GPU/GUI
REM display surface, which causes SAP2000 DirectX / AutoCAD COM calls to hang.
REM `tscon` hands the session back to the local console so the GUI stays active!
REM ============================================================================
for /f "skip=1 tokens=3" %%s in ('query user %USERNAME%') do (
    echo Redirecting session %%s to console...
    %windir%\System32\tscon.exe %%s /dest:console
)
