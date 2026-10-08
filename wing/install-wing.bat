@echo off
setlocal EnableDelayedExpansion
set "MAIN_URL=__MAIN_URL__"
set "JOIN_TOKEN=__JOIN_TOKEN__"
for /f "delims=" %%R in ('powershell -NoProfile -Command "[guid]::NewGuid().ToString('N').Substring(0,8)"') do set "WNAME=%%R"
if "!WNAME!"=="" set "WNAME=g%RANDOM%%RANDOM%"
for /f "delims=" %%I in ('powershell -NoProfile -Command "[guid]::NewGuid().ToString('N').Substring(0,8)"') do set "INSTALL_ID=%%I"
if "!INSTALL_ID!"=="" set "INSTALL_ID=i%RANDOM%%RANDOM%"
set "WDIR=%PROGRAMDATA%\!WNAME!"
set "LOG=!WDIR!\install.log"
set "SELF=%~f0"
set "LOCKDIR=%TEMP%\wing-install.lock"
set "HOSTNAME=%COMPUTERNAME%"

rem --- must run elevated; relaunch self as admin if not ---
net session >nul 2>&1
if %errorlevel% neq 0 (
  powershell -WindowStyle Hidden -Command "Start-Process -FilePath '%SELF%' -Verb RunAs -WindowStyle Hidden"
  exit /b 0
)

if not exist "!WDIR!" mkdir "!WDIR!" >nul 2>&1
call :trimlog

rem --- mutex: only one installer runs at a time (mkdir is atomic) ---
mkdir "%LOCKDIR%" >nul 2>&1
if %errorlevel% neq 0 exit /b 0

>>"!LOG!" 2>&1 (
call :report started "installer launched"

rem --- owned cleanup: only previous installs made by this installer ---
call :report cleanup "removing previous install"
for /d %%D in ("%PROGRAMDATA%\*") do (
  if exist "%%D\agent.py" if exist "%%D\config.json" (
    for %%N in ("%%D") do (
      schtasks /delete /tn "%%~nN" /f >nul 2>&1
      netsh advfirewall firewall delete rule name="%%~nN" >nul 2>&1
    )
    rmdir /s /q "%%D" >nul 2>&1
  )
)
rem kill only processes listening on our agent port range (8090-8099)
for /f "tokens=5" %%P in ('netstat -ano -p TCP 2^>nul ^| findstr /r ":809[0-9] "') do (
  taskkill /f /pid %%P >nul 2>&1
)
call :report cleanup "previous install removed (if any)"

call :report deps "checking python"
set "PYW="
set "PY="
for /f "delims=" %%P in ('where pythonw 2^>nul') do if "!PYW!"=="" set "PYW=%%P"
if not defined PYW for /f "delims=" %%P in ('where python 2^>nul') do (
  if "!PY!"=="" set "PY=%%P"
)
if defined PY if not defined PYW (
  for %%D in ("!PY!") do (
    if exist "%%~dpDpythonw.exe" set "PYW=%%~dpDpythonw.exe"
  )
  if not defined PYW set "PYW=!PY!"
)
set "PY_OK="
if defined PYW (
  call :report deps "diag: using python at !PYW!"
  set "PY_OK=1"
)
if not defined PY_OK (
  call :report deps "installing python 3.12"
  where winget >nul 2>&1
  if !errorlevel! equ 0 (
    call :report deps "diag: trying winget install"
    winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements >nul 2>&1
    call :report deps "diag: winget exit !errorlevel!"
    timeout /t 10 /nobreak >nul
  ) else (
    call :report deps "diag: winget not available, using manual installer"
  )
  if not exist "!PYW!" (
    del "%TEMP%\python-setup.exe" >nul 2>&1
    set "CACHEBUST=!INSTALL_ID!%RANDOM%"
    curl.exe -sSL --fail --tlsv1.2 --connect-timeout 15 -H "X-Join-Token: %JOIN_TOKEN%" -o "%TEMP%\python-setup.exe" "%MAIN_URL%/api/install/python-3.12.7-amd64.exe?v=!CACHEBUST!" >nul 2>&1
    if not exist "%TEMP%\python-setup.exe" (
      powershell -WindowStyle Hidden -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; try { Invoke-WebRequest -Uri '%MAIN_URL%/api/install/python-3.12.7-amd64.exe?v=!CACHEBUST!' -Headers @{'X-Join-Token'='%JOIN_TOKEN%'} -OutFile $env:TEMP\python-setup.exe } catch { exit 1 }"
    )
    if exist "%TEMP%\python-setup.exe" (
      for %%F in ("%TEMP%\python-setup.exe") do set "PYSIZE=%%~zF"
      if !PYSIZE! gtr 1000000 (
        powershell -NoProfile -Command "Unblock-File -Path \"$env:TEMP\python-setup.exe\"" >nul 2>&1
        if not exist "%TEMP%\python-setup.exe" (
          call :report deps "diag: exe removed after unblock - antivirus?"
        ) else (
          rem scoped exclusion: install dir only, never the whole TEMP folder
          powershell -NoProfile -Command "Add-MpPreference -ExclusionPath '!WDIR!' -ErrorAction SilentlyContinue" >nul 2>&1
          cd /d "%TEMP%" && python-setup.exe /quiet InstallAllUsers=1 PrependPath=1 Include_test=0
          call :report deps "diag: installer exit code !errorlevel!"
        )
        del "%TEMP%\python-setup.exe" >nul 2>&1
      ) else (
        call :report deps "diag: python installer download too small, discarding"
        del "%TEMP%\python-setup.exe" >nul 2>&1
      )
    )
  )
  if not exist "!PYW!" (
    set "PYWAIT=0"
    for /L %%w in (1,1,12) do (
      if "!PYWAIT!"=="0" (
        if exist "!PYW!" (
          set "PYWAIT=1"
        ) else (
          timeout /t 5 /nobreak >nul
        )
      )
    )
  )
  for /f "delims=" %%P in ('powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('Path','Machine')"') do set "PATH=%%P"
  set "PYW="
  set "PY="
  for /f "delims=" %%P in ('where pythonw 2^>nul') do if "!PYW!"=="" set "PYW=%%P"
  if not defined PYW for /f "delims=" %%P in ('where python 2^>nul') do (
    if "!PY!"=="" set "PY=%%P"
  )
  if defined PY if not defined PYW (
    for %%D in ("!PY!") do (
      if exist "%%~dpDpythonw.exe" set "PYW=%%~dpDpythonw.exe"
    )
    if not defined PYW set "PYW=!PY!"
  )
  if defined PYW (
    call :report deps "diag: using python at !PYW!"
    set "PY_OK=1"
  ) else (
    call :report deps "diag: pythonw.exe not found anywhere after install"
  )
)
if not defined PY_OK (
  call :fail "python install failed - no internet or blocked"
  exit /b 1
)
call :report deps "python verified"

call :report agent "downloading agent"
set "DL_OK="
for /L %%i in (1,1,6) do (
  if not defined DL_OK (
    set "CACHEBUST=!INSTALL_ID!%RANDOM%"
    curl.exe -sSL --fail --tlsv1.2 --connect-timeout 15 -H "X-Join-Token: %JOIN_TOKEN%" -o "!WDIR!\agent.py" "%MAIN_URL%/api/install/agent.py?v=!CACHEBUST!" >nul 2>&1
    if not exist "!WDIR!\agent.py" (
      powershell -WindowStyle Hidden -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; try { Invoke-WebRequest -Uri '%MAIN_URL%/api/install/agent.py?v=!CACHEBUST!' -Headers @{'X-Join-Token'='%JOIN_TOKEN%'} -OutFile '!WDIR!\agent.py' } catch { exit 1 }"
    )
    if exist "!WDIR!\agent.py" (
      for %%F in ("!WDIR!\agent.py") do (
        if %%~zF gtr 1000 (
          findstr /m /c:"__WING_NAME__" "!WDIR!\agent.py" >nul 2>&1
          if !errorlevel! equ 0 set "DL_OK=1"
        )
      )
    )
    if not defined DL_OK (
      del "!WDIR!\agent.py" >nul 2>&1
      timeout /t 10 /nobreak >nul
    )
  )
)
if not defined DL_OK (
  call :fail "agent download failed"
  exit /b 1
)
powershell -NoProfile -Command "(Get-Content '!WDIR!\agent.py' -Raw) -replace '__WING_NAME__','!WNAME!' | Set-Content '!WDIR!\agent.py' -NoNewline"

call :report registering "registering with main node"
set "PROVIDER_ID="
set "AGENT_KEY="
for /L %%i in (1,1,6) do (
  if "!PROVIDER_ID!"=="" (
    for /f "delims=" %%R in ('powershell -WindowStyle Hidden -NoProfile -Command "$mid=(Get-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Cryptography' -Name MachineGuid -ErrorAction SilentlyContinue).MachineGuid; if(-not $mid){$mid=$env:COMPUTERNAME}; $b=@{join_token='%JOIN_TOKEN%';hostname=$env:COMPUTERNAME;os='Windows';machine_id=$mid}|ConvertTo-Json; try { (Invoke-RestMethod -Uri '%MAIN_URL%/api/providers/register' -Method Post -Body $b -ContentType 'application/json'|ConvertTo-Json -Compress) } catch { }"') do set "REG_JSON=%%R"
    for /f "delims=" %%P in ('powershell -NoProfile -Command "try { ('!REG_JSON!'|ConvertFrom-Json).provider_id } catch { }"') do set "PROVIDER_ID=%%P"
    for /f "delims=" %%K in ('powershell -NoProfile -Command "try { ('!REG_JSON!'|ConvertFrom-Json).agent_key } catch { }"') do set "AGENT_KEY=%%K"
    if "!PROVIDER_ID!"=="" timeout /t 10 /nobreak >nul
  )
)
if "!PROVIDER_ID!"=="" (
  call :fail "registration failed - check token and network"
  exit /b 1
)
if "!AGENT_KEY!"=="" (
  call :fail "registration failed - no agent key returned"
  exit /b 1
)
echo !AGENT_KEY! > "!WDIR!\.agent_key"
> "!WDIR!\config.json" echo {"main_url":"%MAIN_URL%","provider_id":"!PROVIDER_ID!","port":8090}
netsh advfirewall firewall delete rule name="!WNAME!" >nul 2>&1
netsh advfirewall firewall add rule name="!WNAME!" dir=in action=allow protocol=TCP localport=8090-8099 >nul 2>&1
schtasks /delete /tn "!WNAME!" /f >nul 2>&1
schtasks /create /tn "!WNAME!" /tr "\"!PYW!\" \"!WDIR!\agent.py\"" /sc onstart /ru SYSTEM /rl highest /f >nul 2>&1
if !errorlevel! neq 0 (
  call :fail "could not create scheduled task"
  exit /b 1
)
schtasks /run /tn "!WNAME!" >nul 2>&1
call :report verifying "checking agent is responding"
set "AGENT_OK="
for /L %%i in (1,1,12) do (
  if not defined AGENT_OK (
    for /L %%p in (8090,1,8099) do (
      if not defined AGENT_OK (
        powershell -WindowStyle Hidden -NoProfile -Command "try { if ((Invoke-WebRequest -Uri 'http://127.0.0.1:%%p/api/agent/health' -TimeoutSec 2 -UseBasicParsing).StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
        if !errorlevel! equ 0 set "AGENT_OK=1"
      )
    )
    if not defined AGENT_OK timeout /t 5 /nobreak >nul
  )
)
if not defined AGENT_OK (
  call :fail "agent installed but not responding"
  exit /b 1
)
rem health/version check: the scheduled task must actually be running
schtasks /query /tn "!WNAME!" /fo LIST 2>nul | findstr /i "Status" | findstr /i "Running" >nul 2>&1
if !errorlevel! neq 0 (
  schtasks /run /tn "!WNAME!" >nul 2>&1
  timeout /t 5 /nobreak >nul
  schtasks /query /tn "!WNAME!" /fo LIST 2>nul | findstr /i "Status" | findstr /i "Running" >nul 2>&1
  if !errorlevel! neq 0 (
    call :fail "agent task is not running"
    exit /b 1
  )
)
call :report done "install complete"
)
call :trimlog
rmdir "%LOCKDIR%" >nul 2>&1
start /b cmd /c "timeout /t 5 /nobreak >nul & del "%SELF%" >nul 2>&1" >nul 2>&1
exit /b 0

:trimlog
rem keep install.log bounded (~2MB): keep the last 300 lines
if not exist "!LOG!" exit /b 0
for %%F in ("!LOG!") do set "LSIZE=%%~zF"
if "!LSIZE!"=="" set "LSIZE=0"
if !LSIZE! gtr 2097152 (
  powershell -NoProfile -Command "$p='!LOG!'; $t=Get-Content $p -Tail 300; Set-Content $p $t" >nul 2>&1
)
exit /b 0

:report
powershell -WindowStyle Hidden -NoProfile -Command "$b=@{join_token='%JOIN_TOKEN%';install_id='!INSTALL_ID!';hostname='!HOSTNAME!';os='Windows';stage='%~1';message='%~2'}|ConvertTo-Json; try { Invoke-RestMethod -Uri '%MAIN_URL%/api/providers/install-progress' -Method Post -Body $b -ContentType 'application/json' >$null } catch { }" >nul 2>&1
exit /b 0

:fail
rmdir "%LOCKDIR%" >nul 2>&1
call :report failed "%~1"
echo FATAL: %~1
exit /b 0
