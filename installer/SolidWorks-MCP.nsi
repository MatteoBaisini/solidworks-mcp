; SolidWorks-MCP installer (NSIS 3, 64-bit, per user, no administrator rights).
;
; Build:   makensis -INPUTCHARSET UTF8 -DVERSION=2.2.0 installer/SolidWorks-MCP.nsi
;          (or installer/build.sh, which reads the version from the package)
; Output:  installer/dist/SolidWorks-MCP-Setup.exe
;
; The installer copies the PowerShell scripts in app/ and the work-folder template in
; workspace/, then runs app/setup.ps1, which installs uv, the server and Claude Code and
; prepares the work folder. The desktop shortcut runs app/launch.ps1.

Unicode true
Target amd64-unicode
ManifestDPIAware true

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "WinVer.nsh"

!define APP_NAME "SolidWorks-MCP"
!ifndef VERSION
  !define VERSION "0.0.0"
!endif
!define PUBLISHER "Matteo Baisini"
!define APP_URL "https://github.com/MatteoBaisini/solidworks-mcp"
!define REG_APP "Software\${APP_NAME}"
!define REG_UNINST "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"
!define POWERSHELL "$SYSDIR\WindowsPowerShell\v1.0\powershell.exe"
!define PS_ARGS "-NoProfile -ExecutionPolicy Bypass"
!define LAUNCH_ARGS '${PS_ARGS} -WindowStyle Hidden -File "$INSTDIR\launch.ps1"'

Name "${APP_NAME}"
OutFile "dist/${APP_NAME}-Setup.exe"
InstallDir "$LOCALAPPDATA\Programs\${APP_NAME}"
InstallDirRegKey HKCU "${REG_APP}" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
BrandingText "${APP_NAME} ${VERSION}"
ShowInstDetails show
ShowUninstDetails show

Var WorkspaceDir

; ----------------------------------------------------------------------------- pages
!define MUI_ICON "app/icon.ico"
!define MUI_UNICON "app/icon.ico"
!define MUI_WELCOMEFINISHPAGE_BITMAP "app/wizard.bmp"
!define MUI_ABORTWARNING

!define MUI_WELCOMEPAGE_TITLE "$(WelcomeTitle)"
!define MUI_WELCOMEPAGE_TEXT "$(WelcomeText)"
!insertmacro MUI_PAGE_WELCOME

!insertmacro MUI_PAGE_LICENSE "../LICENSE"

!define MUI_PAGE_HEADER_TEXT "$(WsHeader)"
!define MUI_PAGE_HEADER_SUBTEXT "$(WsSubHeader)"
!define MUI_DIRECTORYPAGE_TEXT_TOP "$(WsTop)"
!define MUI_DIRECTORYPAGE_TEXT_DESTINATION "$(WsDest)"
!define MUI_DIRECTORYPAGE_VARIABLE $WorkspaceDir
!insertmacro MUI_PAGE_DIRECTORY

!insertmacro MUI_PAGE_INSTFILES

!define MUI_FINISHPAGE_TITLE "$(FinishTitle)"
!define MUI_FINISHPAGE_TEXT "$(FinishText)"
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "$(RunText)"
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchApp
!define MUI_FINISHPAGE_SHOWREADME
!define MUI_FINISHPAGE_SHOWREADME_TEXT "$(OpenFolderText)"
!define MUI_FINISHPAGE_SHOWREADME_FUNCTION OpenWorkspace
!define MUI_FINISHPAGE_SHOWREADME_NOTCHECKED
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

; The first language is the fallback; Windows picks Italian on Italian systems.
!insertmacro MUI_LANGUAGE "English"
!insertmacro MUI_LANGUAGE "Italian"

; ----------------------------------------------------------------------------- strings
LangString WelcomeTitle ${LANG_ENGLISH} "Welcome to ${APP_NAME}"
LangString WelcomeTitle ${LANG_ITALIAN} "Benvenuto in ${APP_NAME}"
LangString WelcomeText ${LANG_ENGLISH} "This installs everything you need to design in SolidWorks with Claude:$\r$\n$\r$\n  - the SolidWorks MCP server$\r$\n  - Claude Code, if it is not installed yet$\r$\n  - a ready-to-use work folder$\r$\n$\r$\nNo administrator rights are needed; an internet connection is.$\r$\n$\r$\nYou need SolidWorks and a Claude Pro, Max, Team or Enterprise account."
LangString WelcomeText ${LANG_ITALIAN} "Installa tutto il necessario per progettare in SolidWorks con Claude:$\r$\n$\r$\n  - il server MCP per SolidWorks$\r$\n  - Claude Code, se non è già installato$\r$\n  - una cartella di lavoro pronta all'uso$\r$\n$\r$\nNon servono diritti di amministratore; serve la connessione a internet.$\r$\n$\r$\nServono SolidWorks e un account Claude Pro, Max, Team o Enterprise."
LangString WsHeader ${LANG_ENGLISH} "Work folder"
LangString WsHeader ${LANG_ITALIAN} "Cartella di lavoro"
LangString WsSubHeader ${LANG_ENGLISH} "Where Claude Code opens and your parts are saved."
LangString WsSubHeader ${LANG_ITALIAN} "Dove si apre Claude Code e dove vengono salvati i tuoi pezzi."
LangString WsTop ${LANG_ENGLISH} "Claude Code works in this folder and saves your parts in its projects subfolder. The folder is kept if you uninstall ${APP_NAME}."
LangString WsTop ${LANG_ITALIAN} "Claude Code lavora in questa cartella e salva i pezzi nella sottocartella projects. La cartella resta anche se disinstalli ${APP_NAME}."
LangString WsDest ${LANG_ENGLISH} "Work folder"
LangString WsDest ${LANG_ITALIAN} "Cartella di lavoro"
LangString Configuring ${LANG_ENGLISH} "Installing the server and Claude Code, this can take a minute..."
LangString Configuring ${LANG_ITALIAN} "Installazione del server e di Claude Code, può richiedere un minuto..."
LangString SetupFailed ${LANG_ENGLISH} "Part of the setup did not complete.$\r$\n$\r$\nCheck the internet connection, then open Start menu > ${APP_NAME} > Repair.$\r$\n$\r$\nLog: $INSTDIR\logs\setup.log"
LangString SetupFailed ${LANG_ITALIAN} "Una parte dell'installazione non è stata completata.$\r$\n$\r$\nControlla la connessione a internet, poi apri menu Start > ${APP_NAME} > Ripara.$\r$\n$\r$\nRegistro: $INSTDIR\logs\setup.log"
LangString NeedWin10 ${LANG_ENGLISH} "${APP_NAME} needs Windows 10 version 1809 or later."
LangString NeedWin10 ${LANG_ITALIAN} "${APP_NAME} richiede Windows 10 versione 1809 o successiva."
LangString AdminWarning ${LANG_ENGLISH} "The installer is running as administrator.$\r$\n$\r$\nSolidWorks and ${APP_NAME} must run with the same privileges, so install it as a normal user: close this window and double-click the installer.$\r$\n$\r$\nContinue anyway?"
LangString AdminWarning ${LANG_ITALIAN} "L'installer è in esecuzione come amministratore.$\r$\n$\r$\nSolidWorks e ${APP_NAME} devono avere gli stessi privilegi, quindi installalo come utente normale: chiudi questa finestra e fai doppio clic sull'installer.$\r$\n$\r$\nContinuare comunque?"
LangString FinishTitle ${LANG_ENGLISH} "${APP_NAME} is ready"
LangString FinishTitle ${LANG_ITALIAN} "${APP_NAME} è pronto"
LangString FinishText ${LANG_ENGLISH} "Open ${APP_NAME} from the desktop or the Start menu.$\r$\n$\r$\nThe first time, Claude Code asks you to log in and to trust the work folder."
LangString FinishText ${LANG_ITALIAN} "Apri ${APP_NAME} dal desktop o dal menu Start.$\r$\n$\r$\nAl primo avvio Claude Code chiede di accedere e di confermare che ti fidi della cartella di lavoro."
LangString RunText ${LANG_ENGLISH} "Start ${APP_NAME} now"
LangString RunText ${LANG_ITALIAN} "Avvia ${APP_NAME} ora"
LangString OpenFolderText ${LANG_ENGLISH} "Open the work folder"
LangString OpenFolderText ${LANG_ITALIAN} "Apri la cartella di lavoro"
LangString ShortcutDesc ${LANG_ENGLISH} "Design in SolidWorks with Claude"
LangString ShortcutDesc ${LANG_ITALIAN} "Progetta in SolidWorks con Claude"
LangString LnkFolder ${LANG_ENGLISH} "Work folder"
LangString LnkFolder ${LANG_ITALIAN} "Cartella di lavoro"
LangString LnkRepair ${LANG_ENGLISH} "Repair"
LangString LnkRepair ${LANG_ITALIAN} "Ripara"
LangString LnkUninstall ${LANG_ENGLISH} "Uninstall"
LangString LnkUninstall ${LANG_ITALIAN} "Disinstalla"
LangString UnKept ${LANG_ENGLISH} "${APP_NAME} was removed. Your work folder and parts were kept:$\r$\n$WorkspaceDir$\r$\n$\r$\nClaude Code and uv stay installed, since other programs may use them."
LangString UnKept ${LANG_ITALIAN} "${APP_NAME} è stato rimosso. La cartella di lavoro e i tuoi pezzi sono stati mantenuti:$\r$\n$WorkspaceDir$\r$\n$\r$\nClaude Code e uv restano installati, perché altri programmi potrebbero usarli."

VIProductVersion "${VERSION}.0"
VIAddVersionKey /LANG=${LANG_ENGLISH} "ProductName" "${APP_NAME}"
VIAddVersionKey /LANG=${LANG_ENGLISH} "ProductVersion" "${VERSION}"
VIAddVersionKey /LANG=${LANG_ENGLISH} "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=${LANG_ENGLISH} "FileDescription" "${APP_NAME} installer"
VIAddVersionKey /LANG=${LANG_ENGLISH} "CompanyName" "${PUBLISHER}"
VIAddVersionKey /LANG=${LANG_ENGLISH} "LegalCopyright" "MIT License, ${PUBLISHER}"

; ----------------------------------------------------------------------------- install
Function .onInit
  ${IfNot} ${AtLeastBuild} 17763
    MessageBox MB_ICONSTOP|MB_OK "$(NeedWin10)" /SD IDOK
    Abort
  ${EndIf}
  UserInfo::GetAccountType
  Pop $0
  ${If} $0 == "Admin"
    MessageBox MB_ICONEXCLAMATION|MB_YESNO|MB_DEFBUTTON2 "$(AdminWarning)" /SD IDYES IDYES +2
    Abort
  ${EndIf}
  ReadRegStr $WorkspaceDir HKCU "${REG_APP}" "WorkspaceDir"
  ${If} $WorkspaceDir == ""
    StrCpy $WorkspaceDir "$DOCUMENTS\${APP_NAME}"
  ${EndIf}
FunctionEnd

Section "${APP_NAME}" SecMain
  SectionIn RO
  ; Python, the server and Claude Code are downloaded by setup.ps1: count them in "Space required".
  AddSize 300000
  SetOutPath "$INSTDIR"
  File "app/common.ps1"
  File "app/setup.ps1"
  File "app/launch.ps1"
  File "app/icon.ico"
  SetOutPath "$INSTDIR\workspace"
  File "workspace/CLAUDE.md"
  File "workspace/README.md"
  File "workspace/solidworks-mcp.toml.in"
  SetOutPath "$INSTDIR\workspace\.claude"
  ; Kept as workspace/claude/ in the repository so it is not read as project settings there.
  File "workspace/claude/settings.json"
  SetOutPath "$INSTDIR"
  WriteUninstaller "$INSTDIR\uninstall.exe"

  WriteRegStr HKCU "${REG_APP}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${REG_APP}" "WorkspaceDir" "$WorkspaceDir"
  WriteRegStr HKCU "${REG_UNINST}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "${REG_UNINST}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${REG_UNINST}" "Publisher" "${PUBLISHER}"
  WriteRegStr HKCU "${REG_UNINST}" "URLInfoAbout" "${APP_URL}"
  WriteRegStr HKCU "${REG_UNINST}" "DisplayIcon" "$INSTDIR\icon.ico"
  WriteRegStr HKCU "${REG_UNINST}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${REG_UNINST}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "${REG_UNINST}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegDWORD HKCU "${REG_UNINST}" "NoModify" 1
  WriteRegDWORD HKCU "${REG_UNINST}" "NoRepair" 1

  ; uv, the server, Claude Code and the work folder. Output goes to the details list.
  ; "\." keeps a trailing backslash from escaping the closing quote; setup.ps1 normalises it.
  DetailPrint "$(Configuring)"
  nsExec::ExecToLog '"${POWERSHELL}" ${PS_ARGS} -NonInteractive -File "$INSTDIR\setup.ps1" -WorkspaceDir "$WorkspaceDir\."'
  Pop $0
  ${If} $0 != "0"
    MessageBox MB_ICONEXCLAMATION|MB_OK "$(SetupFailed)" /SD IDOK
  ${EndIf}

  CreateShortCut "$DESKTOP\${APP_NAME}.lnk" "${POWERSHELL}" '${LAUNCH_ARGS}' "$INSTDIR\icon.ico" 0 SW_SHOWMINIMIZED "" "$(ShortcutDesc)"
  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "${POWERSHELL}" '${LAUNCH_ARGS}' "$INSTDIR\icon.ico" 0 SW_SHOWMINIMIZED "" "$(ShortcutDesc)"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\$(LnkFolder).lnk" "$WorkspaceDir"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\$(LnkRepair).lnk" "${POWERSHELL}" '${PS_ARGS} -File "$INSTDIR\setup.ps1" -Pause' "$INSTDIR\icon.ico" 0
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\$(LnkUninstall).lnk" "$INSTDIR\uninstall.exe"
SectionEnd

Function LaunchApp
  ; Just installed and checked: skip the update check for a faster first start.
  Exec '"${POWERSHELL}" ${LAUNCH_ARGS} -NoUpdate'
FunctionEnd

Function OpenWorkspace
  ExecShell "open" "$WorkspaceDir"
FunctionEnd

; ----------------------------------------------------------------------------- uninstall
Function un.onInit
  ReadRegStr $WorkspaceDir HKCU "${REG_APP}" "WorkspaceDir"
FunctionEnd

Section "Uninstall"
  ; Removes the server tool; the work folder with the user's parts is never touched.
  nsExec::ExecToLog '"${POWERSHELL}" ${PS_ARGS} -NonInteractive -File "$INSTDIR\setup.ps1" -Uninstall'
  Pop $0
  Delete "$DESKTOP\${APP_NAME}.lnk"
  RMDir /r "$SMPROGRAMS\${APP_NAME}"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "${REG_UNINST}"
  DeleteRegKey HKCU "${REG_APP}"
  MessageBox MB_ICONINFORMATION|MB_OK "$(UnKept)" /SD IDOK
SectionEnd
