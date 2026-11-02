' Strata On-Demand Proxy - watchdog
' ================================================================
' Job: keep the lazy proxy on 8090 alive. Nothing else.
' HARD LIMITS (this script never touches these):
'   1. Probes 127.0.0.1:8090 ONLY. Never looks at 8081, never touches
'      the Strata engine.
'   2. Does not start / keep alive / inspect any model engine process
'      or VRAM usage.
'   3. Does not modify the "unload engine after 15 min idle" logic in
'      strata-on-demand-proxy.py.
' The proxy itself starts the engine on demand and stops it after 900s
' idle. This watchdog is blind to that whole lifecycle.
' NOTE: this file must stay pure ASCII - Chinese comments make cscript
' fail with an "invalid character" error.
' ================================================================

Option Explicit

Const TARGET_HOST  = "127.0.0.1:8090"
Const CHECK_EVERY  = 30000   ' ms, one round every 30s
Const SPAWN_WAIT   = 5000    ' wait after launching, avoid double spawn
Const PROXY_VBS    = "<STRATA_HOME>\proxy\start-proxy.vbs"
Const LOG_FILE     = "<STRATA_HOME>\proxy\guard-proxy.log"
Const TMP_PORTS    = "<STRATA_HOME>\proxy\.guard-ports.tmp"

Dim shell, fso

Set shell = CreateObject("WScript.Shell")
Set fso   = CreateObject("Scripting.FileSystemObject")

If Not fso.FileExists(PROXY_VBS) Then
    LogLine "guard: start-proxy.vbs not found, exiting"
    WScript.Quit 2
End If

LogLine "guard: started, watching " & TARGET_HOST & " every 30s"

Do
    If Not IsPortListening() Then
        LogLine "guard: " & TARGET_HOST & " is down -> starting proxy"
        ' hidden window, do not wait - same way the scheduled task starts it
        shell.Run "wscript.exe """ & PROXY_VBS & """", 0, False
        WScript.Sleep SPAWN_WAIT
        If IsPortListening() Then
            LogLine "guard: proxy is back up"
        Else
            LogLine "guard: proxy still down after launch (retry next round)"
        End If
    End If

    WScript.Sleep CHECK_EVERY
Loop


' ---------------------------------------------------------------
' Port probe.
' Dumps the TCP table to a temp file, then scans it line by line in
' plain VBS. No nested-quote cmd/findstr tricks, so the exit code is
' trustworthy. Returns True only when TARGET_HOST appears on a line
' that also says LISTENING.
' ---------------------------------------------------------------
Function IsPortListening()
    Dim rc, f, line, found
    IsPortListening = False

    On Error Resume Next
    If fso.FileExists(TMP_PORTS) Then fso.DeleteFile TMP_PORTS, True
    rc = shell.Run("cmd /c netstat -ano -p TCP > """ & TMP_PORTS & """", 0, True)
    If Err.Number <> 0 Then
        Err.Clear
        On Error GoTo 0
        Exit Function
    End If
    On Error GoTo 0

    If Not fso.FileExists(TMP_PORTS) Then Exit Function

    On Error Resume Next
    Set f = fso.OpenTextFile(TMP_PORTS, 1, False)   ' 1 = ForReading
    Do While Not f.AtEndOfStream
        line = f.ReadLine
        If InStr(1, line, TARGET_HOST, vbTextCompare) > 0 Then
            If InStr(1, line, "LISTENING", vbTextCompare) > 0 Then
                found = True
                Exit Do
            End If
        End If
    Loop
    f.Close
    On Error GoTo 0

    IsPortListening = found
End Function


' ---------------------------------------------------------------
' Tiny log: one line on start / drop / recovery. Truncated at 256KB.
' ---------------------------------------------------------------
Sub LogLine(msg)
    On Error Resume Next
    Dim ts, f
    ts = Year(Now) & "-" & Right("0" & Month(Now), 2) & "-" & Right("0" & Day(Now), 2) & " " & _
         Right("0" & Hour(Now), 2) & ":" & Right("0" & Minute(Now), 2) & ":" & Right("0" & Second(Now), 2)

    If fso.FileExists(LOG_FILE) Then
        If fso.GetFile(LOG_FILE).Size > 262144 Then
            fso.DeleteFile LOG_FILE, True
        End If
    End If

    Set f = fso.OpenTextFile(LOG_FILE, 8, True)   ' 8 = ForAppending
    f.WriteLine ts & "  " & msg
    f.Close
    On Error GoTo 0
End Sub
