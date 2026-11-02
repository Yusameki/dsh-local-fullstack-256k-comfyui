' Strata On-Demand Proxy - silent launcher
' Called by Windows Task Scheduler at logon, or double-click to start manually.
' Uses pythonw.exe so no console window ever appears.

Option Explicit

Dim shell, fso, pythonExe, proxyScript, command

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

pythonExe = "<STRATA_HOME>\App\control\python\pythonw.exe"
proxyScript = "<STRATA_HOME>\proxy\strata-on-demand-proxy.py"

If Not fso.FileExists(pythonExe) Then
    pythonExe = "<STRATA_HOME>\App\control\python\python.exe"
End If

If Not fso.FileExists(pythonExe) Then
    WScript.Quit 2
End If

If Not fso.FileExists(proxyScript) Then
    WScript.Quit 3
End If

command = """" & pythonExe & """ """ & proxyScript & """"
shell.CurrentDirectory = "<STRATA_HOME>\proxy"
shell.Run command, 0, False
