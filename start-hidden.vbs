' Starts JARVIS in the background with no window, e.g. at login, so "Hey JARVIS" works anytime.
' The log goes to backend\data\jarvis.log. To stop JARVIS: Task Manager > python.exe, or say "Hey JARVIS, ..." to open the HUD.
Set sh = CreateObject("WScript.Shell")
here = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.Run "powershell -NoProfile -WindowStyle Hidden -Command ""try { Invoke-RestMethod http://127.0.0.1:11434/api/tags -TimeoutSec 2 | Out-Null } catch { Start-Process ollama -ArgumentList serve -WindowStyle Hidden }""", 0, True
sh.CurrentDirectory = here & "\backend"
sh.Run "cmd /c set PYTHONUNBUFFERED=1 && .venv\Scripts\python.exe -m app > data\jarvis.log 2>&1", 0, False
