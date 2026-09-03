' Jingjing chat assistant - auto start on Windows login
' Usage: copy this file to the startup folder (Win+R -> shell:startup -> Enter)
' Starts C:\鲸鲸.exe silently (no console window flash)
Set ws = CreateObject("Wscript.Shell")
ws.Run """C:\鲸鲸.exe""", 0, False