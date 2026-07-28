; Inno Setup script. Compiled by build.bat when Inno Setup is installed.
; Installing (rather than unzipping) is what avoids the Mark-of-the-Web
; blocking that breaks pythonnet on colleagues' machines.

[Setup]
AppId={{7C2F3B9E-4A1D-4E5B-9C6F-0A1B2C3D4E5F}
AppName=Campkins Batch Uploader
AppVersion=1.0.0
AppPublisher=Campkins Cameras
DefaultDirName={autopf}\Campkins Batch Uploader
DefaultGroupName=Campkins Batch Uploader
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=CampkinsUploaderSetup
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\CampkinsBatchUploader.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"

[Files]
Source: "dist\CampkinsBatchUploader\*"; DestDir: "{app}"; \
  Flags: recursesubdirs ignoreversion; Excludes: "config.example.json"

[Icons]
Name: "{autoprograms}\Campkins Batch Uploader"; \
  Filename: "{app}\CampkinsBatchUploader.exe"
Name: "{autodesktop}\Campkins Batch Uploader"; \
  Filename: "{app}\CampkinsBatchUploader.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\CampkinsBatchUploader.exe"; \
  Description: "Launch Campkins Batch Uploader"; \
  Flags: nowait postinstall skipifsilent
